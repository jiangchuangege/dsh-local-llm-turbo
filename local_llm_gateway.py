#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_llm_gateway.py  --  本地模型 DeepSeek Harness(DSH)加速网关

让 llama.cpp 服务的本地小模型(Qwen2.5-Coder 等)对接 DSH 时:
  1) 普通聊天 → 流式直通(边生成边返回, 感知速度大幅提升);
  2) 工具调用 → 流式读上游 + 提前止损退化循环 + 改写为标准 tool_calls。

解决两个痛点:
  - 小模型在大量工具 schema 下"退化刷屏"(重复 type/false/JSON 碎片) → 提前掐断, 不再生成垃圾;
  - 单线程 llama-server 被一个长请求占死 → 串行队列, 不至于排队雪崩。

用法
----
    python local_llm_gateway.py --port 8081 --upstream http://127.0.0.1:8080

把 DSH 里该模型的 baseURL 指向 http://127.0.0.1:8081/v1 即可。
"""

import argparse
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

UPSTREAM_DEFAULT = "http://127.0.0.1:8080"
LISTEN_PORT_DEFAULT = 8081
QWEN_STOPS = ["<|im_end|>", "<|im_start|>", "</tools>", "<tools>"]
DESC_MAX = 180
PARAM_DESC_MAX = 110

# 工具请求并发上限(单线程服务, 串行更稳)
MAX_CONCURRENT = 1


def _find_json(text):
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    return text[start:end + 1]


def parse_tool_call(content):
    if not content or not isinstance(content, str) or not content.strip():
        return None
    candidates = []
    m = re.search(r"<tools>\s*([\s\S]*?)\s*</tools>", content, re.IGNORECASE)
    if m:
        candidates.append(m.group(1))
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
    if m:
        candidates.append(m.group(1))
    candidates.append(_find_json(content))
    for c in candidates:
        if not c:
            continue
        body = _find_json(c)
        if not body:
            continue
        try:
            obj = json.loads(body)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name, args = obj.get("name"), obj.get("arguments")
        if name is None and isinstance(obj.get("function"), dict):
            fn = obj["function"]
            name, args = fn.get("name"), fn.get("arguments", args)
        if not isinstance(name, str) or not name:
            continue
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        elif args is None:
            args = "{}"
        elif not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        return name, args
    return None


def _trim_str(s, limit):
    return (s or "")[:limit]


def trim_tools(tools, max_tools):
    if not isinstance(tools, list):
        return tools
    out = []
    for t in tools[:max_tools]:
        try:
            t = json.loads(json.dumps(t))
        except Exception:
            continue
        fn = t.get("function") if isinstance(t, dict) else None
        if fn is None:
            fn = t if isinstance(t.get("name"), str) else None
        if fn is None:
            continue
        if isinstance(fn.get("description"), str):
            fn["description"] = _trim_str(fn["description"], DESC_MAX)
        params = fn.get("parameters")
        if isinstance(params, dict) and isinstance(params.get("properties"), dict):
            for k, v in list(params["properties"].items()):
                if isinstance(v, dict) and isinstance(v.get("description"), str):
                    v["description"] = _trim_str(v["description"], PARAM_DESC_MAX)
                    params["properties"][k] = v
        if isinstance(fn.get("name"), str):
            out.append({"type": "function", "function": fn})
    return out


def looks_degenerate(content):
    """判断是否陷入退化循环(大量重复占位词 / 过长未收尾)。"""
    if not content or not isinstance(content, str) or len(content.strip()) < 40:
        return False
    words = re.findall(r"[A-Za-z]{4,}", content.lower())
    if not words:
        return False
    total = len(words)
    top = max((words.count(w) for w in set(words)), default=0)
    if total >= 18 and top / total > 0.28:
        return True
    if len(content) > 500 and not content.rstrip().endswith(("<|im_end|>", "```", "}")):
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalLLMGateway/1.0"
    protocol_version = "HTTP/1.1"

    UPSTREAM = UPSTREAM_DEFAULT
    MAX_TOOLS = 6
    REPEAT_PENALTY = 1.2

    # 串行队列: 单线程服务一次只放一个生成, 避免并发雪崩
    _sem = threading.BoundedSemaphore(MAX_CONCURRENT)

    def log_message(self, fmt, *args):
        pass

    # ---------- 通用 ----------
    def _upstream_url(self, path):
        return self.UPSTREAM + path

    def _headers(self):
        return {"Content-Type": "application/json"}

    def do_GET(self):
        self._passthrough()

    def do_OPTIONS(self):
        self.send_response(204)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ---------- 主入口 ----------
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if not self.path.rstrip("/").endswith("/v1/chat/completions"):
            self._passthrough(build=False, body=body)
            return

        client_stream = bool(body.get("stream", False))
        has_tools = bool(body.get("tools"))

        if not has_tools:
            # 普通聊天: 流式直通(感知速度大提升)
            self._stream_chat(body, client_stream)
            return

        # 工具调用: 流式读上游, 提前止损, 再改写
        self._stream_tools(body, client_stream)

    # ---------- 普通聊天: 流式直通 ----------
    def _stream_chat(self, body, client_stream):
        upstream = dict(body)
        upstream["stream"] = True
        # 透传给客户端: 若是非流式客户端, 我们把流聚合成一条再回
        if client_stream:
            self._relay_stream(upstream, rewrite=False)
        else:
            gathered = self._gather_stream(upstream)
            self._write_json(200, gathered, False)

    # ---------- 工具调用: 流式读 + 提前止损 + 改写 ----------
    def _stream_tools(self, body, client_stream):
        upstream = dict(body)
        upstream["stream"] = True
        upstream["tools"] = trim_tools(body["tools"], self.MAX_TOOLS)
        upstream.setdefault("repeat_penalty", self.REPEAT_PENALTY)
        upstream.setdefault("min_p", 0.05)
        if isinstance(upstream.get("temperature"), (int, float)) and upstream["temperature"] > 0.9:
            upstream["temperature"] = 0.9
        upstream.setdefault("top_p", 0.9)
        upstream["max_tokens"] = min(int(upstream.get("max_tokens") or 1024), 384)
        stops = list(QWEN_STOPS)
        ex = upstream.get("stop")
        if isinstance(ex, list):
            stops.extend(ex)
        elif isinstance(ex, str):
            stops.append(ex)
        upstream["stop"] = stops

        content = self._gather_stream_abort_on_loop(upstream)
        parsed = parse_tool_call(content)
        if not parsed and looks_degenerate(content):
            # 二次: 更小工具集 + 更强惩罚再试
            r = dict(upstream)
            r["tools"] = trim_tools(body["tools"], max(1, min(3, self.MAX_TOOLS)))
            r["repeat_penalty"] = min(1.5, self.REPEAT_PENALTY + 0.2)
            r["max_tokens"] = 180
            content = self._gather_stream_abort_on_loop(r)
            parsed = parse_tool_call(content)

        if not parsed and looks_degenerate(content):
            self._write_json(200, self._clean_ok(
                "工具调用生成失败(模型对工具列表过载, 已精简并防退化)。"
                "请减少该模型可用的工具/技能数量, 或换用更大的模型。"), client_stream)
            return

        if parsed:
            name, args_str = parsed
            tool_call = {
                "id": "call_" + uuid.uuid4().hex[:16],
                "type": "function",
                "function": {"name": name, "arguments": args_str},
            }
            msg = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
            obj = {
                "id": "chatcmpl-" + uuid.uuid4().hex[:12],
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": upstream.get("model", "qwen-local"),
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}],
            }
            self._write_json(200, obj, client_stream, toolcalls=True)
            return

        # 没解析出但也没严重退化: 原样返回(可能是普通文本回复)
        obj = {"id": "chatcmpl-" + uuid.uuid4().hex[:12],
               "object": "chat.completion",
               "created": int(__import__("time").time()),
               "model": upstream.get("model", "qwen-local"),
               "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop"}]}
        self._write_json(200, obj, client_stream)

    # ---------- 读上游流 ----------
    def _open_stream(self, body):
        req = Request(self._upstream_url(self.path), data=json.dumps(body).encode("utf-8"),
                      headers=self._headers(), method="POST")
        return urlopen(req, timeout=180)

    def _relay_stream(self, body, rewrite=False):
        """把上游 SSE 逐块原样透传给客户端(不会改写)。"""
        self._sem.acquire()
        try:
            resp = self._open_stream(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self._cors()
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                for line in resp:
                    if self.wfile.closed:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            finally:
                resp.close()
        except Exception as e:
            try:
                self._write_json(502, {"error": {"message": str(e), "type": "gateway"}}, False)
            except Exception:
                pass
        finally:
            self._sem.release()

    def _gather_stream(self, body):
        """聚合上游非流式结果(不提前止损)。"""
        self._sem.acquire()
        try:
            resp = self._open_stream(body)
            raw = resp.read().decode("utf-8")
            resp.close()
            # 上游(此处强制非流式返回)应是一个完整 JSON
            try:
                return json.loads(raw)
            except Exception:
                return {"choices": [{"message": {"role": "assistant", "content": raw}, "finish_reason": "stop"}]}
        finally:
            self._sem.release()

    def _gather_stream_abort_on_loop(self, body):
        """流式读上游并累积 content; 一旦判定退化立刻掐断(关连接), 省时。"""
        self._sem.acquire()
        text = ""
        try:
            resp = self._open_stream(body)
            for line in resp:
                # 每块是 "data: {...json...}" 或 "data: [DONE]"
                text += self._consume_chunk(line, 0)
                if looks_degenerate(text):
                    # 提前止损: 关闭上游连接, 停止生成
                    break
            resp.close()
        except Exception:
            pass
        finally:
            self._sem.release()
        return text

    def _consume_chunk(self, line, acc):
        try:
            line = line.decode("utf-8", "ignore").strip()
        except Exception:
            line = ""
        if not line.startswith("data:"):
            return ""
        payload = line[5:].strip()
        if payload == "[DONE]":
            return ""
        try:
            chunk = json.loads(payload)
            delta = chunk["choices"][0].get("delta") or {}
            return delta.get("content") or ""
        except Exception:
            return ""

    # ---------- 其它端点透传 ----------
    def _passthrough(self, build=False, body=None):
        method = self.command
        data = None
        if body is not None:
            try:
                data = json.dumps(body).encode("utf-8")
            except Exception:
                data = None
        req = Request(self._upstream_url(self.path), data=data, method=method,
                      headers=self._headers())
        try:
            with urlopen(req, timeout=60) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                self._common_headers()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self._common_headers()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            self._write_json(502, {"error": {"message": "gateway passthrough failed", "type": "gateway"}}, False)

    def _clean_ok(self, msg):
        return {"id": "chatcmpl-" + uuid.uuid4().hex[:12],
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": "qwen-local",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": msg}, "finish_reason": "stop"}]}

    # ---------- 写响应 ----------
    def _common_headers(self):
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _write_json(self, status, obj, client_stream, toolcalls=False):
        if client_stream:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self._cors()
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            mid = obj.get("id", "chatcmpl-proxy")
            model = obj.get("model", "qwen-local")
            choice = obj["choices"][0]
            self.wfile.write(_sse(mid, model, {"role": "assistant", "content": ""}, None).encode("utf-8"))
            if toolcalls:
                tc = choice["message"]["tool_calls"][0]
                self.wfile.write(_sse(mid, model, {"tool_calls": [{"index": 0, "id": tc["id"], "type": "function",
                                                          "function": {"name": tc["function"]["name"], "arguments": ""}}]}, None).encode("utf-8"))
                self.wfile.write(_sse(mid, model, {"tool_calls": [{"index": 0, "function": {"arguments": tc["function"]["arguments"]}}]}, None).encode("utf-8"))
                self.wfile.write(_sse(mid, model, {}, "tool_calls").encode("utf-8"))
            else:
                content = choice["message"].get("content", "")
                if content:
                    self.wfile.write(_sse(mid, model, {"content": content}, None).encode("utf-8"))
                self.wfile.write(_sse(mid, model, {}, choice.get("finish_reason", "stop")).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _sse(msg_id, model, delta, finish):
    chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": int(__import__("time").time()),
             "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=LISTEN_PORT_DEFAULT)
    ap.add_argument("--upstream", default=UPSTREAM_DEFAULT)
    ap.add_argument("--max-tools", type=int, default=6, help="小模型最大工具数(建议 4~6, 过大易退化)")
    ap.add_argument("--repeat-penalty", type=float, default=1.2, help="抗退化重复惩罚")
    args = ap.parse_args()

    Handler.UPSTREAM = args.upstream.rstrip("/")
    Handler.MAX_TOOLS = max(1, args.max_tools)
    Handler.REPEAT_PENALTY = args.repeat_penalty

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[local_llm_gateway] listening on http://127.0.0.1:{args.port}/v1  ->  upstream {args.upstream}")
    print(f"[local_llm_gateway] max-tools={Handler.MAX_TOOLS} repeat-penalty={Handler.REPEAT_PENALTY}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

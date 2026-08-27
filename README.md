# 🐋 dsh-local-llm-turbo — 本地模型 DeepSeek Harness 加速网关

> 让 **llama.cpp 服务的本地小模型**(如 `Qwen2.5-Coder-7B-Instruct`)对接 **DeepSeek Harness (DSH)** 时**更快、更稳、不被"退化刷屏"淹没**的零依赖网关。

---

## 它解决什么？

本地小模型对接 DSH 的两个老大难，这个网关直接处理:

| 痛点 | 表现 | 本网关的处理 |
|------|------|------|
| 🐢 **慢** | 普通聊天要等整段生成完才显示 | **流式直通**:边生成边把 token 推给客户端,感知速度大幅提升 |
| 🌀 **退化刷屏** | 一次给太多工具 schema 时,模型疯狂重复 `type`/`false` | **提前止损**:在读上游流时一发现退化循环**立刻掐断**,不再生成几百个垃圾 token |
| 🚦 **单线程雪崩** | 一个长请求占死 llama-server,后续全排队 | **串行队列**:一次只放一个生成,不堆积 |

> 核心思路:模型能力没法凭空变强,但**"边跑边给你看" + "一崩就刹停"** 是真真实实的提速。

---

## 怎么用

### 1. 启动网关
```bash
python local_llm_gateway.py --port 8081 --upstream http://127.0.0.1:8080
# 小模型建议:
python local_llm_gateway.py --port 8081 --upstream http://127.0.0.1:8080 --max-tools 4 --repeat-penalty 1.2
```

### 2. 把 DSH 的模型 baseURL 指向网关
在 DSH 设置(如 `~/.dsh/settings.yaml`)里把该模型 provider 的 `baseURL` 改为:
```yaml
llm-pi-ai:
  providers:
    llama:
      api: openai-completions
      baseURL: http://127.0.0.1:8081/v1        # ← 指网关, 不要指 8080
      models:
        - id: Qwen2.5-Coder-7B-Instruct.Q4_K_M.gguf
          contextWindow: 31744                  # 必须按实际 n_ctx 声明
          maxTokens: 4096
```

### 3. 自检
```bash
curl -s -X POST http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":true}'
```
普通聊天会**流式**回来(逐段出现);带 `tools` 的请求则返回改写后的标准 `tool_calls`。

---

## 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--port` | `8081` | 网关监听端口 |
| `--upstream` | `http://127.0.0.1:8080` | llama-server 地址(不含 `/v1`) |
| `--max-tools` | `6` | 发给模型的最大工具数(小模型 4~6,过大易退化) |
| `--repeat-penalty` | `1.2` | 抗退化重复惩罚 |

---

## 与 `qwen_tool_proxy.py` 的区别

| 特性 | `qwen_tool_proxy.py` | **本网关 (`local_llm_gateway.py`)** |
|------|------|------|
| 普通聊天 | 强制非流式,等完全部才回 | **流式直通**,边生成边显示 |
| 工具调用 | 非流式取完整再改写 | **流式读 + 提前止损**,退化即掐断 |
| 退化处理 | 二次重试(更慢) | 提前止损 + 降工具重试 |
| 并发 | 无限制 | 串行队列,防雪崩 |

---

## 局限性(如实说明)

- **不能凭空加速推理**:CPU 上跑 7B,生成本身就要几十秒;网关只是"边跑边给你看 + 一崩就刹停"。
- **模型太弱时工具调用仍可能不准**:建议减少 DSH 暴露的工具/技能,或换 `Qwen2.5-Coder-32B-Instruct` 等更强的模型。
- 需要**常驻运行**网关进程(可配开机自启或交给桌面客户端拉起)。

---

## 相关

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — 本地推理服务
- [DeepSeek Harness (DSH)](https://github.com/deepseek-ai) — 被加速/驱动的 Harness

## 许可
MIT

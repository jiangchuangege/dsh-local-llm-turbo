# 🐋 dsh-local-llm-turbo — 本地模型 DeepSeek Harness 加速网关

> 一个**模型无关的 OpenAI 兼容网关**，给对接 **DeepSeek Harness (DSH)** 的模型加上**流式输出**与**工具调用可靠化**辅助。
> 它不绑定某个模型，服务任何 OpenAI 兼容后端（本地 llama.cpp / vLLM / 云端 API）上的**能调用工具的模型**。

---

## 它能做什么

| 能力 | 说明 |
|------|------|
| 🐢 **流式直通** | 普通聊天边生成边返回，感知速度大幅提升（不一定要等全量） |
| 🔧 **工具调用可靠化** | 用 **JSON-Schema Grammar 约束解码**，强制模型输出合法的 `{"name","arguments"}`，杜绝"退化刷屏"和乱格式 |
| 🛡️ **退化防护** | 对弱小模型：工具预算、抗退化采样、提前止损、垃圾兜底 |
| 🚦 **串行队列** | 单线程本地服务一次只放一个生成，避免请求雪崩 |

---

## ✅ 哪些模型**能用**（推荐 & 兼容）

> 🎯 一句话：**工具调用必须选"能干"的模型。** 下面这些都能配合本网关稳定工作。

### 1️⃣ 云端 Qwen（推荐，最省事）
| 模型 | 说明 |
|------|------|
| **Qwen3.6 / Qwen3.7**（阿里云 `qwen-token-plan-cn`） | **原生函数调用，极其可靠**，在 DSH 里直接调用工具干活。你只需在 DSH 配好后端并选择该模型 |

### 2️⃣ 本地大模型（想本地跑）
| 模型 | 说明 |
|------|------|
| **Qwen2.5-Coder-32B-Instruct**（GGUF） | 工具调用可靠得多，需 ~20GB+ 显存 |
| **Qwen3**（GGUF） | 原生函数调用，可靠性高 |
| 其它 OpenAI 兼容 + 原生支持 `tool_calls` 的模型 | 网关**透明放行**，不会破坏其原生工具调用 |

> ⚠️ **不建议**：低量化的小模型——实测扛不住 DSH 的 Agent 环境（大系统提示 + 大量工具），会退化刷屏或输出空。**底座能力不够，网关救不了。**

---

## 怎么用

### 1. 启动网关
```bash
python local_llm_gateway.py --port 8081 --upstream http://127.0.0.1:8080
```
双击 `启动加速网关.bat` 亦可（Windows）。

### 2. 把 DSH 的模型 baseURL 指向网关
在 DSH 设置(如 `~/.dsh/settings.yaml`)里，把该模型 provider 的 `baseURL` 改为：
```yaml
llm-pi-ai:
  providers:
    llama:
      api: openai-completions
      baseURL: http://127.0.0.1:8081/v1        # 指网关
      models:
        - id: <你的能调用工具的模型 id>           # 例如 Qwen2.5-Coder-32B-Instruct, 或云 Qwen3.6
          contextWindow: <按实际 n_ctx 声明>
          maxTokens: 4096
```

### 3. 自检
```bash
curl -s -X POST http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":true}'
```
普通聊天会**流式**返回；带 `tools` 的请求则返回改写后的标准 `tool_calls`（或透传原生的）。

---

## 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--port` | `8081` | 网关监听端口 |
| `--upstream` | `http://127.0.0.1:8080` | llama-server / 模型服务地址(不含 `/v1`) |
| `--max-tools` | `6` | 发给模型的最大工具数(弱小模型建议 4~6) |
| `--repeat-penalty` | `1.2` | 抗退化重复惩罚 |

---

## 局限性(如实说明)

- **不能凭空造出能力**：网关是"边跑边给你看 + 一崩就刹停 + 格式约束"，但**模型底子弱就没法可靠调用工具**；
- 工具调用是否靠谱，**取决于模型本身**；选上表列出的能干模型即可稳定工作；
- 需要**常驻运行**网关进程。

---

## 相关

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — 本地推理服务
- [DeepSeek Harness (DSH)](https://github.com/deepseek-ai) — 被加速/驱动的 Harness

## 许可
MIT

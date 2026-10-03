---
title: Ollama Cloud
description: 将 Ollama Cloud 配置为显式 API key 提供商。
---

# Ollama Cloud

Ares/Hermes 将 Ollama Cloud 作为显式的 `ollama-cloud` 提供商。默认情况下，请求发送到 `https://ollama.com/v1` 的 OpenAI 兼容 Chat Completions API，并使用 `OLLAMA_API_KEY` 认证。此路径无需在本机安装 Ollama。

直接使用 Cloud API key 与登录 Ollama 应用或 CLI 是两回事。此路径不会读取本机 Ollama 登录状态，也不会复制其他提供商的凭据。

## 准备好时连接账号

1. 在 [Ollama key 设置](https://ollama.com/settings/keys)创建 API key。Ollama 的 [Cloud 指南](https://docs.ollama.com/cloud)介绍了 API key 流程。
2. 将 key 保存为当前 profile 的 secrets-only `.env` 文件中的 `OLLAMA_API_KEY`（位于该 profile 的 `HERMES_HOME` 下），或通过现有秘密管理器提供。不要把 key 放进 `config.yaml`、源代码库或命令行参数。
3. 运行 `hermes model`，选择 **Ollama Cloud**，再选择模型。若直接配置：

   ```yaml
   model:
     provider: "ollama-cloud"
     default: "<Ollama Cloud 返回的模型 ID>"
   ```

请使用 Ollama 返回的确切 API 模型 ID（例如 `GET https://ollama.com/api/tags` 的结果），不要猜测 GLM 变体，也不要根据应用或 CLI 名称自行增删 `:cloud` 后缀。Ares/Hermes 的模型选择器元数据可能滞后于账号可用性，因此仅在选择器中看到某个 ID 并不能证明该账号已启用它。

## 路由行为

- 在 `hermes model`、`hermes chat --provider ollama-cloud` 或 `config.yaml` 的 `model.provider` 中显式选择 `ollama-cloud`。
- 若要保持现有 OpenAI 订阅路由为主路由，请保留其显式 `model.provider` 配置。使用 Ollama Cloud 表示有意切换到独立的 `OLLAMA_API_KEY` 路由；它不会复用 OpenAI 订阅认证。
- `ollama-cloud` 与本地 Ollama 使用不同的提供商身份。本地 Ollama 通过 Custom Endpoint 路由，常见地址为 `http://localhost:11434/v1`。
- 此集成不会添加到本地 Ollama、OpenAI 或其他账号路由的自动回退。缺少/无效的 Ollama 凭据和提供商错误会作为所选路由的错误返回。现有 `auto` 提供商解析规则及另行配置的 `fallback_providers` 策略保持不变。
- Ollama Cloud 是托管推理；这不表示相同模型 ID 或模型尺寸也能在本机运行。

请参阅 Ollama 的 [Cloud API 指南](https://docs.ollama.com/cloud)、[Chat API](https://docs.ollama.com/api/chat) 和 [OpenAI 兼容性说明](https://docs.ollama.com/api/openai-compatibility)了解当前 API 语义。

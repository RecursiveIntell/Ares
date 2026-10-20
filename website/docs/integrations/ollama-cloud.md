---
title: Ollama Cloud
description: Configure Ollama Cloud as an explicit API-key provider.
---

# Ollama Cloud

Ares/Hermes supports Ollama Cloud as the explicit `ollama-cloud` provider. By default, it sends requests to `https://ollama.com/v1` through the OpenAI-compatible Chat Completions API and authenticates with `OLLAMA_API_KEY`. No local Ollama installation is required for this route.

The direct Cloud API key route is separate from signing in to the Ollama app or CLI. It does not read local Ollama sign-in state or copy credentials from another provider.

## Connect an account when ready

1. Create an API key in [Ollama's key settings](https://ollama.com/settings/keys). Ollama's [Cloud guide](https://docs.ollama.com/cloud) documents the API key flow.
2. Store it as `OLLAMA_API_KEY` in the active profile's secrets-only `.env` file under that profile's `HERMES_HOME`, or provide it through your existing secret manager. Never put the key in `config.yaml`, source control, or a command-line argument.
3. Run `hermes model`, choose **Ollama Cloud**, and select a model. This saves `ollama-cloud` as the provider. To configure it directly:

   ```yaml
   model:
     provider: "ollama-cloud"
     default: "<model ID returned by Ollama Cloud>"
   ```

Ollama's [Cloud model list](https://docs.ollama.com/cloud#models) explains the model naming rules. Use the exact API model ID Ollama returns (for example, from `GET https://ollama.com/api/tags`); do not guess a GLM variant or add/remove a `:cloud` suffix based on an app or CLI model name. Ares/Hermes model-picker metadata can lag account availability, so the picker alone is not proof that an ID is enabled for your account.

## Route behavior

- Select `ollama-cloud` explicitly in `hermes model`, `hermes chat --provider ollama-cloud`, or `model.provider` in `config.yaml`.
- To keep the existing OpenAI subscription route primary, leave its explicit `model.provider` configuration in place. Using Ollama Cloud is a deliberate switch to its separate `OLLAMA_API_KEY` route; it does not reuse OpenAI subscription authentication.
- `ollama-cloud` keeps a separate provider identity from local Ollama. Local Ollama uses the Custom Endpoint route, commonly `http://localhost:11434/v1`.
- This integration does not add a fallback to local Ollama, OpenAI, or another account route. Missing/invalid Ollama credentials and provider errors remain errors for the selected route. Existing `auto` provider resolution and any separately configured `fallback_providers` policy remain unchanged.
- Ollama Cloud is hosted inference. It does not imply that the same model ID or model size can run locally.

For Ollama's current API semantics, see its [Cloud API guide](https://docs.ollama.com/cloud), [Chat API](https://docs.ollama.com/api/chat), and [OpenAI compatibility notes](https://docs.ollama.com/api/openai-compatibility).

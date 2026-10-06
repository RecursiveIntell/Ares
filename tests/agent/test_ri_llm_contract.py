"""The native transport must preserve or refuse the admitted request."""
import concurrent.futures
import math
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.transports import ri_llm as owner


class NativeConfig:
    def __init__(self, **values):
        self.values = values


class NativePipeline:
    calls = []

    def __init__(self, url, model, *, config):
        self.trace = dict(url=url, model=model, config=config.values)
        self.calls.append(self.trace)

    def call(self, prompt, *, system=None, config=None):
        self.trace.update(prompt=prompt, system=system)
        return "plain answer"

    def call_structured(self, *args, **kwargs):
        raise AssertionError("unsupported structured tool request reached native")


def agent(provider="ollama-launch", **overrides):
    values = dict(provider=provider, model="llama", api_mode="chat_completions",
                  api_key="no-key-required", base_url="http://localhost:11434/v1",
                  _interrupt_requested=False, platform="cron")
    values.update(overrides)
    return SimpleNamespace(**values)


def request(**overrides):
    values = dict(model="chosen-model", messages=[{"role":"user", "content":"raw user"}],
                  temperature=0, max_tokens=7)
    values.update(overrides)
    return values


class TransportContract(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.native = patch.multiple(owner, _NATIVE_AVAILABLE=True,
                                     _NativePipeline=NativePipeline, LlmConfig=NativeConfig)
        self.native.start()
        NativePipeline.calls = []

    def tearDown(self):
        self.native.stop()
        self.environment.stop()

    def qualify(self, item, config):
        owner.configure_ri_pipeline(item, config)
        return item

    def test_supported_ollama_preserves_parameters_content_and_unknown_usage(self):
        item = agent()
        payload = request(messages=[{"role":"system", "content":"exact system"},
                                    {"role":"user", "content":"raw user"}])
        result = owner.ri_pipeline_chat_completion(item, payload)
        self.assertEqual(NativePipeline.calls, [dict(url=item.base_url, model="chosen-model",
            config=dict(temperature=0, max_tokens=7, thinking=False, json_mode=False),
            prompt="raw user", system="exact system")])
        self.assertIsNone(result.usage)
        self.assertEqual(result.model, "chosen-model")
        self.assertEqual(result.choices[0].message.content, "plain answer")
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertIsNone(result.choices[0].finish_reason)

    def test_configured_off_beats_available_native_and_env_whitelist(self):
        item = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":False}}})
        os.environ["HERMES_RI_PIPELINE_PROVIDERS"] = "ollama-launch"
        os.environ["HERMES_RI_PIPELINE"] = "1"
        self.assertFalse(owner._should_use_ri_pipeline(item, request()))
        self.assertEqual(NativePipeline.calls, [])

    def test_configured_whitelist_does_not_admit_another_provider(self):
        item = self.qualify(agent("openrouter"), {"agent":{"llm_pipeline":{"providers":["ollama-launch"]}}})
        self.assertFalse(owner._should_use_ri_pipeline(item, request()))

    def test_env_whitelist_precedes_config_and_selected_incompatible_refuses(self):
        item = self.qualify(agent("openrouter"), {"agent":{"llm_pipeline":{"providers":["ollama-launch"]}}})
        os.environ["HERMES_RI_PIPELINE_PROVIDERS"] = "openrouter"
        self.assertTrue(owner._should_use_ri_pipeline(item, request()))
        with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
            owner.ri_pipeline_chat_completion(item, request())
        self.assertEqual(NativePipeline.calls, [])

    def test_env_off_overrides_explicit_selection(self):
        item = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
        os.environ["HERMES_RI_PIPELINE"] = "0"
        self.assertFalse(owner._should_use_ri_pipeline(item, request()))

    def test_default_unknown_provider_retains_sdk_route(self):
        for provider in ("openrouter", "openai", "unknown-provider", ""):
            with self.subTest(provider=provider):
                item = self.qualify(agent(provider), {})
                self.assertFalse(owner._should_use_ri_pipeline(item, request()))

    def test_native_unavailable_retains_sdk_even_when_explicit(self):
        item = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
        with patch.object(owner, "_NATIVE_AVAILABLE", False):
            self.assertFalse(owner._should_use_ri_pipeline(item, request()))

    def test_default_unsupported_shapes_retain_sdk(self):
        item = self.qualify(agent(), {})
        for payload in self.unsupported():
            with self.subTest(payload=payload):
                self.assertFalse(owner._should_use_ri_pipeline(item, payload))

    @staticmethod
    def unsupported():
        return [
            request(tools=[{"type":"function", "function":{"name":"search"}}], tool_choice="none"),
            request(messages=[{"role":"user", "content":[{"type":"text", "text":"hi"},
                               {"type":"image_url", "image_url":{"url":"https://inert.invalid"}}]}]),
            request(messages=[{"role":"developer", "content":"policy"}, {"role":"user", "content":"hi"}]),
            request(messages=[{"role":"user", "content":"first"}, {"role":"assistant", "content":"prior"},
                               {"role":"user", "content":"next"}]),
            request(messages=[{"role":"assistant", "content":None, "tool_calls":[]},
                               {"role":"tool", "tool_call_id":"old", "content":"result"}]),
            request(timeout=10), request(extra_headers={"X-Test":"yes"}),
            request(extra_body={"thinking":True}), request(response_format={"type":"json_object"}),
            request(messages=[{"role":"system", "content":""},{"role":"user", "content":"hi"}]),
            request(messages=[{"role":"user", "content":"explain literal {input}"}]),
        ]

    def test_explicit_unsupported_shapes_refuse_before_native_effects(self):
        item = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
        for payload in self.unsupported():
            with self.subTest(payload=payload):
                self.assertTrue(owner._should_use_ri_pipeline(item, payload))
                with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
                    owner.ri_pipeline_chat_completion(item, payload)
        self.assertEqual(NativePipeline.calls, [])

    def test_authenticated_and_callable_credentials_refuse_without_evaluation(self):
        def forbidden_key():
            self.fail("credential callable was evaluated")
        for key in ("SYNTHETIC-KEY", forbidden_key):
            with self.subTest(key=type(key).__name__):
                item = self.qualify(agent(api_key=key), {"agent":{"llm_pipeline":{"enabled":True}}})
                with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
                    owner.ri_pipeline_chat_completion(item, request())
        self.assertEqual(NativePipeline.calls, [])

    def test_invalid_generation_settings_and_endpoint_refuse(self):
        for payload in (request(max_tokens=0), request(max_tokens=True), request(max_tokens=1.5),
                        request(temperature=math.nan), request(temperature=True), request(stream=1)):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
                    owner.ri_pipeline_chat_completion(agent(), payload)
        for url in ("https://inert.invalid/custom", "https://user:synthetic@inert.invalid/v1",
                    "https://inert.invalid/v1?key=synthetic", "http://localhost:bad/v1", "not-a-url"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
                    owner.ri_pipeline_chat_completion(agent(base_url=url), request())
        self.assertEqual(NativePipeline.calls, [])

    def test_concurrent_supported_calls_leave_process_credentials_untouched(self):
        os.environ["OPENAI_API_KEY"] = "SYNTHETIC-SENTINEL"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: owner.ri_pipeline_chat_completion(agent(), request()), range(2)))
        self.assertEqual(os.environ["OPENAI_API_KEY"], "SYNTHETIC-SENTINEL")
        self.assertTrue(all(item.usage is None for item in results))

    def test_actual_downstream_usage_normalizer_retains_missing_raw_usage(self):
        from agent.usage_pricing import normalize_usage
        response = owner.ri_pipeline_chat_completion(agent(), request())
        usage = normalize_usage(response.usage, provider="ollama-launch", api_mode="chat_completions")
        self.assertIsNone(usage.raw_usage)
        self.assertEqual(usage.prompt_tokens, 0)
        self.assertFalse(bool(response.usage))

    def test_native_unknown_finish_survives_actual_normalizer_and_message_builder(self):
        from agent.transports.chat_completions import ChatCompletionsTransport
        from agent import chat_completion_helpers as host
        item = agent()
        response = owner.ri_pipeline_chat_completion(item, request())
        normalized = ChatCompletionsTransport().normalize_response(response)
        self.assertIsNone(normalized.finish_reason)
        self.assertIsNone(normalized.usage)
        item._extract_reasoning = lambda *_:None
        item._strip_think_blocks = lambda text:text
        # Content redaction is outside this metadata regression; no secret read.
        redactor = SimpleNamespace(redact_sensitive_text=lambda text:text)
        with patch.dict(sys.modules, {"agent.redact":redactor}):
            persisted = host.build_assistant_message(item, normalized, normalized.finish_reason)
        self.assertIsNone(persisted["finish_reason"])
        self.assertEqual(persisted["content"], "plain answer")

    def test_sdk_missing_finish_and_text_marker_keep_legacy_stop_default(self):
        from agent.transports.chat_completions import ChatCompletionsTransport
        for text in ("plain answer", "RiCompletionResponse unknown native finish"):
            response = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=None), finish_reason=None)],
                usage=None, native=True, _ri_finish_unknown=True)
            normalized = ChatCompletionsTransport().normalize_response(response)
            self.assertEqual(normalized.finish_reason, "stop")
            self.assertIsNone(normalized.usage)

    def test_omitted_generation_settings_keep_sdk_or_refuse_explicit_native(self):
        for key in ("temperature", "max_tokens"):
            payload = request()
            del payload[key]
            ordinary = self.qualify(agent(), {})
            self.assertFalse(owner._should_use_ri_pipeline(ordinary, payload))
            selected = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
            self.assertTrue(owner._should_use_ri_pipeline(selected, payload))
            with self.assertRaises(owner.RiTransportUnsupported):
                owner.ri_pipeline_chat_completion(selected, payload)
        self.assertEqual(NativePipeline.calls, [])

    def test_canonical_client_overrides_keep_sdk_or_refuse_before_native(self):
        for options in ({"default_headers":{"X-Inert":"required"}},
                        {"http_client":object()}, {"timeout":120},
                        {"default_query":{"api-version":"synthetic"}},
                        {"api_key":"SYNTHETIC-OVERRIDE"},
                        {"base_url":"http://localhost:11435/v1"}):
            with self.subTest(option=next(iter(options))):
                ordinary = self.qualify(agent(_client_kwargs=options), {})
                self.assertFalse(owner._should_use_ri_pipeline(ordinary, request()))
                selected = self.qualify(agent(_client_kwargs=options),
                    {"agent":{"llm_pipeline":{"enabled":True}}})
                with self.assertRaises(owner.RiTransportUnsupported):
                    owner.ri_pipeline_chat_completion(selected, request())
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_dispatch_keeps_omissions_and_client_options_on_sdk(self):
        from agent import chat_completion_helpers as host
        item = self.qualify(agent(_client_kwargs={"default_headers":{"X-Inert":"required"}}), {})
        payload = request()
        del payload["max_tokens"]
        sdk_calls = []
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs:sdk_calls.append(kwargs) or "sdk-response")))
        self.assertEqual(host._dispatch_nonstreaming_api_request(item, payload,
            make_client=lambda *_:sdk), "sdk-response")
        self.assertEqual(sdk_calls, [payload])
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(item._client_kwargs, {"default_headers":{"X-Inert":"required"}})
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_host_dispatch_retains_sdk_and_refuses_explicit_selection(self):
        from agent import chat_completion_helpers as host
        payload = request(tools=[])
        sdk_calls = []
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: sdk_calls.append(kwargs) or "sdk-response")))
        ordinary = self.qualify(agent("openrouter"), {})
        self.assertEqual(host._dispatch_nonstreaming_api_request(ordinary, payload, make_client=lambda *_:sdk), "sdk-response")
        self.assertEqual(sdk_calls, [payload])
        selected = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
        with self.assertRaisesRegex(ValueError, "RI_PIPELINE_REQUEST_UNSUPPORTED"):
            host._dispatch_nonstreaming_api_request(selected, payload, make_client=lambda *_:self.fail("SDK fallback"))
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_host_nonstream_and_cron_stream_use_supported_native(self):
        from agent import chat_completion_helpers as host
        item = self.qualify(agent(), {})
        item._interruptible_api_call = lambda kwargs: host._dispatch_nonstreaming_api_request(
            item, kwargs, make_client=lambda *_:self.fail("unexpected SDK dispatch"))
        response = host.interruptible_streaming_api_call(item, request(stream=True))
        self.assertEqual(response.choices[0].message.content, "plain answer")
        self.assertIsNone(response.usage)

    def moa_receiver(self, config):
        trace = []
        response = SimpleNamespace(id="moa-sdk-response")

        def prepare(**kwargs):
            trace.append(("prepare", kwargs))

        def create(**kwargs):
            prepare(**kwargs)
            trace.append(("create", kwargs))
            return response

        item = self.qualify(agent("moa"), config)
        item.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(prepare=prepare, create=create)))
        return item, trace, response

    def test_actual_host_selected_moa_refuses_before_prepare_or_create(self):
        from agent import chat_completion_helpers as host
        for config in ({"agent":{"llm_pipeline":{"enabled":True}}},
                       {"agent":{"llm_pipeline":{"providers":["moa"]}}}):
            for streamed in (False, True):
                with self.subTest(config=config, streamed=streamed):
                    item, trace, _ = self.moa_receiver(config)
                    with self.assertRaises(owner.RiTransportUnsupported):
                        host._dispatch_nonstreaming_api_request(item, request(stream=streamed),
                            make_client=lambda *_:self.fail("unexpected client construction"))
                    self.assertEqual(trace, [])
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_host_default_moa_preserves_facade_prepare_and_create(self):
        from agent import chat_completion_helpers as host
        item, trace, response = self.moa_receiver({})
        payload = request(_moa_prepared_request="inert-prepared")
        result = host._dispatch_nonstreaming_api_request(item, payload,
            make_client=lambda *_:self.fail("MoA facade was replaced"))
        self.assertIs(result, response)
        self.assertEqual(trace, [("prepare",payload), ("create",payload)])
        self.assertEqual(payload["_moa_prepared_request"], "inert-prepared")
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_host_selected_moa_cron_stream_refuses_before_effects(self):
        from agent import chat_completion_helpers as host
        item, trace, _ = self.moa_receiver({"agent":{"llm_pipeline":{"enabled":True}}})
        item._interruptible_api_call = lambda kwargs: host._dispatch_nonstreaming_api_request(
            item, kwargs, make_client=lambda *_:self.fail("unexpected client construction"))
        with self.assertRaises(owner.RiTransportUnsupported):
            host.interruptible_streaming_api_call(item, request(stream=True))
        self.assertEqual(trace, [])
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_host_default_moa_cron_stream_keeps_facade_route(self):
        from agent import chat_completion_helpers as host
        item, trace, response = self.moa_receiver({})
        item._interruptible_api_call = lambda kwargs: host._dispatch_nonstreaming_api_request(
            item, kwargs, make_client=lambda *_:self.fail("MoA facade was replaced"))
        payload = request(stream=True)
        # MoA keeps its established worker; exercise its canonical dispatch seam
        # without launching the real interrupt/stream worker in this offline test.
        self.assertFalse(host.should_use_direct_api_call(item))
        self.assertFalse(owner._should_use_ri_pipeline(item, payload))
        result = host._dispatch_nonstreaming_api_request(item, payload,
            make_client=lambda *_:self.fail("MoA facade was replaced"))
        self.assertIs(result, response)
        self.assertEqual(trace, [("prepare",payload), ("create",payload)])
        self.assertEqual(NativePipeline.calls, [])

    def test_actual_error_classifier_does_not_offer_retry_for_typed_refusal(self):
        from agent.error_classifier import classify_api_error
        item = self.qualify(agent(), {"agent":{"llm_pipeline":{"enabled":True}}})
        try:
            owner.ri_pipeline_chat_completion(item, request(tools=[]))
        except ValueError as error:
            verdict = classify_api_error(error, provider="ollama-launch")
            self.assertFalse(verdict.retryable)
            self.assertFalse(verdict.should_fallback)
            self.assertFalse(verdict.should_rotate_credential)
            self.assertFalse(verdict.should_compress)
            self.assertTrue(verdict.error_context.get("native_transport_refusal"))
        else:
            self.fail("unsupported request did not refuse")

    def test_provider_text_and_unknown_errors_keep_existing_retry_behavior(self):
        from agent.error_classifier import classify_api_error
        for error in (RuntimeError("RI_PIPELINE_REQUEST_UNSUPPORTED"),
                      ValueError("request fields unavailable in native binding")):
            with self.subTest(error=type(error).__name__):
                verdict = classify_api_error(error, provider="ollama-launch")
                self.assertTrue(verdict.retryable)
                self.assertFalse(verdict.error_context.get("native_transport_refusal"))

    def test_plugin_cannot_attach_native_refusal_marker_to_provider_error(self):
        from agent.error_classifier import classify_api_error, FailoverReason
        plugin = SimpleNamespace(get_plugin_error_classification=lambda **kwargs: {
            "reason":FailoverReason.unknown, "retryable":True,
            "error_context":{"native_transport_refusal":True, "plugin_note":"retained"},
        })
        with patch.dict(sys.modules, {"hermes_cli.plugins":plugin}):
            verdict = classify_api_error(RuntimeError("RI_PIPELINE_REQUEST_UNSUPPORTED"))
        self.assertTrue(verdict.retryable)
        self.assertEqual(verdict.error_context, {"plugin_note":"retained"})


if __name__ == "__main__":
    unittest.main()

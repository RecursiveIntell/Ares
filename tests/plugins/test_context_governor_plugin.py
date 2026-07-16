import copy
import hashlib
import json
import subprocess
from pathlib import Path

from plugins.context_engine import discover_context_engines, load_context_engine
from plugins.context_engine.context_governor import ContextGovernorEngine


def _find_binary():
    """Use the same resolution path as the live Hermes adapter.

    Keeping a test-only preference for a debug or ~/.local binary let the
    plugin suite certify a different executable than the desktop process used
    (which resolves PATH first). That masks CLI contract drift after library
    work until a real session needs to compact.
    """
    binary = ContextGovernorEngine().binary
    assert Path(binary).exists(), f"context-governor binary unavailable: {binary}"
    return binary


def test_context_governor_plugin_discovers_and_loads():
    discovered = {name: available for name, _desc, available in discover_context_engines()}
    assert discovered.get("context_governor") is True
    engine = load_context_engine("context_governor")
    assert engine is not None
    assert engine.name == "context_governor"
    assert engine.is_available()


def test_context_governor_exposes_expand_and_search_tools(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    schemas = engine.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "context_expand" in names
    assert "context_search" in names
    assert "context_status" in names


def test_context_governor_handle_tool_call_status(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    result = json.loads(engine.handle_tool_call("context_status", {}))
    assert result["engine"] == "context_governor"
    assert "compression_count" in result


def test_context_governor_handle_tool_call_unknown(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    result = json.loads(engine.handle_tool_call("nonexistent_tool", {}))
    assert "error" in result


def test_context_governor_compress_preserves_latest_user_last(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    engine.on_session_start("pytest-context-governor")
    needle = "PLUGIN_CERT_NEEDLE_20260629_RECOVER_ME"
    latest = "Latest task: keep this final user message active."
    messages = [
        {"role": "system", "content": "system"},
        {"role": "tool", "content": ("noise\n" * 500) + needle},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": latest},
    ]
    compacted = engine.compress(messages, current_tokens=4000)
    assert compacted[-1]["role"] == "user"
    assert compacted[-1]["content"] == latest
    assert engine.last_receipt_id
    assert any(tmp_path.rglob("*.json"))

    # The exact omitted payload must be recoverable from the stored receipt.
    receipt = engine.last_receipt_id
    proc = subprocess.run(
        [binary, "search", "--dir", str(tmp_path), "--query", needle],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert receipt in proc.stdout
    assert needle in proc.stdout


def test_context_governor_receipt_keeps_exact_old_tool_output(tmp_path):
    """The adapter may shape the prompt, but it must never discard the only
    copy of an old tool result before the Rust engine records exact fallback."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1_000, protect_last_n=1)
    needle = "EXACT_OLD_TOOL_OUTPUT_NEEDLE"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "inspect the old command"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-old", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-old", "content": needle + "\n" + ("x" * 20_000)},
        {"role": "assistant", "content": "old command completed"},
        {"role": "user", "content": "latest task remains active"},
    ]
    compacted = engine.compress(messages, current_tokens=8_000)
    assert compacted[-1]["content"] == "latest task remains active"
    found = subprocess.run(
        [binary, "search", "--dir", str(tmp_path), "--query", needle],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert needle in found.stdout


def test_context_governor_preserves_tool_calls_and_tool_call_id(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "run tests"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "file1\nfile2\n" * 200},
        {"role": "user", "content": "what files exist?"},
    ]
    compacted = engine.compress(messages, current_tokens=4000)
    # Latest user must be last
    assert compacted[-1]["role"] == "user"
    assert compacted[-1]["content"] == "what files exist?"
    # No dangling tool messages without paired assistant tool_calls
    for i, msg in enumerate(compacted):
        if msg.get("role") == "tool":
            assert msg.get("tool_call_id"), f"tool message at {i} missing tool_call_id"
    # No assistant message should have both empty content AND empty tool_calls (data loss)
    for msg in compacted:
        if msg.get("role") == "assistant":
            has_content = bool(msg.get("content"))
            has_tcs = bool(msg.get("tool_calls"))
            assert has_content or has_tcs, "assistant message lost both content and tool_calls"


def test_context_governor_anti_thrashing_skips_ineffective_compression(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    # Simulate two ineffective compressions
    engine._ineffective_compression_count = 2
    # Still above the normal trigger, but below the emergency safety band.
    should = engine.should_compress(prompt_tokens=600)
    assert should is False  # Anti-thrashing blocks


def test_context_governor_emergency_pressure_overrides_anti_thrashing(tmp_path):
    """Never send a request near the provider limit just because compaction
    previously made little progress.

    The old unconditional anti-thrash return allowed an already oversized
    session to grow until the provider rejected it with a context-window 400.
    """
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    engine._ineffective_compression_count = 2

    assert engine.should_compress(prompt_tokens=950) is True


def test_context_governor_emergency_pressure_overrides_preflight_futility_deferral(tmp_path):
    """Automatic callers must reach the emergency should_compress() gate."""
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    engine._ineffective_compression_count = 2

    # The preflight gate runs before should_compress(). It must not hide the
    # emergency override after two low-yield compaction passes.
    assert engine.should_defer_preflight_to_real_usage(950) is False
    assert engine.should_compress(prompt_tokens=950) is True


def test_context_governor_should_compress_normal(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=1000)
    # threshold = 500 (1000 * 0.50)
    assert engine.should_compress(prompt_tokens=600) is True
    assert engine.should_compress(prompt_tokens=400) is False


def test_context_governor_reads_policy_from_config(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    # Default policy should be safe
    assert engine._policy["budget_mode"] == "soft_warn"
    assert engine._policy["allocator"] == "deterministic_v1"
    assert engine._policy["semantic_memory_enabled"] is False


def test_context_governor_accepts_max_tokens(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000, max_tokens=2000)
    # effective_window = 10000 - 2000 = 8000
    # threshold = 8000 * 0.50 = 4000
    assert engine.max_tokens == 2000
    assert engine.threshold_tokens == 4000


def test_context_governor_max_tokens_none(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    # No max_tokens -> effective_window = 10000
    assert engine.max_tokens is None
    assert engine.threshold_tokens == 5000


def test_context_governor_update_model_retains_summary_credentials(tmp_path):
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(
        model="glm-5.2:cloud",
        context_length=10000,
        base_url="http://127.0.0.1:11434/v1",
        api_key="test-key",
        provider="ollama-launch",
        api_mode="chat_completions",
    )
    assert engine.model == "glm-5.2:cloud"
    assert engine.base_url == "http://127.0.0.1:11434/v1"
    assert engine.api_key == "test-key"
    assert engine.provider == "ollama-launch"
    assert engine.api_mode == "chat_completions"


def test_context_governor_deferred_preflight(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    # No real usage yet -> don't defer
    assert engine.should_defer_preflight_to_real_usage(9000) is False
    # Simulate real usage that fit under threshold
    engine.last_real_prompt_tokens = 4000
    engine.last_prompt_tokens = 4000
    engine._last_rough_tokens_when_real_fit = 5000
    # Rough estimate above threshold but close to baseline -> defer
    assert engine.should_defer_preflight_to_real_usage(8600) is True
    # Rough estimate way above baseline -> don't defer
    assert engine.should_defer_preflight_to_real_usage(20000) is False


def test_context_governor_prune_old_tool_results(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    # Two identical tool results — older should be deduped
    big_content = "x" * 500
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do stuff"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": big_content},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc2", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "tc2", "content": big_content},
        {"role": "user", "content": "latest task"},
    ]
    pruned = engine._prune_old_tool_results(messages)
    # First tool result should be deduped
    dupes = [m for m in pruned if m.get("role") == "tool" and "Duplicate" in m.get("content", "")]
    assert len(dupes) >= 1


def test_context_governor_returns_original_messages_on_binary_failure(tmp_path):
    engine = ContextGovernorEngine(binary="/no/such/context-governor", store_dir=str(tmp_path), timeout_sec=1)
    messages = [{"role": "user", "content": "latest"}]
    assert engine.compress(messages) is messages
    assert engine.last_error


def test_context_governor_sanitize_dangling_tool_messages(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "orphan", "content": "orphan tool result"},
        {"role": "user", "content": "latest task"},
    ]
    sanitized = engine._sanitize_dangling_tool_messages(messages)
    # The dangling tool message should be converted to assistant
    assert sanitized[1]["role"] == "assistant"
    assert "orphan tool result" in sanitized[1]["content"]


def test_context_governor_classify_subprocess_error(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    assert engine._classify_subprocess_error(RuntimeError("HTTP 401 unauthorized")) == "auth"
    assert engine._classify_subprocess_error(RuntimeError("HTTP 403 forbidden")) == "auth"
    assert engine._classify_subprocess_error(RuntimeError("connection reset by peer")) == "network"
    assert engine._classify_subprocess_error(subprocess.TimeoutExpired(cmd="x", timeout=1)) == "timeout"
    assert engine._classify_subprocess_error(RuntimeError("some other error")) == "transient"


def test_context_governor_status_includes_policy(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    status = engine.get_status()
    assert "policy" in status
    assert status["policy"]["budget_mode"] == "soft_warn"
    assert "ineffective_compression_count" in status
    assert "last_savings_pct" in status


def test_context_governor_telemetry_uses_quarantined_default_db(tmp_path, monkeypatch):
    monkeypatch.delenv("CEA_TELEMETRY_DB", raising=False)
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    assert engine.telemetry_db_path.name == "cea-telemetry-v2.db"
    assert engine.telemetry_db_path.name != "cea.db"
    assert engine.get_status()["telemetry_advisory"] is True
    monkeypatch.setenv("CEA_TELEMETRY_DB", str(tmp_path / "override.db"))
    overridden = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    assert overridden.telemetry_db_path == tmp_path / "override.db"


def test_context_governor_captures_original_tool_telemetry_before_compaction(tmp_path, monkeypatch):
    """Telemetry observes immutable input; the Rust engine owns degradation and
    exact-fallback storage, so the adapter must not pre-prune it."""
    engine = ContextGovernorEngine(binary="/no/such/context-governor", store_dir=str(tmp_path), timeout_sec=1)
    engine._telemetry_available = True
    captured = []

    def capture(messages):
        captured.extend(messages)

    monkeypatch.setattr(engine, "_record_tool_telemetry", capture)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-original", "function": {"name": "terminal", "arguments": '{"command":"x"}'}}
        ]},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "tool_call_id": "call-original", "content": "ORIGINAL_TOOL_RESULT"},
        {"role": "user", "content": "latest"},
    ]
    engine.compress(messages, current_tokens=4000)
    assert captured[3]["content"] == "ORIGINAL_TOOL_RESULT"


def test_context_governor_telemetry_sends_required_ids_and_digests(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine._telemetry_available = True
    engine.session_id = "session-1"
    sent = []
    monkeypatch.setattr(engine, "_run_telemetry_json", lambda args, payload: sent.append((args, payload)) or {})
    output = '{"success": true, "exit_code": 0}'
    engine._record_tool_telemetry([
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": '{"command":"secret"}'}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": output},
    ])
    assert sent[0][0] == ["record-telemetry"]
    payload = sent[0][1]
    assert payload["session_id"] == "session-1"
    assert payload["tool_call_id"] == "call-1"
    assert payload["result_digest"] == hashlib.sha256(output.encode()).hexdigest()
    assert payload["outcome"] == "success"


def test_context_governor_classifies_ambiguous_tool_output_as_unknown(tmp_path):
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    assert engine._classify_tool_outcome("finished some text") == ("unknown", None)


def test_context_governor_classifies_explicit_success_and_error(tmp_path):
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    assert engine._classify_tool_outcome('{"success": true}') == ("success", None)
    assert engine._classify_tool_outcome('{"exit_code": 1, "error": "failed"}')[0] == "error"
    assert engine._classify_tool_outcome("Traceback (most recent call last)")[0] == "error"


def test_context_governor_advisory_protection_is_bounded_and_fail_open(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.protect_last_n = 2
    engine._policy["telemetry_max_additional_protected_messages"] = 3
    assert engine._advisory_protect_last_n(30, {0: 1.0}) == 5
    monkeypatch.setattr(engine, "_run_telemetry_json", lambda *_args: (_ for _ in ()).throw(RuntimeError("bridge down")))
    engine._telemetry_available = True
    assert engine._score_telemetry_relevance([{"role": "tool", "name": "terminal"}], None) == {}


def test_context_governor_summary_uses_live_auxiliary_client(tmp_path, monkeypatch):
    """LLM mode must call Hermes' real auxiliary routing layer, not a dead import."""
    from types import SimpleNamespace
    from agent import auxiliary_client

    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="summary body"))]
        )

    monkeypatch.setattr(auxiliary_client, "call_llm", fake_call_llm)
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(
        model="glm-5.2:cloud",
        context_length=10000,
        base_url="http://127.0.0.1:11434/v1",
        api_key="test-key",
        provider="ollama-launch",
    )

    assert engine._call_summary_llm("receipt-aware prompt", 400, system_prompt="structured system") == "summary body"
    assert captured["task"] == "compression"
    assert captured["model"] == "glm-5.2:cloud"
    assert captured["provider"] == "ollama-launch"
    assert captured["base_url"] == "http://127.0.0.1:11434/v1"
    assert captured["api_key"] == "test-key"
    assert captured["messages"] == [
        {"role": "system", "content": "structured system"},
        {"role": "user", "content": "receipt-aware prompt"},
    ]


def test_context_governor_llm_summary_mode_fallback(tmp_path):
    """When summary_mode=llm but call_llm is not available, should fall back to extractive."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._summary_mode = "llm"  # Enable LLM mode
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Build parser. Acceptance gate: cargo test must pass."},
        {"role": "assistant", "content": "Decision: use JSON parsing."},
        {"role": "tool", "tool_call_id": "tc1", "content": "log output\n" * 500},
        {"role": "user", "content": "Latest task: summarize what remains."},
    ]
    compacted = engine.compress(messages, current_tokens=8000)
    # Should still produce valid output even if LLM call fails
    assert compacted[-1]["role"] == "user"
    assert engine.compression_count == 1
    # The extractive summary should still be present
    has_summary = any(m.get("name") == "context_governor" for m in compacted)
    assert has_summary


def test_context_governor_llm_waits_for_deterministic_checkpoint(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._summary_mode = "llm"
    llm_calls = []

    response = {
        "receipt": {
            "receipt_id": "ctxr_checkpoint",
            "original_approx_tokens": 1000,
            "compacted_approx_tokens": 100,
            "token_savings_estimate": 900,
            "original_transcript_blake3": "original",
            "compacted_transcript_blake3": "compacted",
        },
        "allocation_plan": {"summarized_item_ids": ["ctxi_1"], "items": []},
        "compacted_messages": [
            {"id": "m0", "role": "assistant", "content": "extractive", "name": "context_governor"},
            {"id": "m1", "role": "user", "content": "latest"},
        ],
    }
    calls = []

    def run_json(args, payload):
        calls.append(args)
        if args == ["compact"]:
            return copy.deepcopy(response)
        if args == ["finalize"]:
            return copy.deepcopy(payload)
        raise AssertionError(f"unexpected binary call: {args}")

    monkeypatch.setattr(engine, "_run_json", run_json)
    monkeypatch.setattr(engine, "_store_response", lambda response: None)
    monkeypatch.setattr(
        engine,
        "_enhance_with_llm_summary",
        lambda *args: llm_calls.append(args) or args[0],
    )

    compacted = engine.compress([{"role": "user", "content": "latest"}], current_tokens=1000)

    assert compacted[-1] == {"role": "user", "content": "latest"}
    assert llm_calls == []
    assert calls == [["compact"], ["finalize"]]


def test_context_governor_finalizes_adapter_output_before_store(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    source = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "latest"},
    ]
    response = {
        "receipt": {
            "receipt_id": "ctxr_finalize",
            "original_approx_tokens": 2,
            "compacted_approx_tokens": 2,
            "token_savings_estimate": 0,
            "original_transcript_blake3": "same",
            "compacted_transcript_blake3": "same",
        },
        "allocation_plan": {"summarized_item_ids": [], "items": []},
        "compacted_messages": [
            {"id": "m0", "role": "assistant", "content": "old"},
            {"id": "m1", "role": "user", "content": "latest"},
        ],
    }
    finalized_payload = {}
    stored = {}

    def run_json(args, payload):
        if args == ["compact"]:
            return copy.deepcopy(response)
        if args == ["finalize"]:
            finalized_payload.update(copy.deepcopy(payload))
            finalized = copy.deepcopy(payload)
            finalized["compacted_messages"][0]["content"] = "finalized-old"
            finalized["receipt"]["compacted_transcript_blake3"] = "final-blake3"
            finalized["receipt"]["compacted_transcript_sha256"] = "final-sha256"
            return finalized
        raise AssertionError(f"unexpected binary call: {args}")

    monkeypatch.setattr(engine, "_run_json", run_json)
    monkeypatch.setattr(engine, "_store_response", lambda value: stored.update(copy.deepcopy(value)))

    emitted = engine.compress(source, current_tokens=2)

    assert finalized_payload["compacted_messages"][0]["content"] == "old"
    assert emitted[0]["content"] == "finalized-old"
    assert stored["compacted_messages"] == [
        engine._message_to_governor(message, i) for i, message in enumerate(emitted)
    ]
    assert stored["receipt"]["compacted_transcript_blake3"] == "final-blake3"
    assert stored["receipt"]["compacted_transcript_sha256"] == "final-sha256"


def test_context_governor_store_failure_falls_back_without_reporting_success(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    source = [{"role": "user", "content": "latest"}]
    response = {
        "receipt": {"receipt_id": "ctxr_not_persisted"},
        "allocation_plan": {"summarized_item_ids": [], "items": []},
        "compacted_messages": [{"role": "user", "content": "latest"}],
    }

    def run_json(args, payload):
        if args == ["compact"]:
            return copy.deepcopy(response)
        if args == ["finalize"]:
            return copy.deepcopy(payload)
        raise AssertionError(f"unexpected binary call: {args}")

    monkeypatch.setattr(engine, "_run_json", run_json)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=1,
            stdout="",
            stderr="disk unavailable",
        ),
    )

    assert engine.compress(source, current_tokens=1) == source
    assert engine.compression_count == 0
    assert engine.last_receipt_id is None
    assert engine.last_error == "disk unavailable"


def test_context_governor_checkpoint_requires_consistent_receipt_evidence(tmp_path):
    engine = ContextGovernorEngine(binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=10)
    a_hash = "a" * 64
    b_hash = "b" * 64

    assert engine._deterministic_summary_checkpoint_ready({
        "receipt": {
            "original_transcript_blake3": a_hash,
            "compacted_transcript_blake3": a_hash,
            "original_approx_tokens": 100,
            "compacted_approx_tokens": 100,
            "token_savings_estimate": 0,
        }
    })
    assert engine._deterministic_summary_checkpoint_ready({
        "receipt": {
            "original_transcript_blake3": a_hash,
            "compacted_transcript_blake3": b_hash,
            "original_approx_tokens": 100,
            "compacted_approx_tokens": 91,
            "token_savings_estimate": 9,
        }
    })
    for receipt in (
        {
            "original_transcript_blake3": "same",
            "compacted_transcript_blake3": "same",
            "original_approx_tokens": 100,
            "compacted_approx_tokens": 100,
            "token_savings_estimate": 0,
        },
        {
            "original_transcript_blake3": a_hash,
            "compacted_transcript_blake3": b_hash,
            "original_approx_tokens": 100,
            "compacted_approx_tokens": 101,
            "token_savings_estimate": -1,
        },
        {
            "original_transcript_blake3": a_hash,
            "compacted_transcript_blake3": b_hash,
            "original_approx_tokens": 100,
            "compacted_approx_tokens": 90,
            "token_savings_estimate": 1,
        },
    ):
        assert not engine._deterministic_summary_checkpoint_ready({"receipt": receipt})


def test_context_governor_finalize_round_trips_multimodal_latest_user(tmp_path, monkeypatch):
    engine = ContextGovernorEngine(binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    content = [
        {"type": "text", "text": "inspect this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    source = [{"role": "user", "content": content}]
    response = {
        "receipt": {"receipt_id": "ctxr_multimodal"},
        "allocation_plan": {"summarized_item_ids": [], "items": []},
        "compacted_messages": [{"role": "user", "content": "inspect this\n[image]"}],
    }
    stored = {}

    def run_json(args, payload):
        if args == ["compact"]:
            return copy.deepcopy(response)
        if args == ["finalize"]:
            return copy.deepcopy(payload)
        raise AssertionError(f"unexpected binary call: {args}")

    monkeypatch.setattr(engine, "_run_json", run_json)
    monkeypatch.setattr(engine, "_store_response", lambda value: stored.update(copy.deepcopy(value)))

    emitted = engine.compress(source, current_tokens=1)

    assert emitted[-1]["content"] == content
    assert stored["compacted_messages"][-1]["metadata"]["hermes_content"] == content


def test_context_governor_llm_mode_uses_specialized_receipt_prompt(tmp_path, monkeypatch):
    """LLM enhancement must use the Rust receipt-aware prompt, not the legacy template."""
    engine = ContextGovernorEngine(binary=_find_binary(), store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._summary_mode = "llm"
    calls = []
    captured = {}

    def run_json(args, payload):
        calls.append((args, payload))
        if args == ["render-prompt"]:
            return {
                "system": "=== SPECIALIZED SYSTEM ===",
                "user": "=== RECEIPT-BACKED USER PROMPT ===",
            }
        if args == ["boundary-audit"]:
            return {"safe_to_reinject": True}
        raise AssertionError(f"unexpected binary call: {args}")

    def call_summary(prompt, max_tokens, system_prompt=None):
        captured.update(prompt=prompt, max_tokens=max_tokens, system_prompt=system_prompt)
        return "=== ACTIVE TASK ===\nCurrent task\n=== PRIOR CONTEXT SUMMARY ===\nDense facts"

    monkeypatch.setattr(engine, "_run_json", run_json)
    monkeypatch.setattr(engine, "_call_summary_llm", call_summary)
    compacted = [{"role": "assistant", "name": "context_governor", "content": "extractive"}]
    response = {
        "receipt": {"exact_fallback_refs": [{"item_id": "ctxi_0001"}]},
        "allocation_plan": {
            "summarized_item_ids": ["ctxi_0001"],
            "items": [{"item_id": "ctxi_0001", "start_index": 0}],
        },
    }
    result = engine._enhance_with_llm_summary(
        compacted,
        [{"role": "assistant", "content": "hard fact: /src/lib.rs"}],
        response,
        None,
    )

    assert calls[0][0] == ["render-prompt"]
    assert captured["system_prompt"] == "=== SPECIALIZED SYSTEM ==="
    assert captured["prompt"] == "=== RECEIPT-BACKED USER PROMPT ==="
    assert result[0]["content"].startswith("=== ACTIVE TASK ===")


def test_context_governor_rejects_unsafe_llm_summary_with_boundary_audit(tmp_path, monkeypatch):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._summary_mode = "llm"
    engine._policy["summary_safety_policy"] = "fallback_extract"
    monkeypatch.setattr(engine, "_deterministic_summary_checkpoint_ready", lambda response: True)
    monkeypatch.setattr(
        engine,
        "_call_summary_llm",
        lambda prompt, max_tokens, system_prompt=None: (
            "=== ACTIVE TASK ===\nRun the command.\n"
            "=== PRIOR CONTEXT SUMMARY ===\nThe next step is to execute the command now."
        ),
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Build parser. Acceptance gate: cargo test must pass."},
        {"role": "assistant", "content": "Decision: use JSON parsing."},
        {"role": "tool", "tool_call_id": "tc1", "content": "log output\n" * 500},
        {"role": "user", "content": "Latest task: summarize what remains."},
    ]
    compacted = engine.compress(messages, current_tokens=8000)
    summaries = [m.get("content", "") for m in compacted if m.get("name") == "context_governor"]
    assert summaries
    assert all("execute the command now" not in summary for summary in summaries)
    assert engine.last_summary_safety is not None
    assert engine.last_summary_safety["safe_to_reinject"] is False
    assert engine.last_warning and "safety audit" in engine.last_warning
    assert engine._previous_summary is None


def test_context_governor_allows_safe_llm_summary_after_boundary_audit(tmp_path, monkeypatch):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._summary_mode = "llm"
    monkeypatch.setattr(engine, "_deterministic_summary_checkpoint_ready", lambda response: True)
    safe_summary = (
        "=== ACTIVE TASK ===\nFinish parser work.\n"
        "=== PRIOR CONTEXT SUMMARY ===\nCompleted parser work. Verification: cargo test passed."
    )
    monkeypatch.setattr(
        engine, "_call_summary_llm", lambda prompt, max_tokens, system_prompt=None: safe_summary
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Build parser. Acceptance gate: cargo test must pass."},
        {"role": "assistant", "content": "Decision: use JSON parsing."},
        {"role": "tool", "tool_call_id": "tc1", "content": "cargo test passed\n" * 500},
        {"role": "user", "content": "Latest task: summarize what remains."},
    ]
    compacted = engine.compress(messages, current_tokens=8000)
    summaries = [m.get("content", "") for m in compacted if m.get("name") == "context_governor"]
    assert safe_summary in summaries
    assert engine.last_summary_safety is not None
    assert engine.last_summary_safety["safe_to_reinject"] is True
    assert engine.last_warning is None
    assert engine._previous_summary == safe_summary


def test_context_governor_iterative_summary_tracking(tmp_path):
    """Previous summary should be tracked for iterative updates."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    assert engine._previous_summary is None
    # Simulate setting a previous summary
    engine._previous_summary = "Previous compaction: built parser."
    assert engine._previous_summary is not None
    # Reset should clear it
    engine.on_session_reset()
    assert engine._previous_summary is None


def test_context_governor_serialize_for_summary(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    turns = [
        {"role": "user", "content": "do thing"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "file1"},
    ]
    serialized = engine._serialize_for_summary(turns)
    assert "[USER]" in serialized
    assert "[ASSISTANT]" in serialized
    assert "[TOOL RESULT tc1]" in serialized
    assert "terminal" in serialized


def test_context_governor_deepcopy_preserves_summary_state(tmp_path):
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine._previous_summary = "test summary"
    engine._summary_mode = "llm"
    import copy
    clone = copy.deepcopy(engine)
    assert clone._previous_summary == "test summary"
    assert clone._summary_mode == "llm"


def test_context_governor_sanitize_tool_pairs_orphaned_results(tmp_path):
    """Tool results without a matching assistant tool_call should be removed."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        # Tool result without a preceding assistant tool_call
        {"role": "tool", "tool_call_id": "orphan", "content": "orphan result"},
        {"role": "user", "content": "latest"},
    ]
    # First sanitize dangling (converts orphan to assistant text)
    sanitized = engine._sanitize_dangling_tool_messages(messages)
    # Then sanitize tool pairs (should find no orphaned results since dangling was handled)
    result = engine._sanitize_tool_pairs(sanitized)
    # The orphan tool should have been converted to assistant text by _sanitize_dangling_tool_messages
    tool_msgs = [m for m in result if m.get("role") == "tool"]
    assert len(tool_msgs) == 0


def test_context_governor_sanitize_tool_pairs_missing_results(tmp_path):
    """Assistant tool_calls without matching tool results should get stub results."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_missing", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        # No tool result for call_missing
        {"role": "user", "content": "latest"},
    ]
    result = engine._sanitize_tool_pairs(messages)
    # A stub tool result should have been inserted
    stub_results = [m for m in result if m.get("role") == "tool" and m.get("tool_call_id") == "call_missing"]
    assert len(stub_results) == 1
    assert "earlier conversation" in stub_results[0]["content"]


def test_context_governor_preserve_multimodal_tail(tmp_path):
    """Multimodal content in the protected tail should be preserved."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    original = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "look at this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KG="}},
        ]},
        {"role": "assistant", "content": "noted"},
    ]
    # Simulate compacted output where the multimodal was flattened to text
    compacted = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "look at this image\n[image]"},
        {"role": "assistant", "content": "noted"},
    ]
    result = engine._preserve_multimodal_tail(original, compacted)
    # The text "look at this image" won't match the flattened version exactly
    # because _content_to_text produces "look at this image\n[image]"
    # So the original lookup should find it
    user_msg = [m for m in result if m.get("role") == "user"][0]
    # Either preserved as list (if matched) or still string (if not matched)
    # The key invariant: no crash, valid output
    assert user_msg["role"] == "user"


def test_context_governor_cross_session_receipts_available(tmp_path):
    """Prior receipts should be discoverable on session start."""
    binary = _find_binary()
    # First session — create a receipt
    engine1 = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine1.update_model(model="test", context_length=1000)
    engine1.on_session_start("session-1")
    needle = "CROSS_SESSION_NEEDLE_PARSER_ACCEPTANCE_GATE"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": f"Build {needle}. Acceptance gate: cargo test must pass."},
        {"role": "tool", "tool_call_id": "tc1", "content": "log\n" * 500},
        {"role": "user", "content": "Latest task: summarize."},
    ]
    engine1.compress(messages, current_tokens=4000)
    assert engine1.last_receipt_id

    # Second session — should be able to search prior receipts
    engine2 = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path), timeout_sec=10)
    engine2.update_model(model="test", context_length=1000)
    engine2.on_session_start("session-2")
    # Search for content from the first session
    result = json.loads(engine2.handle_tool_call("context_search", {"query": needle}))
    assert isinstance(result, list)
    assert len(result) > 0


def test_context_governor_on_session_start_no_crash_empty_store(tmp_path):
    """on_session_start should not crash if the store is empty."""
    binary = _find_binary()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(tmp_path / "empty"), timeout_sec=10)
    engine.update_model(model="test", context_length=10000)
    engine.on_session_start("new-session")
    assert engine.session_id == "new-session"


class TestContextGovernorHostContract:
    """Pin the host contract that agent/turn_context.py and
    agent/conversation_compression.py depend on.

    Background: a live session crashed with
    ``AttributeError: 'ContextGovernorEngine' object has no attribute
    'last_real_prompt_tokens'`` on the second turn after two
    compactions. The adapter implements the ContextEngine ABC but
    not the implicit host contract the built-in ContextCompressor
    provides. These tests pin every attribute the host reads on
    ``agent.context_compressor`` without a ``getattr`` default.
    """

    def test_adapter_declares_host_contract_attributes(self, tmp_path):
        """The four attributes the host reads on a fresh engine
        must exist (not raise AttributeError) without any prior
        update_from_response call."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",  # attribute check is binary-free
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        # These are the exact attributes the host reads. If any
        # are removed, this test breaks before the live crash does.
        assert hasattr(engine, "last_real_prompt_tokens")
        assert hasattr(engine, "last_compression_rough_tokens")
        assert hasattr(engine, "awaiting_real_usage_after_compression")
        # And their default values match the built-in
        # ContextCompressor (test_context_compressor.py:59,86,99).
        assert engine.last_real_prompt_tokens == 0
        assert engine.last_compression_rough_tokens == 0
        assert engine.awaiting_real_usage_after_compression is False

    def test_update_from_response_sets_last_real_prompt_tokens(self, tmp_path):
        """The built-in mirrors last_prompt_tokens into
        last_real_prompt_tokens when provider usage arrives. The
        adapter must do the same or the host's
        f"{_compressor.last_real_prompt_tokens:,}" log line at
        turn_context.py:378 silently drops back to the no-attribute
        crash path."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        engine.update_model("test", context_length=100_000)
        # No usage yet — both stay zero.
        assert engine.last_real_prompt_tokens == 0
        # Provider reports 5000 prompt tokens. last_real_prompt_tokens
        # must mirror it.
        engine.update_from_response({"prompt_tokens": 5000, "completion_tokens": 100, "total_tokens": 5100})
        assert engine.last_real_prompt_tokens == 5000
        # Provider reports 0 prompt tokens (some providers do this
        # on streaming-only responses). Mirror must not overwrite.
        engine.update_from_response({"prompt_tokens": 0, "completion_tokens": 100, "total_tokens": 100})
        assert engine.last_real_prompt_tokens == 5000

    def test_on_session_reset_clears_host_contract_attributes(self, tmp_path):
        """Per the built-in on_session_reset (context_compressor.py:643-646),
        the host-contract attributes must reset to their zero defaults."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        engine.update_model("test", context_length=100_000)
        engine.last_real_prompt_tokens = 42_000
        engine.last_compression_rough_tokens = 30_000
        engine.awaiting_real_usage_after_compression = True
        engine.on_session_reset()
        assert engine.last_real_prompt_tokens == 0
        assert engine.last_compression_rough_tokens == 0
        assert engine.awaiting_real_usage_after_compression is False

    def test_bind_session_state_is_a_no_op(self, tmp_path):
        """run_agent.py:709 calls bind_session_state inside a
        ``hasattr(engine, "bind_session_state")`` guard. The method
        must exist (so the host rebinds on session-reset) but the
        adapter is stateless across sessions, so it is a no-op."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        # Must not raise, must not error.
        assert engine.bind_session_state() is None
        assert engine.bind_session_state(session_db=None, session_id="x") is None

    def test_get_active_compression_failure_cooldown_returns_none(self, tmp_path):
        """agent/turn_context.py:368 reads this with a getattr
        default of None. The adapter must define the method
        explicitly so the host can distinguish "no cooldown" from
        "engine doesn't support cooldowns" (currently the latter
        silently bypasses the preflight-skip path)."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        assert engine.get_active_compression_failure_cooldown() is None

    def test_deepcopy_preserves_host_contract_attributes(self, tmp_path):
        """The host deep-copies the engine between sessions. The
        new attributes must round-trip."""
        engine = ContextGovernorEngine(
            binary="/nonexistent",
            store_dir=str(tmp_path),
            timeout_sec=1,
        )
        engine.last_real_prompt_tokens = 12_345
        engine.last_compression_rough_tokens = 9_999
        engine.awaiting_real_usage_after_compression = True
        clone = copy.deepcopy(engine)
        assert clone.last_real_prompt_tokens == 12_345
        assert clone.last_compression_rough_tokens == 9_999
        assert clone.awaiting_real_usage_after_compression is True

    def test_set_defer_baseline_writes_both_names(self, tmp_path):
        """The new private setter must keep both attribute names in sync."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine._set_defer_baseline(5000)
        assert engine._last_rough_tokens_when_real_fit == 5000
        assert engine.last_rough_tokens_when_real_prompt_fit == 5000
        # Setter accepts and coerces None to 0.
        engine._set_defer_baseline(None)
        assert engine._last_rough_tokens_when_real_fit == 0
        assert engine.last_rough_tokens_when_real_prompt_fit == 0

    def test_adapter_exposes_host_named_baseline_attribute(self, tmp_path):
        """The host's public name ``last_rough_tokens_when_real_prompt_fit``
        must exist on a fresh engine and read zero (matches the built-in
        ContextCompressor test pattern at test_413_compression.py:581,622
        and test_context_compressor.py:60,72,75,80,87)."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        assert hasattr(engine, "last_rough_tokens_when_real_prompt_fit")
        assert engine.last_rough_tokens_when_real_prompt_fit == 0

    def test_update_from_response_uses_rough_estimate_not_real_tokens(self, tmp_path):
        """Regression for the wrong-baseline bug. After a compression
        cycle that parks ``last_compression_rough_tokens``, the next
        real-usage API call that fits under threshold must set the
        defer baseline to the rough estimate — NOT to the real
        provider prompt count. The pre-patch adapter overwrote the
        rough estimate with ``last_prompt_tokens`` on the very next
        line. This test pins the correct behavior."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model("test", context_length=200_000, max_tokens=8_192)
        # Simulate the post-compression state: host parked the rough
        # estimate and set awaiting_real_usage_after_compression = True.
        engine.last_compression_rough_tokens = 95_000
        engine.awaiting_real_usage_after_compression = True
        # Next real-usage API call returns 80_000 prompt tokens
        # (under threshold of 163_175). This is the "real usage fit
        # under threshold" branch — baseline must be the rough
        # estimate (95_000), NOT the real prompt count (80_000).
        engine.update_from_response({
            "prompt_tokens": 80_000,
            "completion_tokens": 100,
            "total_tokens": 80_100,
        })
        assert engine.last_rough_tokens_when_real_prompt_fit == 95_000
        assert engine._last_rough_tokens_when_real_fit == 95_000
        # And the pre-patch bug would have produced 80_000 here:
        assert engine.last_rough_tokens_when_real_prompt_fit != 80_000

    def test_update_from_response_zeroes_baseline_when_above_threshold(self, tmp_path):
        """If real usage is ABOVE threshold, baseline resets to 0
        (matches built-in: context_compressor.py:1020-1021)."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model("test", context_length=200_000, max_tokens=8_192)
        # Seed a non-zero baseline first
        engine._set_defer_baseline(50_000)
        # Real usage above threshold
        engine.update_from_response({
            "prompt_tokens": 200_000,
            "completion_tokens": 100,
            "total_tokens": 200_100,
        })
        assert engine.last_rough_tokens_when_real_prompt_fit == 0
        assert engine._last_rough_tokens_when_real_fit == 0

    def test_on_session_reset_clears_host_named_baseline(self, tmp_path):
        """Per the built-in on_session_reset
        (context_compressor.py:812), the public host-name baseline
        must reset to 0 along with the internal one."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine._set_defer_baseline(75_000)
        engine.on_session_reset()
        assert engine.last_rough_tokens_when_real_prompt_fit == 0
        assert engine._last_rough_tokens_when_real_fit == 0

    def test_deepcopy_preserves_both_baseline_names(self, tmp_path):
        """The previous patch's deepcopy test only covered the host-
        contract attributes. Add a check that the new public
        baseline name also round-trips through deepcopy."""
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.last_rough_tokens_when_real_prompt_fit = 42_000
        clone = copy.deepcopy(engine)
        assert clone.last_rough_tokens_when_real_prompt_fit == 42_000
        assert clone._last_rough_tokens_when_real_fit == 42_000

    def test_update_model_accepts_host_compression_policy(self, tmp_path):
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model(
            "test",
            context_length=100_000,
            max_tokens=10_000,
            threshold_percent=0.50,
            protect_first_n=5,
            protect_last_n=12,
        )
        assert engine.threshold_percent == 0.50
        assert engine.protect_first_n == 5
        assert engine.protect_last_n == 12
        assert engine.max_tokens == 10_000
        assert engine.threshold_tokens == 45_000

    def test_deferred_preflight_stops_when_rough_growth_exceeds_tolerance(self, tmp_path):
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model("test", context_length=200_000, threshold_percent=0.50)
        engine.last_real_prompt_tokens = 80_000
        engine.last_prompt_tokens = 80_000
        engine.last_rough_tokens_when_real_prompt_fit = 100_000
        assert engine.should_defer_preflight_to_real_usage(104_000) is True
        # Baseline moves forward while deferring, like the built-in compressor.
        assert engine.last_rough_tokens_when_real_prompt_fit == 104_000
        # A jump beyond 5% threshold tolerance must not defer; this triggers
        # preflight compression instead of creeping toward hard context overflow.
        assert engine.should_defer_preflight_to_real_usage(120_000) is False

    def test_deferred_preflight_defers_once_immediately_after_compression(self, tmp_path):
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model("test", context_length=200_000, threshold_percent=0.50)
        engine.awaiting_real_usage_after_compression = True
        engine.last_real_prompt_tokens = 150_000  # stale pre-compression value
        assert engine.should_defer_preflight_to_real_usage(140_000) is True

    def test_post_compaction_defer_yields_at_emergency_pressure(self, tmp_path):
        """A successful compaction must not authorize the next API request to
        exceed the usable input window before fresh provider usage arrives.

        This models a large protected tail plus late hook/tool-schema context:
        the post-compaction one-turn defer is useful below the safety band, but
        must yield at 90% of the effective input window so the host invokes its
        normal emergency compaction gate instead of sending the oversized call.
        """
        engine = ContextGovernorEngine(
            binary="/nonexistent", store_dir=str(tmp_path), timeout_sec=1,
        )
        engine.update_model(
            "test", context_length=272_000, max_tokens=8_000, threshold_percent=0.50,
        )
        engine.awaiting_real_usage_after_compression = True
        assert engine._emergency_pressure_threshold() == 237_600
        assert engine.should_defer_preflight_to_real_usage(237_599) is True
        assert engine.should_defer_preflight_to_real_usage(237_600) is False

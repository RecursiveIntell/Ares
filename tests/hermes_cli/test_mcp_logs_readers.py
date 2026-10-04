"""Actual MCP log-reader integration with temporary stderr files only."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import hermes_cli.logs as logs
    import tools.mcp_tool as mcp_tool
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mcp_tool, "_mcp_stderr_log_files", {})
    token = set_hermes_home_override(str(home))
    try:
        yield logs, mcp_tool, home
    finally:
        reset_hermes_home_override(token)
        for fh in mcp_tool._mcp_stderr_log_files.values():
            fh.close()


def capture(runtime, text, server="cea_graph"):
    _logs, mcp_tool, _home = runtime
    record = mcp_tool._begin_stdio_diagnostic(server)
    record["stream"].write(text)
    record["stream"].flush()
    record["status"] = "closed"
    mcp_tool._finish_stdio_diagnostic(record)
    return record


def manual_record(home, attempt="a" * 32, server="cea_graph", text="child payload\n"):
    path = home / "logs" / "mcp-stderr" / f"{attempt}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    record = {
        "kind": "mcp.stdio.attempt", "attempt_id": attempt, "server": server,
        "config_home": str(home), "parent_pid": 123, "stderr_path": str(path),
        "destination": "file", "phase": "transport", "status": "starting",
    }
    return record


def write_index(home, records, legacy=""):
    path = home / "logs" / "mcp-stderr.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(legacy + "".join(json.dumps(record) + "\n" for record in records))
    return path


def test_actual_writer_and_reader_show_stderr_for_only_the_selected_profile(
    runtime, tmp_path, capsys
):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    logs, _mcp_tool, home = runtime
    first = capture(runtime, "first alpha stderr\n")
    token = set_hermes_home_override(str(tmp_path / "beta"))
    try:
        capture(runtime, "beta stderr must stay separate\n")
    finally:
        reset_hermes_home_override(token)
    second = capture(runtime, "second alpha stderr\n", server="pilot_bridge")
    logs.tail_log("mcp", num_lines=50)
    output = capsys.readouterr().out
    assert "first alpha stderr" in output
    assert "second alpha stderr" in output
    assert "beta stderr must stay separate" not in output
    assert first["attempt_id"] in output and second["attempt_id"] in output
    assert "cea_graph" in output and "pilot_bridge" in output
    assert str(home) in output
    # The reader retrieves actual content, without duplicating it in the index.
    assert "first alpha stderr" not in (home / "logs" / "mcp-stderr.log").read_text()
    assert '"kind": "mcp.stdio.attempt"' not in output


def test_tail_limit_applies_to_child_content_and_preserves_raw_json(runtime, capsys):
    logs, _mcp_tool, home = runtime
    record = manual_record(home, text='older\n{"kind":"child.event","message":"newest"}\n')
    write_index(home, [record])
    logs.tail_log("mcp", num_lines=1)
    output = capsys.readouterr().out
    assert '"message":"newest"' in output
    assert "older" not in output
    assert record["attempt_id"] in output


def test_filters_use_child_timestamp_level_component_and_attempt(runtime, capsys):
    logs, _mcp_tool, home = runtime
    now = datetime.now()
    old = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    recent = now.strftime("%Y-%m-%d %H:%M:%S")
    record = manual_record(home, text=(
        f"{old} ERROR tools.reader: stale payload\n"
        f"{recent} INFO tools.reader: lower level\n"
        f"{recent} ERROR gateway.reader: wrong component\n"
        f"{recent} ERROR tools.reader: matching child payload\n"
    ))
    write_index(home, [record])
    logs.tail_log("mcp", level="WARNING", since="1h", component="tools", session=record["attempt_id"])
    output = capsys.readouterr().out
    assert "matching child payload" in output
    assert "stale payload" not in output
    assert "lower level" not in output
    assert "wrong component" not in output


def test_legacy_stderr_and_new_attempts_are_both_readable(runtime, capsys):
    logs, _mcp_tool, home = runtime
    record = manual_record(home, text="attributed child payload\n")
    write_index(home, [record], legacy='legacy raw stderr\n{"kind":"old.server","message":"legacy JSON"}\n')
    logs.tail_log("mcp", num_lines=10)
    output = capsys.readouterr().out
    assert "legacy raw stderr" in output
    assert '"message":"legacy JSON"' in output
    assert "attributed child payload" in output


@pytest.mark.parametrize("payload", ["last unterminated child payload", "progress\r"])
def test_final_unterminated_child_line_stays_separate_from_metadata(runtime, capsys, payload):
    logs, _mcp_tool, _home = runtime
    capture(runtime, payload)
    logs.tail_log("mcp", num_lines=1)
    output = capsys.readouterr().out
    assert payload in output
    assert '"kind": "mcp.stdio.attempt"' not in output


def test_tail_separates_partial_rows_from_two_active_attempts(runtime, capsys):
    logs, mcp_tool, _home = runtime
    first = mcp_tool._begin_stdio_diagnostic("cea_graph")
    first["stream"].write("alpha partial")
    first["stream"].flush()
    second = mcp_tool._begin_stdio_diagnostic("pilot_bridge")
    second["stream"].write("beta complete\n")
    second["stream"].flush()
    try:
        logs.tail_log("mcp", num_lines=20)
        assert Path(first["stderr_path"]).read_bytes().endswith(b"alpha partial")
    finally:
        for record in (second, first):
            record["status"] = "closed"
            mcp_tool._finish_stdio_diagnostic(record)
    output = capsys.readouterr().out
    assert "alpha partial\n" in output
    assert "beta complete\n" in output


def test_tail_to_follow_separates_the_preview_and_completes_its_partial_line(runtime, monkeypatch, capsys):
    logs, mcp_tool, _home = runtime
    record = mcp_tool._begin_stdio_diagnostic("cea_graph")
    record["stream"].write("alpha partial")
    record["stream"].flush()
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            record["stream"].write(" remainder\nfresh complete child\n")
            record["stream"].flush()
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    try:
        logs.tail_log("mcp", num_lines=20, follow=True)
    finally:
        record["status"] = "closed"
        mcp_tool._finish_stdio_diagnostic(record)
    output = capsys.readouterr().out
    assert "alpha partial\n" in output
    assert output.count("alpha partial remainder\n") == 1
    assert output.count("fresh complete child\n") == 1


@pytest.mark.parametrize("invalid", ["wrong_profile", "outside_path", "symlink", "bad_id", "discarded", "missing", "invalid_pid"])
def test_index_cannot_read_an_unowned_or_unavailable_file(runtime, tmp_path, capsys, invalid):
    logs, _mcp_tool, home = runtime
    record = manual_record(home)
    foreign = tmp_path / "other-profile" / "foreign.log"
    foreign.parent.mkdir()
    foreign.write_text("FOREIGN_CONTENT_MUST_NOT_BE_READ\n")
    if invalid == "wrong_profile":
        record["config_home"] = str(foreign.parent)
    elif invalid == "outside_path":
        record["stderr_path"] = str(foreign)
    elif invalid == "symlink":
        path = Path(record["stderr_path"])
        path.unlink()
        path.symlink_to(foreign)
    elif invalid == "bad_id":
        record["attempt_id"] = "../foreign"
    elif invalid == "discarded":
        record["destination"] = "discarded"
    elif invalid == "missing":
        Path(record["stderr_path"]).unlink()
    elif invalid == "invalid_pid":
        record["parent_pid"] = True
    write_index(home, [record])
    logs.tail_log("mcp", num_lines=10)
    output = capsys.readouterr().out
    assert "FOREIGN_CONTENT_MUST_NOT_BE_READ" not in output
    assert "child payload" not in output


def test_follow_reads_existing_and_new_attempts_without_replaying_history(
    runtime, monkeypatch, capsys
):
    logs, mcp_tool, home = runtime
    active = mcp_tool._begin_stdio_diagnostic("cea_graph")
    active["stream"].write("historical child payload\n")
    active["stream"].flush()
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            active["stream"].write("fresh active stderr\n")
            active["stream"].flush()
            capture(runtime, "fresh next-attempt stderr\n", server="pilot_bridge")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    try:
        logs.tail_log("mcp", num_lines=0, follow=True)
    finally:
        active["status"] = "closed"
        mcp_tool._finish_stdio_diagnostic(active)
    output = capsys.readouterr().out
    assert "historical child payload" not in output
    assert output.count("fresh active stderr") == 1
    assert output.count("fresh next-attempt stderr") == 1
    assert active["attempt_id"] in output
    assert str(home) in output


def test_follow_preserves_legacy_appends_and_handles_attempt_replacement(
    runtime, monkeypatch, capsys
):
    logs, _mcp_tool, home = runtime
    record = manual_record(home, text="existing child payload\n")
    index = write_index(home, [record], legacy="legacy history\n")
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            with index.open("a") as stream:
                stream.write("legacy fresh stderr\n")
            path = Path(record["stderr_path"])
            replacement = path.with_suffix(".new")
            replacement.write_text("replacement child stderr\n")
            replacement.replace(path)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    logs.tail_log("mcp", num_lines=0, follow=True)
    output = capsys.readouterr().out
    assert output.count("legacy fresh stderr") == 1
    assert output.count("replacement child stderr") == 1
    assert "existing child payload" not in output


def test_follow_preserves_an_index_record_across_the_read_budget(runtime, monkeypatch, capsys):
    logs, _mcp_tool, home = runtime
    index = write_index(home, [])
    record = manual_record(home, text="boundary child stderr\n")
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            with index.open("a") as stream:
                stream.write("x" * (65536 - 32) + "\n" + json.dumps(record) + "\n")
        elif iterations > 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    logs.tail_log("mcp", num_lines=0, follow=True)
    output = capsys.readouterr().out
    assert output.count("boundary child stderr") == 1
    assert '"kind": "mcp.stdio.attempt"' not in output


@pytest.mark.parametrize("filtered", [False, True])
def test_follow_frames_a_child_line_written_across_polls(runtime, monkeypatch, capsys, filtered):
    logs, _mcp_tool, home = runtime
    record = manual_record(home, text="")
    write_index(home, [record])
    path = Path(record["stderr_path"])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    first = f"{stamp} ERROR tools.reader: split child "
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            with path.open("a") as stream:
                stream.write(first)
        elif iterations == 2:
            assert "split child" not in capsys.readouterr().out
            with path.open("a") as stream:
                stream.write("payload\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    options = {"level": "WARNING", "component": "tools", "since": "1h"} if filtered else {}
    logs.tail_log("mcp", num_lines=0, follow=True, **options)
    output = capsys.readouterr().out
    assert output.count(first + "payload\n") == 1
    assert output.count("[mcp ") == 1


def test_follow_frames_a_control_record_written_across_polls(runtime, monkeypatch, capsys):
    logs, _mcp_tool, home = runtime
    index = write_index(home, [])
    record = manual_record(home, text="split-index child payload\n")
    encoded = json.dumps(record)
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            with index.open("a") as stream:
                stream.write(encoded[:40])
        elif iterations == 2:
            assert '"kind"' not in capsys.readouterr().out
            with index.open("a") as stream:
                stream.write(encoded[40:] + "\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    logs.tail_log("mcp", num_lines=0, follow=True)
    output = capsys.readouterr().out
    assert output.count("split-index child payload") == 1
    assert '"kind": "mcp.stdio.attempt"' not in output


@pytest.mark.parametrize("filtered", [False, True])
def test_follow_advances_past_an_oversized_line_with_a_visible_notice(runtime, monkeypatch, capsys, filtered):
    logs, _mcp_tool, home = runtime
    monkeypatch.setattr(logs, "_MCP_FOLLOW_BYTES", 512, raising=False)
    monkeypatch.setattr(logs, "_MCP_MAX_LINE_BYTES", 2048, raising=False)
    record = manual_record(home, text="")
    write_index(home, [record])
    path = Path(record["stderr_path"])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            with path.open("a") as stream:
                stream.write("x" * 4097 + f"\n{stamp} ERROR tools.reader: after oversized child line\n")
        elif iterations >= 12:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    options = {"level": "WARNING", "component": "tools", "since": "1h"} if filtered else {}
    logs.tail_log("mcp", num_lines=0, follow=True, **options)
    output = capsys.readouterr().out
    assert output.count("exceeds MCP preview limit") == 1
    assert output.count("after oversized child line") == 1


@pytest.mark.parametrize("filtered", [False, True])
def test_tail_has_a_finite_window_and_keeps_recent_content(runtime, monkeypatch, capsys, filtered):
    logs, _mcp_tool, home = runtime
    monkeypatch.setattr(logs, "_MCP_TAIL_BYTES", 2048, raising=False)
    monkeypatch.setattr(logs, "_MCP_MAX_LINE_BYTES", 1024, raising=False)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = manual_record(home, text="x" * 4096 + f"\n{stamp} ERROR tools.reader: recent child payload\n")
    write_index(home, [record])
    options = {"level": "WARNING", "component": "tools", "since": "1h"} if filtered else {}
    logs.tail_log("mcp", num_lines=10, **options)
    output = capsys.readouterr().out
    assert "recent child payload" in output
    assert "exceeds MCP preview limit" in output
    assert "x" * 2048 not in output


def test_follow_discards_a_pending_fragment_on_observed_truncation(runtime, monkeypatch, capsys):
    logs, _mcp_tool, home = runtime
    record = manual_record(home, text="")
    write_index(home, [record])
    path = Path(record["stderr_path"])
    iterations = 0

    def advance(_seconds):
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            path.write_text("stale fragment" * 100)
        elif iterations == 2:
            path.write_text("replacement complete\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(logs.time, "sleep", advance)
    logs.tail_log("mcp", num_lines=0, follow=True)
    output = capsys.readouterr().out
    assert "stale fragment" not in output
    assert output.count("replacement complete\n") == 1


def test_non_mcp_reader_keeps_its_existing_behavior(runtime, capsys):
    logs, _mcp_tool, home = runtime
    path = home / "logs" / "agent.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("older agent row\nnewest agent row\n")
    logs.tail_log("agent", num_lines=1)
    output = capsys.readouterr().out
    assert "newest agent row" in output and "older agent row" not in output
    assert "[mcp " not in output

"""Todo callbacks under existing dispatcher and SessionDB turn custody.

This is a consumer of the canonical snapshot owner, not a second todo ledger.
The trusted application dispatcher supplies the live block; row/call IDs only
correlate it. Neither these checks nor Python object types authenticate hostile
in-process code. Intentional no-DB and persistence-isolated agents stay local.
"""
import json
import threading
import uuid

from hermes_state import SessionDB, TodoSnapshotError
from tools.todo_tool import TodoStore, todo_tool


_RLOCK_TYPE = type(threading.RLock())


def _error(reason, disposition="rejected"):
    return json.dumps({"error": "Todo owner execution failed", "reason": reason,
                       "disposition": disposition})


def _anchor(db, session_id, messages, tool_call_id):
    """Validate the current dispatched block against committed owner rows."""
    if not isinstance(messages, list) or not isinstance(tool_call_id, str) or not tool_call_id:
        raise ValueError("dispatcher_context")
    assistant = next((m for m in reversed(messages)
                      if isinstance(m, dict) and m.get("role") != "tool"), None)
    if not assistant or assistant.get("role") != "assistant":
        raise ValueError("assistant_block")
    row_id = assistant.get("_row_id")
    if type(row_id) is not int or row_id <= 0:
        raise ValueError("uncommitted_anchor")
    calls = assistant.get("tool_calls")
    if not isinstance(calls, list):
        raise ValueError("tool_calls")
    matching = [c for c in calls if isinstance(c, dict) and c.get("id") == tool_call_id]
    if (len(matching) != 1 or not isinstance(matching[0].get("function"), dict)
            or matching[0]["function"].get("name") != "todo"):
        raise ValueError("tool_call_binding")
    # Keyset read starts at the exact anchor, never scans/imports old transcript.
    rows = db.get_messages(session_id, after_id=row_id - 1)
    if (not rows or rows[0]["id"] != row_id or rows[0]["role"] != "assistant"
            or rows[0].get("tool_calls") != calls
            or any(r["role"] != "tool" for r in rows[1:])):
        raise ValueError("persisted_block_binding")
    head = db.get_active_message_watermark(session_id)
    if head != rows[-1]["id"]:
        raise ValueError("head_changed")
    return row_id, head


def execute_todo(agent, args, *, tool_call_id=None, messages=None):
    """Stage then commit; never turn an owner error into memory-only success."""
    db = getattr(agent, "_session_db", None)
    store = getattr(agent, "_todo_store", None)
    if db is None or getattr(agent, "_persist_disabled", False):
        return todo_tool(todos=args.get("todos"), merge=args.get("merge", False), store=store)
    lock = getattr(agent, "_session_persist_lock", None)
    if not isinstance(db, SessionDB) or not isinstance(store, TodoStore):
        return _error("owner")
    if not isinstance(lock, _RLOCK_TYPE):
        return _error("persistence_lock")
    with lock:
        session_id = getattr(agent, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return _error("session")
        if getattr(agent, "_incremental_persistence_failed", False):
            return _error("prior_persistence_failure", "outcome_unknown")
        writing = args.get("todos") is not None
        holder = None
        anchor = head = 0
        try:
            if writing:
                holder = getattr(agent, "_active_session_turn_lease_holder", None)
                if not isinstance(holder, str) or not holder:
                    return _error("lease")
                anchor, head = _anchor(db, session_id, messages, tool_call_id)
            selection = db.get_todo_recovery_state(session_id)
            if selection.state == "selected":
                if selection.snapshot is None:
                    return _error("selection")
                initial = selection.snapshot["todos"]
            elif selection.state == "cleared_by_rewind":
                initial = []
            elif selection.state == "never_committed":
                # Existing explicitly hydrated legacy cache may seed a newly
                # executed mutation; passive transcript import never commits.
                initial = store.read()
            else:
                return _error("selection")
            staged = TodoStore()
            staged.write(initial)
            result = todo_tool(todos=args.get("todos"), merge=args.get("merge", False), store=staged)
            if not writing or "error" in json.loads(result):
                return result
            todos_json = json.dumps(staged.read(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (TodoSnapshotError, ValueError, TypeError, KeyError):
            return _error("admission")
        except Exception:
            return _error("owner_read_unavailable")

        try:
            committed = db.commit_todo_snapshot(
                session_id=session_id, owner_execution_id=uuid.uuid4().hex,
                anchor_assistant_row_id=anchor, expected_head_id=head,
                turn_lease_holder=holder,
                expected_prior_snapshot_id=(selection.snapshot["snapshot_id"]
                                            if selection.snapshot is not None else None),
                todos_json=todos_json,
            )
        except TodoSnapshotError as exc:
            return _error(exc.reason)
        except Exception:
            # The owner may have committed before acknowledgement was lost.
            # Do not retry, publish proposed cache, or dispatch later mutations.
            agent._incremental_persistence_failed = True
            return _error("commit_unacknowledged", "outcome_unknown")
        try:
            if committed is None:
                raise ValueError("missing committed projection")
            store.write(committed["todos"], merge=False)
            return todo_tool(store=store)
        except Exception:
            # Durable commit is not undone because its projection failed.
            agent._incremental_persistence_failed = True
            return _error("committed_projection_failed", "committed")

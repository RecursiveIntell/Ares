"""Exercise actual agent serialization/flush and temporary SessionDB owner."""
import copy

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.mark.parametrize("fail", [False, True])
def test_flush_projects_only_committed_row_coordinates(tmp_path, fail):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", source="test")
        agent = AIAgent.__new__(AIAgent)
        agent.session_id = "session"
        agent._session_db = db
        agent._session_db_created = True
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_ids = set()
        agent._flushed_db_message_session_id = None
        rows = [{"role": "user", "content": "request"},
                {"role": "assistant", "content": "action", "_row_id": -88,
                 "tool_calls": [{"id": "model-id", "type": "function",
                                 "function": {"name": "todo", "arguments": "{}"}}]}]
        before = copy.deepcopy(rows)
        if fail:
            db._conn.execute("""CREATE TRIGGER reject_counter BEFORE UPDATE OF message_count ON sessions
                WHEN NEW.message_count > 0
                BEGIN SELECT RAISE(ABORT, 'injected counter failure'); END""")
        result = agent._flush_messages_to_session_db(rows)
        if fail:
            assert result is False
            assert rows == before
            assert db.get_messages("session") == []
        else:
            assert result is True
            stored = db.get_messages("session")
            assert [row.get("_row_id") for row in rows] == [row["id"] for row in stored]
            assert all(row["_db_persisted"] for row in rows)
            assert agent._flush_messages_to_session_db(rows) is True
            assert db.get_messages("session") == stored
    finally:
        db.close()

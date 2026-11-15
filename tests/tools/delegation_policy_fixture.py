"""Real temporary delegation-policy ownership for unittest-style suites."""

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


@contextmanager
def temporary_policy_owner():
    """Yield a real session DB and session ID; never alter the live profile."""
    with TemporaryDirectory(prefix="delegation-policy-test-") as directory:
        home = Path(directory)
        (home / "config.yaml").write_text('delegation:\n  provider: ""\n  model: ""\n')
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            token = set_hermes_home_override(home)
            db = None
            try:
                db = SessionDB(home / "state.db")
                session_id = "delegation-test-parent"
                db.create_session(session_id, "cli", model="test-model")
                yield db, session_id
            finally:
                if db is not None:
                    db.close()
                reset_hermes_home_override(token)

"""The governor binary fallback must not name one machine's home directory.

`_default_binary` used to fall through to `/home/<user>/.local/bin/context-governor`, so a checkout on
another machine could silently resolve to a path that exists only on the author's. The fallback is now the
bare command name, which the caller resolves the same way it resolves any other command.
"""
from plugins.context_engine._context_governor import pure_helpers


def test_a_resolved_binary_is_used(monkeypatch):
    monkeypatch.setattr(pure_helpers.shutil, "which", lambda name: "/opt/bin/context-governor")
    assert pure_helpers._default_binary() == "/opt/bin/context-governor"


def test_without_a_resolved_binary_the_bare_name_is_returned(monkeypatch):
    monkeypatch.setattr(pure_helpers.shutil, "which", lambda name: None)
    value = pure_helpers._default_binary()
    assert value == "context-governor"
    assert not value.startswith("/")

import threading

import pytest

from tools import delegate_tool as delegate


@pytest.mark.parametrize('value', [True, False, 'oops', '', [], {}, float('inf'), float('nan'), '-inf', '1e100', threading.TIMEOUT_MAX * 2])
def test_invalid_cap_is_a_typed_refusal(monkeypatch, value):
    monkeypatch.setattr(delegate, '_load_config', lambda: {'child_timeout_seconds': value})
    with pytest.raises(ValueError, match='DELEGATION_INVALID_TIMEOUT'):
        delegate._get_child_timeout()


@pytest.mark.parametrize('value, expected', [(None, None), (0, None), ('0', None), (-1, None), ('-2', None), (1, 30.0), ('60.5', 60.5), (600, 600.0)])
def test_legacy_disabled_and_positive_floor(monkeypatch, value, expected):
    monkeypatch.delenv('DELEGATION_CHILD_TIMEOUT_SECONDS', raising=False)
    monkeypatch.setattr(delegate, '_load_config', lambda: {'child_timeout_seconds': value})
    assert delegate._get_child_timeout() == expected

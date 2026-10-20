"""Preserve predecessor launchctl approval semantics while fixing scan cost."""
import re

import pytest

from tools import approval

_REASON = 'stop/restart hermes launchd service (kills running agents)'
_PREDECESSOR = re.compile(
    r'(?=[\s\S]*\blaunchctl\s+(?:stop|kickstart|bootout|unload|kill|disable|remove)\b)'
    r'(?=[\s\S]*\b(?:hermes|ai\.hermes)\b)',
    re.IGNORECASE | re.DOTALL,
)


def _current_rule():
    return next(expression for expression, reason in approval.DANGEROUS_PATTERNS_COMPILED
                if reason == _REASON)


def _short_cases():
    result = ['', 'printf safe', 'printf hermes', 'launchctl list ai.hermes.gateway',
              'launchctl stop com.other.service', 'relaunchctl stop hermes', 'launchctl stopped hermes']
    for verb in ('stop', 'kickstart', 'bootout', 'unload', 'kill', 'disable', 'remove'):
        for label in ('hermes', 'ai.hermes.gateway', 'HERMeS', 'other.service'):
            for separator in ('; ', '\n', ' && '):
                result.extend([f'{label}{separator}launchctl {verb} $label',
                               f'launchctl {verb} $label{separator}{label}'])
    for prefix in ('', '\u2603 ', '\x00', 'printf safe;\n'):
        result.extend([prefix + 'hermes\nlaunchctl kickstart -k $label',
                       prefix + 'launchctl bootout $label\nhermes'])
    return sorted(set(result))


@pytest.mark.parametrize('command', _short_cases())
def test_launchctl_rule_retains_predecessor_language(command):
    assert bool(_current_rule().search(command)) == bool(_PREDECESSOR.search(command))


def test_launchctl_legacy_approval_key_is_preserved():
    assert approval._legacy_pattern_key(_current_rule().pattern) == approval._legacy_pattern_key(_PREDECESSOR.pattern)


def test_stored_legacy_launchctl_approval_still_authorizes_same_reason(monkeypatch):
    legacy = approval._legacy_pattern_key(_PREDECESSOR.pattern)
    monkeypatch.setattr(approval, '_permanent_approved', set())
    monkeypatch.setattr(approval, '_session_approved', {})
    session = 'launchctl-anchor-legacy-control'
    assert approval.is_approved(session, _REASON) is False
    assert {_REASON, legacy} <= approval._approval_key_aliases(_REASON)
    monkeypatch.setitem(approval._session_approved, session, {legacy})
    assert approval.is_approved(session, _REASON) is True


@pytest.mark.parametrize('command', [
    'printf hermes; launchctl stop $label',
    'launchctl kickstart -k $label; printf hermes',
    'printf ai.hermes.gateway\nlaunchctl bootout $label',
])
def test_full_detector_keeps_order_independent_launchctl_approval(command):
    assert approval.detect_dangerous_command(command) == (True, _REASON, _REASON)

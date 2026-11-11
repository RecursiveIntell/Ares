"""Regression (audit L6a): a specialist response split across TCP reads must parse.

_desktop_request previously issued one recv(); a reply split across packets was
rejected as malformed. These tests drive the real function with a fake socket
that delivers the reply in fragments.
"""

import json

import pytest

from ares_runtime import specialist_dispatch as sd


class _FragmentedConnection:
    """Serves a fixed reply in the given chunk sizes, then EOF."""

    def __init__(self, reply: bytes, chunk: int):
        self._chunks = [reply[i : i + chunk] for i in range(0, len(reply), chunk)] or [b""]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, _data):
        return None

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


def _endpoint(tmp_path):
    token = "t" * 40
    (tmp_path / "specialist-dispatch.json").write_text(
        json.dumps(
            {
                "schema": sd.ENDPOINT_SCHEMA,
                "host": "127.0.0.1",
                "port": 9,
                "token": token,
            }
        )
    )
    return tmp_path


def _patch_connection(monkeypatch, reply: bytes, chunk: int):
    monkeypatch.setattr(
        sd.socket,
        "create_connection",
        lambda *_a, **_k: _FragmentedConnection(reply, chunk),
    )


@pytest.mark.parametrize("chunk", [1, 7, 64])
def test_reply_split_across_reads_is_reassembled(tmp_path, monkeypatch, chunk):
    reply = json.dumps({"schema": sd.ENDPOINT_SCHEMA, "outcome": "admitted"}).encode() + b"\n"
    _patch_connection(monkeypatch, reply, chunk)
    result = sd._desktop_request(_endpoint(tmp_path), {"operation": "status"})
    assert result == {"schema": sd.ENDPOINT_SCHEMA, "outcome": "admitted"}


def test_reply_over_bound_is_rejected(tmp_path, monkeypatch):
    big = b'{"x":"' + b"a" * (sd.MAX_RESPONSE_BYTES + 10) + b'"}\n'
    _patch_connection(monkeypatch, big, 4096)
    with pytest.raises(sd.DispatchError):
        sd._desktop_request(_endpoint(tmp_path), {"operation": "status"})

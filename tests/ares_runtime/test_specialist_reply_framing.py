"""Regression (audit L6a): a specialist response split across TCP reads must parse.

_desktop_request previously issued one recv(); a reply split across packets was
rejected as malformed. These tests drive the real function with a fake socket
that delivers the reply in fragments.
"""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

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


class _ScriptedConnection(_FragmentedConnection):
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, size):
        chunk = self._chunks.pop(0) if self._chunks else b""
        if isinstance(chunk, BaseException):
            raise chunk
        if len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
        return chunk[:size]


def _scripted_connection(monkeypatch, chunks):
    connection = _ScriptedConnection(chunks)
    monkeypatch.setattr(sd.socket, "create_connection", lambda *_a, **_k: connection)


@pytest.mark.parametrize("reply", [b'{"outcome":"admitted"}', b"", b'{"outcome":'])
def test_reply_requires_newline_before_eof(tmp_path, monkeypatch, reply):
    _scripted_connection(monkeypatch, [reply])
    with pytest.raises(sd.DispatchError):
        sd._desktop_request(_endpoint(tmp_path), {"operation": "status"})


@pytest.mark.parametrize("suffix", [b" ", b"\n", b'{"second":true}\n'])
@pytest.mark.parametrize("separate_read", [False, True])
def test_reply_rejects_every_byte_after_the_frame(tmp_path, monkeypatch, suffix, separate_read):
    frame = b'{"outcome":"admitted"}\n'
    chunks = [frame, suffix] if separate_read else [frame + suffix]
    _scripted_connection(monkeypatch, chunks)
    with pytest.raises(sd.DispatchError, match="DESKTOP_RESPONSE_INVALID"):
        sd._desktop_request(_endpoint(tmp_path), {"operation": "status"})


@pytest.mark.parametrize("frame_first", [False, True])
def test_reply_timeout_is_unavailable_not_a_success(tmp_path, monkeypatch, frame_first):
    chunks = [b'{"outcome":"admitted"}\n'] if frame_first else []
    failure = TimeoutError("injected bounded socket timeout")
    _scripted_connection(monkeypatch, chunks + [failure])
    with pytest.raises(sd.DispatchError, match="DESKTOP_DISPATCH_UNAVAILABLE") as raised:
        sd._desktop_request(_endpoint(tmp_path), {"operation": "status"})
    assert raised.value.__cause__ is failure


def test_reply_wire_bound_includes_the_required_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "MAX_RESPONSE_BYTES", 128)
    body = b'{"outcome":"admitted"}'
    frame = body + b" " * (sd.MAX_RESPONSE_BYTES - len(body) - 1) + b"\n"
    _scripted_connection(monkeypatch, [frame])
    assert sd._desktop_request(_endpoint(tmp_path), {"operation": "status"}) == {"outcome": "admitted"}
    _scripted_connection(monkeypatch, [frame[:-1] + b" \n"])
    with pytest.raises(sd.DispatchError, match="DESKTOP_RESPONSE_INVALID"):
        sd._desktop_request(tmp_path, {"operation": "status"})


@contextmanager
def _loopback_peer(tmp_path, reply, fragment_size):
    """One disposable peer; no installed endpoint or credential is read."""
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        root = _endpoint(tmp_path)
        endpoint_path = root / "specialist-dispatch.json"
        endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
        endpoint["port"] = listener.getsockname()[1]
        endpoint_path.write_text(json.dumps(endpoint), encoding="utf-8")

        def serve():
            with listener.accept()[0] as peer:
                peer.settimeout(5)
                request = bytearray()
                while not request.endswith(b"\n"):
                    chunk = peer.recv(4096)
                    assert chunk, "client closed before request frame"
                    request.extend(chunk)
                    assert len(request) <= sd.MAX_REQUEST_BYTES
                assert json.loads(request) == {"operation": "status"}
                for offset in range(0, len(reply), fragment_size):
                    peer.sendall(reply[offset : offset + fragment_size])

        served = pool.submit(serve)
        try:
            yield root
        finally:
            served.result(timeout=5)


def test_real_tcp_reply_sent_bytewise_is_accepted(tmp_path):
    frame = b'{"outcome":"admitted"}\n'
    with _loopback_peer(tmp_path, frame, 1) as root:
        assert sd._desktop_request(root, {"operation": "status"}) == {"outcome": "admitted"}


@pytest.mark.parametrize("reply", [b'{"outcome":"admitted"}', b'{"outcome":"admitted"}\n \n'])
def test_real_tcp_invalid_frame_is_rejected(tmp_path, reply):
    with _loopback_peer(tmp_path, reply, len(reply)) as root:
        with pytest.raises(sd.DispatchError):
            sd._desktop_request(root, {"operation": "status"})

"""An idle RPC listener owns no tool-dispatch initialization work."""
import builtins
import socket
import threading
import types

from tools import code_execution_tool as code


def test_unused_rpc_listener_does_not_import_dispatcher(monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    ready = threading.Event()
    stopped = threading.Event()
    imports = []
    errors = []
    original_import = builtins.__import__

    def observe_import(name, *args, **kwargs):
        if name == "model_tools" and threading.current_thread() is worker:
            imports.append(name)
            return types.SimpleNamespace(handle_function_call=lambda *a, **k: None)
        return original_import(name, *args, **kwargs)

    class ObservedListener:
        def settimeout(self, value):
            listener.settimeout(value)
            ready.set()

        def accept(self):
            return listener.accept()

    def serve():
        try:
            code._rpc_server_loop(ObservedListener(), "no-rpc", [], [0], 1,
                                  frozenset(), stopped, "fixture-token")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve, name="test-unused-rpc-listener")
    monkeypatch.setattr(builtins, "__import__", observe_import)
    worker.start()
    try:
        assert ready.wait(3), "RPC listener never reached accept setup"
    finally:
        stopped.set()
        worker.join(3)
        listener.close()
    assert not worker.is_alive()
    assert not errors
    assert imports == [], "no request exists that could own dispatcher initialization"

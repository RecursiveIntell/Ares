"""Disposable real backend for native pairing witnesses. Lifespan is disabled."""
import json
import os
from pathlib import Path
import socket
import sys

# The runner supplies only a source-owned fixture and synthetic environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from hermes_cli import web_server as ws
import uvicorn

ws.app.state.bound_host = "127.0.0.1"
ws.app.state.auth_required = False
ws.app.state.trusted_public_hosts = frozenset()
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
print(json.dumps({"port": listener.getsockname()[1], "pid": os.getpid(), "identity": ws._DESKTOP_BOOTSTRAP_ID}), flush=True)
server = uvicorn.Server(uvicorn.Config(ws.app, lifespan="off", proxy_headers=False, access_log=False, log_level="error"))
try:
    server.run(sockets=[listener])
finally:
    listener.close()

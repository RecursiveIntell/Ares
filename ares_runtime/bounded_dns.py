"""DNS is an explicitly owned cancellable subprocess, never an orphan thread.

A libc resolver may ignore socket deadlines. This owner can kill and reap its
resolver without waiting for an unbounded asyncio default-executor shutdown.
No credentials or prompt content enter the resolver process environment.
"""

import asyncio
import ipaddress
import json
import sys

import anyio
import httpcore

_CODE = """import json,socket,sys
print(json.dumps(list(dict.fromkeys(v[4][0] for v in socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)))))
"""


async def resolve(host, port):
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    # Trio support is explicitly refused at transport admission until qualified.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _CODE,
        host,
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={},
    )
    try:
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise httpcore.ConnectError("bounded DNS resolver failed")
        result = json.loads(stdout)
        if not result or any(not isinstance(v, str) for v in result):
            raise httpcore.ConnectError("bounded DNS resolver returned no addresses")
        return result
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.CancelScope(shield=True):
                await process.wait()

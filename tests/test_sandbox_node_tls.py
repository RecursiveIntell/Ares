"""Exercise stage2's emitted Node trust settings against its real MITM proxy."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time

import pytest

pytestmark = pytest.mark.linux_only
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def npm_proxy(tmp_path):
    required = ("bash", "openssl", "node", "npm")
    if any(shutil.which(binary) is None for binary in required):
        pytest.skip("sandbox TLS fixture requires bash, openssl, Node and npm")
    real_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    if not real_ca.is_file():
        pytest.skip("sandbox TLS fixture requires the host public CA bundle")
    clean_env = {key: value for key, value in os.environ.items()
                 if key in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    sandbox = tmp_path / "sandbox"
    certs = sandbox / "root/certs"
    for relative in ("root/logs", "root/certs", "etc", "home"):
        (sandbox / relative).mkdir(parents=True, exist_ok=True)
    (sandbox / "root/logs/slirp.ready").write_text("ready\n")
    ca_env = clean_env | {"OPENSSL_CONF": str(REPO_ROOT / "scripts/sandbox/openssl.cnf")}
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "2", "-subj", "/CN=Sandbox TLS test CA",
                    "-extensions", "sandbox_ca_ext", "-keyout", str(certs / "ca.key"),
                    "-out", str(certs / "ca.pem")], env=ca_env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    shutil.copyfile(real_ca, certs / "real-ca.pem")

    # Capture the real stage2 command boundary without namespaces or mounts.
    binaries = tmp_path / "bin"
    binaries.mkdir()
    capture = tmp_path / "bwrap-argv.json"
    bwrap = binaries / "bwrap"
    bwrap.write_text(f"#!{sys.executable}\nimport json,sys\n"
                     f"json.dump(sys.argv[1:],open({str(capture)!r},'w'))\n")
    bwrap.chmod(0o755)
    env = clean_env | {"PATH": str(binaries) + os.pathsep + clean_env["PATH"],
                      "DEV_SANDBOX_ROOT": str(sandbox), "DEV_SANDBOX_BASH": "/bin/bash",
                      "DEV_SANDBOX_INTERACTIVE": "false", "DEV_SANDBOX_USER": "test",
                      "DEV_SANDBOX_HOME": "/home/test"}
    subprocess.run(["bash", str(REPO_ROOT / "scripts/sandbox/stage2-run.sh"), "true"],
                   env=env, check=True, capture_output=True, timeout=15)
    argv = json.loads(capture.read_text())
    projected = {argv[index + 1]: argv[index + 2]
                 for index, value in enumerate(argv) if value == "--setenv"}
    node_ca = sandbox / "root" / Path(projected["NODE_EXTRA_CA_CERTS"]).relative_to("/work")

    root = tmp_path / "http"
    registry = root / "registry.npmjs.org"
    registry.mkdir(parents=True)
    package_bytes = json.dumps({"name": "ares-ci-trust-proof", "version": "1.0.0"}).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as package_tar:
        member = tarfile.TarInfo("package/package.json")
        member.size = len(package_bytes)
        package_tar.addfile(member, io.BytesIO(package_bytes))
    tarball = archive.getvalue()
    tarball_path = registry / "ares-ci-trust-proof/-/ares-ci-trust-proof-1.0.0.tgz"
    tarball_path.parent.mkdir(parents=True)
    tarball_path.write_bytes(tarball)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    metadata = {"name": "ares-ci-trust-proof", "dist-tags": {"latest": "1.0.0"},
                "versions": {"1.0.0": {"name": "ares-ci-trust-proof", "version": "1.0.0",
                    "dist": {"tarball": "https://registry.npmjs.org/ares-ci-trust-proof/-/ares-ci-trust-proof-1.0.0.tgz",
                             "integrity": integrity}}}}
    metadata_path = registry / "ares-ci-trust-proof/index.html"
    metadata_path.write_text(json.dumps(metadata))

    # Override only the listen port and reject all non-fixture upstream I/O.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    driver = """
import importlib.util,sys,threading
path,root,certs,real_ca,port=sys.argv[1:]
sys.argv=[path,root,certs,real_ca]
spec=importlib.util.spec_from_file_location('sandbox_proxy',path)
proxy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
def no_upstream(*args):
    raise RuntimeError('unmocked upstream request refused by test fixture')
proxy.forward_https=no_upstream
proxy.forward_http=no_upstream
proxy.LISTEN_ADDRESS=('127.0.0.1',int(port))
threading.Thread(target=proxy.main,daemon=True).start()
sys.stdin.read()
"""
    with (tmp_path / "proxy.log").open("w") as proxy_log:
        proxy = subprocess.Popen([sys.executable, "-c", driver,
             str(REPO_ROOT / "scripts/sandbox/proxy.py"), str(root), str(certs),
             str(certs / "real-ca.pem"), str(port)], env=ca_env,
             stdin=subprocess.PIPE, stdout=proxy_log, stderr=proxy_log)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.05):
                        break
                except OSError:
                    time.sleep(.02)
            else:
                pytest.fail("sandbox proxy failed to start")
            yield {"env": clean_env, "node_ca": node_ca, "certs": certs,
                   "proxy": f"http://127.0.0.1:{port}", "registry": registry,
                   "metadata": metadata, "metadata_path": metadata_path,
                   "projected": projected}
        finally:
            proxy.stdin.close()
            proxy.wait(timeout=5)


def npm_install(fixture, project, *, ca=None):
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"name": "sandbox-tls-app",
        "version": "1.0.0", "dependencies": {"ares-ci-trust-proof": "1.0.0"}}))
    env = fixture["env"] | {"HOME": str(project),
        "CURL_CA_BUNDLE": str(fixture["certs"] / "ca.pem"),
        "SSL_CERT_FILE": str(fixture["certs"] / "ca.pem"),
        "NODE_EXTRA_CA_CERTS": str(ca or fixture["node_ca"]),
        "HTTPS_PROXY": fixture["proxy"], "HTTP_PROXY": fixture["proxy"], "NO_PROXY": "",
        "npm_config_update_notifier": "false"}
    return subprocess.run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund",
        "--fetch-retries=0", "--fetch-timeout=5000", "--loglevel=error",
        "--registry=https://registry.npmjs.org", f"--https-proxy={fixture['proxy']}"],
        env=env, cwd=project, capture_output=True, text=True, timeout=20)


def test_stage2_node_trust_installs_from_sandbox_proxy(npm_proxy, tmp_path):
    bundle = npm_proxy["node_ca"].read_bytes()
    assert (npm_proxy["certs"] / "ca.pem").read_bytes() in bundle
    assert (npm_proxy["certs"] / "real-ca.pem").read_bytes() in bundle
    done = npm_install(npm_proxy, tmp_path / "install")
    assert done.returncode == 0, done.stdout + done.stderr
    installed = json.loads((tmp_path / "install/node_modules/ares-ci-trust-proof/package.json").read_text())
    assert installed["version"] == "1.0.0"


def test_public_roots_alone_refuse_sandbox_proxy(npm_proxy, tmp_path):
    done = npm_install(npm_proxy, tmp_path / "untrusted", ca=npm_proxy["certs"] / "real-ca.pem")
    assert done.returncode != 0
    assert "UNABLE_TO_VERIFY_LEAF_SIGNATURE" in done.stderr


def test_trusted_sandbox_proxy_still_rejects_tarball_integrity_mismatch(npm_proxy, tmp_path):
    bad_integrity = "sha512-" + base64.b64encode(hashlib.sha512(b"wrong archive").digest()).decode()
    npm_proxy["metadata"]["versions"]["1.0.0"]["dist"]["integrity"] = bad_integrity
    npm_proxy["metadata_path"].write_text(json.dumps(npm_proxy["metadata"]))
    done = npm_install(npm_proxy, tmp_path / "bad-integrity")
    assert done.returncode != 0
    assert "EINTEGRITY" in done.stderr

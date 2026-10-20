"""Synthetic native request factory for real caller fixtures; no live secrets."""
import secrets
import time
from hermes_cli.dashboard_auth.native_bootstrap import native_mac


def native_headers(key, identity, endpoint):
    nonce = secrets.token_hex(32)
    issued = int(time.time())
    return {
        "X-Hermes-Native-Identity": identity,
        "X-Hermes-Native-Nonce": nonce,
        "X-Hermes-Native-Issued": str(issued),
        "X-Hermes-Native-Proof": native_mac(key, ["hermes-native-v1", "request", endpoint, identity, nonce, issued]),
    }

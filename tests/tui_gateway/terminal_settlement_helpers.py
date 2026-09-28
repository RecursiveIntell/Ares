"""Test synchronization for asynchronous host terminal projection."""
import time


def wait_for_terminal_projection(host, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with host._registry_lock:
            if not host._terminal_workers and not host._terminal_queues:
                return
        time.sleep(0.005)
    raise AssertionError("host terminal projection did not become idle")

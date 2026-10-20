"""Process-local, bounded single-use grants for an operator's browser launch.

These authorize HTML bootstrap only, never API calls. Restart drops outstanding
launch grants. URL credentials remain sensitive until redeemed or expired.
"""
import hashlib
import secrets
import threading
import time


class BrowserLaunchTickets:
    def __init__(self, *, clock=time.monotonic, ttl_seconds=60, capacity=32):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("launch limits must be positive")
        self._clock = clock
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._tickets = {}
        self._lock = threading.Lock()

    def _prune(self, now):
        for key in list(self._tickets):
            if self._tickets[key] <= now:
                del self._tickets[key]

    def issue(self):
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._tickets) >= self._capacity:
                raise RuntimeError("browser launch grant capacity exhausted")
            ticket = secrets.token_urlsafe(32)
            key = hashlib.sha256(ticket.encode("ascii")).digest()
            self._tickets[key] = now + self._ttl
            return ticket

    def consume(self, ticket):
        if not isinstance(ticket, str) or len(ticket) != 43 or not ticket.isascii():
            return False
        key = hashlib.sha256(ticket.encode("ascii")).digest()
        with self._lock:
            now = self._clock()
            self._prune(now)
            deadline = self._tickets.pop(key, None)
            return deadline is not None and now < deadline

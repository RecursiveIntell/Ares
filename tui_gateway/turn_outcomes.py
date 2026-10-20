"""Bounded, process-local reply projection owned by one existing live session.

This window grants no execution or replay authority. It never reads a transcript.
Callers hold the session history lock and verify the current session object.
"""
from __future__ import annotations

import copy
import json

MAX_TURNS = 8
MAX_FINALIZED = 32
MAX_BYTES = 256 * 1024
STATUSES = frozenset({"complete", "error", "interrupted"})


def unavailable() -> dict:
    return {"version": 1, "scope": "process_local", "availability": "unavailable", "turns": []}


class TurnOutcomeWindow:
    def __init__(self, session: dict, sid: str, *, supervisor=None):
        self.owner_id = id(session)
        self.sid = sid
        self.profile_home = session.get("profile_home")
        self.supervisor = supervisor
        self.turns: list[dict] = []

    def owns(self, session: dict, sid: str) -> bool:
        return (self.owner_id == id(session) and self.sid == sid
                and self.profile_home == session.get("profile_home"))

    def begin(self, request_id: str, route: str, boot_id: str | None) -> dict:
        ref = {"request_id": request_id, "session_id": self.sid,
               "route": route, "host_boot_id": boot_id}
        self.turns.append({"accepted_turn": ref, "state": "running", "finalized": []})
        del self.turns[:-MAX_TURNS]
        return copy.deepcopy(ref)

    def find(self, request_id: str) -> dict | None:
        return next((t for t in self.turns if t["accepted_turn"]["request_id"] == request_id), None)

    def bind_boot(self, request_id: str, boot_id: str | None) -> dict | None:
        turn = self.find(request_id)
        if turn is None or not boot_id:
            return None
        previous = turn["accepted_turn"]["host_boot_id"]
        if previous not in (None, boot_id):
            self.invalidate(turn, "host_replaced")
            return None
        turn["accepted_turn"]["host_boot_id"] = boot_id
        return copy.deepcopy(turn["accepted_turn"])

    @staticmethod
    def invalidate(turn: dict, reason: str) -> None:
        turn.update(state="unavailable", finalized=[], reason=reason)

    def _bound(self, turn: dict) -> None:
        # Never present a silently truncated answer as a complete window.
        if len(turn["finalized"]) > MAX_FINALIZED or self._size(turn) > MAX_BYTES:
            self.invalidate(turn, "payload_limit")
        while len(self.turns) > 1 and sum(self._size(t) for t in self.turns) > MAX_BYTES:
            self.turns.pop(0)

    @staticmethod
    def _size(turn: dict) -> int:
        # Escaped JSON is a conservative byte budget and also handles valid
        # JSON strings containing an unpaired surrogate without raising.
        return len(json.dumps(turn, ensure_ascii=True).encode("ascii"))

    def append(self, request_id: str, payload: dict) -> dict | None:
        turn = self.find(request_id)
        if turn is None or turn["state"] != "running":
            return None
        if not isinstance(payload.get("text"), str) or payload.get("status") not in STATUSES:
            self.invalidate(turn, "invalid_finalized_payload")
            return None
        final = {k: payload[k] for k in ("text", "status", "error", "warning") if k in payload}
        if any(not isinstance(v, str) for v in final.values()):
            self.invalidate(turn, "invalid_finalized_payload")
            return None
        turn["finalized"].append(final)
        self._bound(turn)
        return copy.deepcopy(turn["accepted_turn"])

    def finish(self, request_id: str, *, interrupted=False, error=False, waiting=False) -> None:
        turn = self.find(request_id)
        if turn is None or turn["state"] != "running":
            return
        statuses = {p["status"] for p in turn["finalized"]}
        if error or "error" in statuses:
            turn["state"] = "error"
        elif interrupted or "interrupted" in statuses:
            turn["state"] = "interrupted"
        elif waiting:
            turn["state"] = "waiting"
        elif turn["finalized"]:
            turn["state"] = "complete"
        else:
            self.invalidate(turn, "finalized_payload_unavailable")

    def accept_terminal(self, request_id: str, frame: dict) -> None:
        turn = self.find(request_id)
        if turn is None or turn["state"] != "running":
            return
        outcome = frame.get("turn_outcome")
        if frame.get("reason") == "crash":
            self.invalidate(turn, "host_lost")
        elif frame.get("type") == "turn.error":
            turn["state"] = "error"
        elif (not isinstance(outcome, dict)
              or outcome.get("accepted_turn") != turn["accepted_turn"]):
            self.invalidate(turn, "terminal_projection_unavailable")
        elif outcome.get("state") == "unavailable":
            self.invalidate(turn, str(outcome.get("reason") or "finalized_payload_unavailable"))
        elif outcome.get("state") not in {"complete", "error", "interrupted", "waiting"}:
            self.invalidate(turn, "invalid_terminal_projection")
        elif not isinstance(outcome.get("finalized"), list) or len(outcome["finalized"]) > MAX_FINALIZED:
            self.invalidate(turn, "invalid_terminal_projection")
        else:
            for final in outcome["finalized"]:
                if not isinstance(final, dict) or self.append(request_id, final) is None:
                    self.invalidate(turn, "invalid_terminal_projection")
                    return
            self.finish(request_id, interrupted=bool(frame.get("interrupted")) or outcome["state"] == "interrupted",
                        error=outcome["state"] == "error", waiting=outcome["state"] == "waiting")
        self._bound(turn)

    def snapshot(self, session: dict, sid: str, *, supervisor=None) -> dict:
        if not self.owns(session, sid) or (self.supervisor is not None and supervisor is not self.supervisor):
            return unavailable()
        turns = copy.deepcopy(self.turns)
        for turn in turns:
            ref = turn["accepted_turn"]
            if ref["route"] == "compute_host" and self.supervisor is not None:
                if not ref["host_boot_id"] or ref["host_boot_id"] != self.supervisor.boot_id:
                    self.invalidate(turn, "host_replaced")
        return {"version": 1, "scope": "process_local",
                "availability": "available" if turns else "unavailable", "turns": turns}

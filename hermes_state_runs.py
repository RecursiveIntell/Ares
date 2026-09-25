"""Run-scoped checkpoint persistence owned by SessionDB.

This mixin uses the existing state_meta transaction owner, not a new database,
coordinator, scheduler or filesystem lock. It fences cooperating callers only.
A validated resume is a read result, never a tool/Git/provider authorization.
No external work runs inside a database transaction. Durability is exactly the
SessionDB SQLite/filesystem configuration; no extra power-loss guarantee.
"""
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
from typing import TYPE_CHECKING, Callable, TypeVar

try:  # Match SessionDB's scaffold import boundary without admitting custody.
    import psutil
except ImportError:
    psutil = None

T = TypeVar("T")


class RunCustodyError(RuntimeError):
    """Typed refusal; the stable code is also available independently of prose."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 1_000_000:
        raise RunCustodyError("INVALID_TEXT")
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RunCustodyError("INVALID_DIGEST")
    return value


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise RunCustodyError("INVALID_INTEGER")
    return value


def _run_id(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise RunCustodyError("INVALID_RUN_ID")
    return value


def _ttl(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise RunCustodyError("INVALID_TTL")
    return time.monotonic_ns() + seconds * 1_000_000_000


def _json(value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw) > 4_000_000:
        raise RunCustodyError("CHECKPOINT_TOO_LARGE")
    return raw


def _sha(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunCustodyError("INTEGRITY_DUPLICATE_KEY")
        result[key] = value
    return result


def _load(raw):
    if type(raw) is not str or len(raw) > 4_000_000:
        raise RunCustodyError("INTEGRITY_SIZE")
    try:
        return json.loads(raw, object_pairs_hook=_strict_object)
    except (ValueError, TypeError) as exc:
        raise RunCustodyError("INTEGRITY_JSON") from exc


def _exact_keys(value, cls):
    if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
        raise RunCustodyError("INTEGRITY_SCHEMA")


def _process_identity(pid):
    if psutil is None:
        raise RunCustodyError("PROCESS_INSPECTION_UNAVAILABLE")
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return f"{psutil.boot_time().hex()}:{process.create_time().hex()}"
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, OSError) as exc:
        raise RunCustodyError("PROCESS_UNKNOWN") from exc


def _controller(pid):
    _integer(pid)
    if psutil is None:
        raise RunCustodyError("PROCESS_INSPECTION_UNAVAILABLE")
    # Permit a short-lived command child of the real controller, never an
    # arbitrary caller-chosen unrelated process as an immortal lease holder.
    try:
        lineage = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    except (psutil.Error, OSError) as exc:
        raise RunCustodyError("PROCESS_UNKNOWN") from exc
    if pid not in lineage:
        raise RunCustodyError("CALLER_NOT_CONTROLLER")
    identity = _process_identity(pid)
    if identity is None:
        raise RunCustodyError("PROCESS_GONE")
    return identity


def generation_key(run_id, generation):
    return f"run-custody:{_run_id(run_id)}:generation:{_integer(generation)}"


def _head_key(run_id):
    return f"run-custody:{_run_id(run_id)}:head"


@dataclass(frozen=True)
class RunCheckpoint:
    plan_digest: str
    contract_digest: str
    source_digest: str
    next_action: str
    members: tuple[tuple[str, str], ...]
    unresolved_effects: tuple[str, ...]
    unresolved_findings: tuple[str, ...]
    restrictions: tuple[str, ...]

    def __post_init__(self):
        for name in ("plan_digest", "contract_digest", "source_digest"):
            _digest(getattr(self, name))
        _text(self.next_action)
        for name in ("unresolved_effects", "unresolved_findings", "restrictions"):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > 10000:
                raise RunCustodyError("INVALID_INVENTORY")
            for value in values:
                _text(value)
            if len(set(values)) != len(values):
                raise RunCustodyError("DUPLICATE_INVENTORY")
        if type(self.members) is not tuple or len(self.members) > 10000:
            raise RunCustodyError("INVALID_MEMBERS")
        names = set()
        for member in self.members:
            if type(member) is not tuple or len(member) != 2:
                raise RunCustodyError("INVALID_MEMBER")
            name, raw = member
            _text(name)
            _text(raw)
            if name in names:
                raise RunCustodyError("DUPLICATE_MEMBER")
            names.add(name)
        _json(asdict(self))

    @classmethod
    def from_dict(cls, value):
        _exact_keys(value, cls)
        value = dict(value)
        for name in ("members", "unresolved_effects", "unresolved_findings", "restrictions"):
            if type(value[name]) is not list:
                raise RunCustodyError("INTEGRITY_INVENTORY")
            if name == "members":
                if any(type(member) is not list for member in value[name]):
                    raise RunCustodyError("INTEGRITY_MEMBERS")
                value[name] = tuple(tuple(member) for member in value[name])
            else:
                value[name] = tuple(value[name])
        return cls(**value)


@dataclass(frozen=True)
class RunCustody:
    schema: str
    run_id: str
    generation: int
    predecessor_digest: str | None
    owner_token: str
    controller_pid: int
    process_identity: str
    origin_session_id: str
    current_session_id: str
    historical_goal_digest: str
    expires_monotonic_ns: int
    disposition: str
    checkpoint: RunCheckpoint

    def __post_init__(self):
        if self.schema != "SessionDBRunCustodyV1":
            raise RunCustodyError("INTEGRITY_SCHEMA")
        _run_id(self.run_id)
        _integer(self.generation)
        if self.generation == 1:
            if self.predecessor_digest is not None:
                raise RunCustodyError("INTEGRITY_PREDECESSOR")
        else:
            _digest(self.predecessor_digest)
        _digest(self.owner_token)
        _integer(self.controller_pid)
        for name in ("process_identity", "origin_session_id", "current_session_id"):
            _text(getattr(self, name))
        _digest(self.historical_goal_digest)
        _integer(self.expires_monotonic_ns)
        if self.disposition not in {"active", "released"}:
            raise RunCustodyError("INTEGRITY_DISPOSITION")
        if type(self.checkpoint) is not RunCheckpoint:
            raise RunCustodyError("INVALID_CHECKPOINT")

    @classmethod
    def from_dict(cls, value):
        _exact_keys(value, cls)
        return cls(**{**value, "checkpoint": RunCheckpoint.from_dict(value["checkpoint"])})


def _preserve(old, new):
    # No settlement/authority-change API is invented here. Those owners must
    # supply a separately reviewed transition before obligations can be removed.
    for name in ("unresolved_effects", "unresolved_findings", "restrictions"):
        if not set(getattr(old, name)) <= set(getattr(new, name)):
            raise RunCustodyError("OMITTED_OBLIGATION")
    if not set(dict(old.members)) <= set(dict(new.members)):
        raise RunCustodyError("OMITTED_MEMBER")
    if not set(old.members) <= set(new.members):
        raise RunCustodyError("SUBSTITUTED_MEMBER")
    if old.source_digest != new.source_digest:
        raise RunCustodyError("SOURCE_MISMATCH")
    if old.plan_digest != new.plan_digest or old.contract_digest != new.contract_digest:
        raise RunCustodyError("PLAN_CONTRACT_MISMATCH")


_REFRESH_SCHEMA = "SessionDBRunCustodyRefreshV1"


def _refresh_parts(document):
    """Decode only the explicit compact storage envelope, never widen V1."""
    if (type(document) is not dict or
            set(document) != {"schema", "custody", "checkpoint_reference"} or
            document["schema"] != _REFRESH_SCHEMA):
        raise RunCustodyError("INTEGRITY_REFRESH_SCHEMA")
    metadata, reference = document["custody"], document["checkpoint_reference"]
    if (type(metadata) is not dict or
            set(metadata) != {f.name for f in fields(RunCustody)} - {"checkpoint"} or
            type(reference) is not dict or set(reference) != {"generation", "digest"}):
        raise RunCustodyError("INTEGRITY_REFRESH_SCHEMA")
    _integer(reference["generation"])
    _digest(reference["digest"])
    return metadata, reference


def _decode_run_record(run_id, generation, expected_digest, read_value):
    """Materialize one V1 logical value from a full or versioned compact record.

    Compact records refer directly to a full immutable checkpoint, never to
    another compact record. The immediate predecessor binds that same reference
    and ownership. Each historical read validates its own link without recursive
    expansion of an arbitrarily long refresh chain.
    """
    def load(number, digest):
        raw = read_value(generation_key(run_id, number))
        if raw is None or _sha(raw) != digest:
            raise RunCustodyError("INTEGRITY_MEMBER")
        document = _load(raw)
        if type(document) is not dict:
            raise RunCustodyError("INTEGRITY_SCHEMA")
        return document

    document = load(generation, expected_digest)
    if document.get("schema") == "SessionDBRunCustodyV1":
        value = RunCustody.from_dict(document)
    else:
        metadata, reference = _refresh_parts(document)
        if reference["generation"] >= generation:
            raise RunCustodyError("INTEGRITY_REFRESH_REFERENCE")
        anchor_document = load(reference["generation"], reference["digest"])
        anchor = RunCustody.from_dict(anchor_document)
        if anchor.run_id != run_id or anchor.generation != reference["generation"]:
            raise RunCustodyError("INTEGRITY_REFRESH_REFERENCE")
        # Use validated wire lists, not dataclass tuples: V1's strict decoder
        # must continue rejecting non-JSON inventory shapes.
        checkpoint = anchor_document["checkpoint"]
        value = RunCustody.from_dict({**metadata, "checkpoint": checkpoint})
        prior_document = load(generation - 1, value.predecessor_digest)
        if prior_document.get("schema") == "SessionDBRunCustodyV1":
            prior = RunCustody.from_dict(prior_document)
            prior_reference = {"generation": generation - 1, "digest": value.predecessor_digest}
        else:
            prior_metadata, prior_reference = _refresh_parts(prior_document)
            prior = RunCustody.from_dict({**prior_metadata, "checkpoint": checkpoint})
        if (prior_reference != reference or prior.run_id != run_id or
                prior.generation != generation - 1 or value.disposition != "active"):
            raise RunCustodyError("INTEGRITY_REFRESH_REFERENCE")
        for field in fields(RunCustody):
            if field.name not in {"generation", "predecessor_digest", "expires_monotonic_ns"}:
                if getattr(prior, field.name) != getattr(value, field.name):
                    raise RunCustodyError("INTEGRITY_REFRESH_OWNERSHIP")
    if value.run_id != run_id or value.generation != generation:
        raise RunCustodyError("INTEGRITY_IDENTITY")
    return value


class SessionRunCustodyMixin:
    """Typed run APIs on SessionDB's existing transaction/key-value owner."""

    if TYPE_CHECKING:
        # Host interface only; SessionDB provides both concrete operations.
        def get_meta(self, key: str) -> str | None: ...
        def _execute_write(self, fn: Callable[[sqlite3.Connection], T],
                           patience_s: float | None = None) -> T: ...
        def _session_turn_lease_key_on_conn(self, conn: sqlite3.Connection,
                                            session_id: str) -> str: ...

    def _read_run_head(self, run_id):
        raw = self.get_meta(_head_key(run_id))
        if raw is None:
            return None, None
        head = _load(raw)
        if type(head) is not dict or set(head) != {"generation", "digest"}:
            raise RunCustodyError("INTEGRITY_HEAD")
        _integer(head["generation"])
        _digest(head["digest"])
        # The immutable member can be read after releasing the head read lock:
        # a legitimate publisher never changes/removes an existing generation.
        value = self._read_run_member(run_id, head["generation"], head["digest"])
        return raw, value

    def _read_run_member(self, run_id, generation, expected_digest):
        return _decode_run_record(run_id, generation, expected_digest, self.get_meta)

    def read_run_custody(self, run_id):
        """Read committed native metadata; does not authorize continuation."""
        return self._read_run_head(run_id)[1]

    def list_run_custody_for_session(self, session_id, *, active_only=True):
        """Read custody heads bound to one physical session; grants no authority."""
        _text(session_id)
        if type(active_only) is not bool:
            raise RunCustodyError("INVALID_FILTER")
        values = []
        for key, _raw in self.list_meta_prefix("run-custody:"):
            match = re.fullmatch(
                r"run-custody:([A-Za-z0-9][A-Za-z0-9_.-]{0,127}):head",
                key,
            )
            if match is None:
                continue
            value = self.read_run_custody(match.group(1))
            if value is None or value.current_session_id != session_id:
                continue
            if active_only and value.disposition != "active":
                continue
            values.append(value)
        values.sort(key=lambda value: value.run_id)
        return values

    def read_run_checkpoint(self, run_id, *, generation):
        _integer(generation)
        value = self.read_run_custody(run_id)
        if value is None or generation > value.generation:
            raise RunCustodyError("GENERATION_NOT_FOUND")
        if value.generation - generation > 10000:
            raise RunCustodyError("HISTORY_BOUND_EXCEEDED")
        while value.generation > generation:
            value = self._read_run_member(run_id, value.generation - 1, value.predecessor_digest)
        return value

    def _check_run_claim_bindings(self, conn, value, lease_holder):
        """Native checks on the admitted connection, not external authority."""
        session = conn.execute("SELECT ended_at,end_reason FROM sessions WHERE id=?",
                               (value.current_session_id,)).fetchone()
        if session is None:
            raise RunCustodyError("SESSION_MISMATCH")
        if session["ended_at"] is not None or session["end_reason"] is not None:
            raise RunCustodyError("SESSION_NOT_CURRENT")
        root = self._session_turn_lease_key_on_conn(conn, value.current_session_id)
        lease = conn.execute("SELECT holder,expires_at FROM session_turn_leases "
                             "WHERE conversation_id=?", (root,)).fetchone()
        if lease is None or lease["holder"] != lease_holder:
            raise RunCustodyError("LEASE_MISMATCH")
        try:
            expiry = float(lease["expires_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise RunCustodyError("LEASE_MISMATCH") from exc
        if not math.isfinite(expiry) or expiry <= time.time():
            raise RunCustodyError("LEASE_MISMATCH")
        goal_key = dict(value.checkpoint.members).get("historical-goal-key")
        if not goal_key:
            raise RunCustodyError("HISTORICAL_GOAL_BINDING_MISSING")
        goal = conn.execute("SELECT value FROM state_meta WHERE key=?", (goal_key,)).fetchone()
        if goal is None or type(goal[0]) is not str or _sha(goal[0]) != value.historical_goal_digest:
            raise RunCustodyError("HISTORICAL_GOAL_MISMATCH")

    def _commit_run(self, expected_head, value, *, require_live_owner=True,
                    predecessor_expires_ns=None, claim_lease_holder=None,
                    refresh_reference=None, validate_conn=None):
        document = asdict(value)
        if refresh_reference is not None:
            document.pop("checkpoint")
            document = {"schema": _REFRESH_SCHEMA, "custody": document,
                        "checkpoint_reference": refresh_reference}
        raw = _json(document)
        head = _json({"generation": value.generation, "digest": _sha(raw)})
        key = _head_key(value.run_id)
        member_key = generation_key(value.run_id, value.generation)

        def write(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if (None if row is None else row[0]) != expected_head:
                raise RunCustodyError("FENCE_MISMATCH")
            # Recheck after waiting for SQLite admission, not only before it.
            if require_live_owner:
                if predecessor_expires_ns is not None and time.monotonic_ns() >= predecessor_expires_ns:
                    raise RunCustodyError("OWNER_EXPIRED")
                if _controller(value.controller_pid) != value.process_identity:
                    raise RunCustodyError("STALE_PROCESS")
                if time.monotonic_ns() >= value.expires_monotonic_ns:
                    raise RunCustodyError("OWNER_EXPIRED")
            if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (member_key,)).fetchone():
                raise RunCustodyError("INTEGRITY_GENERATION_EXISTS")
            if claim_lease_holder is not None:
                self._check_run_claim_bindings(conn, value, claim_lease_holder)
            if validate_conn is not None:
                validate_conn(conn)
            if refresh_reference is not None:
                def read_value(record_key):
                    if record_key == member_key:
                        return raw
                    row = conn.execute("SELECT value FROM state_meta WHERE key=?", (record_key,)).fetchone()
                    return None if row is None else row[0]
                if _decode_run_record(value.run_id, value.generation, _sha(raw), read_value) != value:
                    raise RunCustodyError("INTEGRITY_REFRESH_VALUE")
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (member_key, raw))
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, head))
        self._execute_write(write)
        return value

    def claim_run_custody(self, run_id, *, expected_generation, checkpoint,
                          origin_session_id, current_session_id,
                          historical_goal_digest, ttl_seconds=300, controller_pid=None):
        """Low-level metadata claim; does not establish session/goal bindings."""
        raw, value = self._prepare_run_claim(run_id, expected_generation=expected_generation,
            checkpoint=checkpoint, origin_session_id=origin_session_id,
            current_session_id=current_session_id, historical_goal_digest=historical_goal_digest,
            ttl_seconds=ttl_seconds, controller_pid=controller_pid)
        return self._commit_run(raw, value)

    def claim_run_custody_checked(self, run_id, *, lease_holder, expected_generation,
                                  checkpoint, origin_session_id, current_session_id,
                                  historical_goal_digest, ttl_seconds=300, controller_pid=None):
        """Claim with native session/lease/goal checks in the commit transaction.

        The historical-goal key comes from the checkpoint's bound member.
        Caller-observed file digests and goal semantics are not verified here.
        This is not downstream effect authorization or filesystem atomicity;
        other mutation methods remain low-level custody primitives.
        """
        _text(lease_holder)  # Invalid/missing bindings must never disable checks.
        raw, value = self._prepare_run_claim(run_id, expected_generation=expected_generation,
            checkpoint=checkpoint, origin_session_id=origin_session_id,
            current_session_id=current_session_id, historical_goal_digest=historical_goal_digest,
            ttl_seconds=ttl_seconds, controller_pid=controller_pid)
        return self._commit_run(raw, value, claim_lease_holder=lease_holder)

    def _prepare_run_claim(self, run_id, *, expected_generation, checkpoint,
                           origin_session_id, current_session_id,
                           historical_goal_digest, ttl_seconds, controller_pid):
        _integer(expected_generation, 0)
        if type(checkpoint) is not RunCheckpoint:
            raise RunCustodyError("INVALID_CHECKPOINT")
        pid = os.getpid() if controller_pid is None else controller_pid
        identity = _controller(pid)
        raw, old = self._read_run_head(run_id)
        if (0 if old is None else old.generation) != expected_generation:
            raise RunCustodyError("FENCE_MISMATCH")
        if old is not None:
            if old.disposition == "active" and _process_identity(old.controller_pid) == old.process_identity:
                # Expiry alone never grants a competing writer takeover.
                raise RunCustodyError("OWNER_ACTIVE")
            if (origin_session_id != old.origin_session_id or
                    historical_goal_digest != old.historical_goal_digest):
                raise RunCustodyError("HISTORY_MISMATCH")
            if checkpoint != old.checkpoint:
                raise RunCustodyError("TAKEOVER_REQUIRES_EXACT_CHECKPOINT")
        value = RunCustody("SessionDBRunCustodyV1", run_id, expected_generation + 1,
            None if old is None else _load(raw)["digest"], secrets.token_hex(32), pid,
            identity, origin_session_id, current_session_id, historical_goal_digest,
            _ttl(ttl_seconds), "active", checkpoint)
        return raw, value

    def _owned_run(self, run_id, owner_token, expected_generation, *, allow_expired=False):
        _integer(expected_generation)
        raw, old = self._read_run_head(run_id)
        if old is None or old.generation != expected_generation:
            raise RunCustodyError("FENCE_MISMATCH")
        if old.disposition != "active" or not secrets.compare_digest(old.owner_token, owner_token):
            raise RunCustodyError("STALE_OWNER")
        if _controller(old.controller_pid) != old.process_identity:
            raise RunCustodyError("STALE_PROCESS")
        if not allow_expired and time.monotonic_ns() >= old.expires_monotonic_ns:
            raise RunCustodyError("OWNER_EXPIRED")
        return raw, old

    def publish_run_checkpoint(self, run_id, *, owner_token, expected_generation,
                               expected_source_digest, checkpoint, ttl_seconds=300):
        raw, old = self._owned_run(run_id, owner_token, expected_generation)
        if expected_source_digest != old.checkpoint.source_digest:
            raise RunCustodyError("SOURCE_MISMATCH")
        if type(checkpoint) is not RunCheckpoint:
            raise RunCustodyError("INVALID_CHECKPOINT")
        _preserve(old.checkpoint, checkpoint)
        value = replace(old, generation=old.generation + 1, predecessor_digest=_load(raw)["digest"],
                        checkpoint=checkpoint, expires_monotonic_ns=_ttl(ttl_seconds))
        return self._commit_run(raw, value, predecessor_expires_ns=old.expires_monotonic_ns)

    def transition_run_source(self, run_id, *, owner_token, expected_generation,
                              expected_source_digest, new_source_digest,
                              observation_ref, observation_digest, ttl_seconds=300):
        """Record an explicit caller-observed source change, not permission.

        The opaque observation reference is bounded text; its digest is bound
        but not fetched or verified here. The controller must independently
        observe the old/new source and verify the referenced evidence. Ordinary
        publication and resume checks retain their strict source equality.
        """
        raw, old = self._owned_run(run_id, owner_token, expected_generation)
        if expected_source_digest != old.checkpoint.source_digest:
            raise RunCustodyError("SOURCE_MISMATCH")
        _digest(new_source_digest)
        _text(observation_ref)
        _digest(observation_digest)
        if new_source_digest == expected_source_digest:
            raise RunCustodyError("SOURCE_UNCHANGED")
        member = (f"source-transition:{old.generation + 1}", _json({
            "schema": "RunSourceTransitionV1",
            "old_source_digest": expected_source_digest,
            "new_source_digest": new_source_digest,
            "observation_ref": observation_ref,
            "observation_digest": observation_digest,
        }))
        # Construct, never accept, the new checkpoint: every other field is
        # retained exactly. Duplicate names are refused by RunCheckpoint.
        checkpoint = replace(old.checkpoint, source_digest=new_source_digest,
                             members=old.checkpoint.members + (member,))
        value = replace(old, generation=old.generation + 1,
                        predecessor_digest=_load(raw)["digest"], checkpoint=checkpoint,
                        expires_monotonic_ns=_ttl(ttl_seconds))
        return self._commit_run(raw, value, predecessor_expires_ns=old.expires_monotonic_ns)

    def transfer_run_session(self, run_id, *, owner_token, expected_generation,
                             expected_session_id, new_session_id, ttl_seconds=300):
        """Fence one live custody owner onto a published continuation session.

        This retains the same checkpoint, historical binding, owner token and
        process identity. It is not a release/reclaim and grants no downstream
        effect authority. Both old and new session identities are checked in
        the same SessionDB write transaction that advances custody generation.
        """
        _text(expected_session_id)
        _text(new_session_id)
        raw, old = self._owned_run(run_id, owner_token, expected_generation)
        if old.current_session_id != expected_session_id:
            raise RunCustodyError("SESSION_MISMATCH")
        value = replace(
            old,
            generation=old.generation + 1,
            predecessor_digest=_load(raw)["digest"],
            current_session_id=new_session_id,
            expires_monotonic_ns=_ttl(ttl_seconds),
        )

        def validate(conn):
            prior = conn.execute(
                "SELECT ended_at,end_reason FROM sessions WHERE id=?",
                (expected_session_id,),
            ).fetchone()
            child = conn.execute(
                "SELECT ended_at,parent_session_id,model_config FROM sessions WHERE id=?",
                (new_session_id,),
            ).fetchone()
            if (
                prior is None
                or prior["ended_at"] is None
                or prior["end_reason"] not in ("compression", "context_rebase")
                or child is None
                or child["ended_at"] is not None
                or child["parent_session_id"] != expected_session_id
            ):
                raise RunCustodyError("SESSION_TRANSITION_MISMATCH")
            if prior["end_reason"] == "context_rebase":
                try:
                    config = json.loads(child["model_config"] or "{}")
                except (TypeError, ValueError) as exc:
                    raise RunCustodyError("SESSION_TRANSITION_MISMATCH") from exc
                if (
                    type(config) is not dict
                    or config.get("_context_rebase_from") != expected_session_id
                ):
                    raise RunCustodyError("SESSION_TRANSITION_MISMATCH")

        return self._commit_run(
            raw,
            value,
            predecessor_expires_ns=old.expires_monotonic_ns,
            validate_conn=validate,
        )

    def refresh_run_custody(self, run_id, *, owner_token, expected_generation, ttl_seconds=300):
        raw, old = self._owned_run(run_id, owner_token, expected_generation)
        member = self.get_meta(generation_key(run_id, old.generation))
        old_digest = _load(raw)["digest"]
        if member is None or _sha(member) != old_digest:
            raise RunCustodyError("INTEGRITY_MEMBER")
        document = _load(member)
        if document.get("schema") == "SessionDBRunCustodyV1":
            reference = {"generation": old.generation, "digest": old_digest}
        else:
            _, reference = _refresh_parts(document)
        value = replace(old, generation=old.generation + 1, predecessor_digest=old_digest,
                        expires_monotonic_ns=_ttl(ttl_seconds))
        return self._commit_run(raw, value, predecessor_expires_ns=old.expires_monotonic_ns,
                                refresh_reference=reference)

    def release_run_custody(self, run_id, *, owner_token, expected_generation):
        raw, old = self._owned_run(run_id, owner_token, expected_generation, allow_expired=True)
        value = replace(old, generation=old.generation + 1, predecessor_digest=_load(raw)["digest"],
                        disposition="released")
        return self._commit_run(raw, value, require_live_owner=False)

    def validate_run_resume(self, run_id, *, owner_token, expected_generation,
                            source_digest, plan_digest, contract_digest):
        """Caller must independently observe inputs; this performs no effects."""
        _raw, old = self._owned_run(run_id, owner_token, expected_generation)
        if source_digest != old.checkpoint.source_digest:
            raise RunCustodyError("SOURCE_MISMATCH")
        if (plan_digest, contract_digest) != (old.checkpoint.plan_digest, old.checkpoint.contract_digest):
            raise RunCustodyError("PLAN_CONTRACT_MISMATCH")
        return old

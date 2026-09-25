"""Cold inbox work driven by the gateway's existing startup/reconnect timer."""
from __future__ import annotations

import asyncio
import time
import weakref

from gateway.context_input import GatewayContextInput, input_authorizer
from hermes_state_continuity import ContextContinuationError


def schedule_input_recovery(runner, platform=None):
    """Native discovery is not execution authority; ordinary turn owns custody."""
    from gateway.run import _profile_runtime_scope, logger
    from hermes_state_runs import _process_identity

    scheduled, enrolled = 0, set()
    tasks = getattr(runner, "_context_input_wake_tasks", None)
    if tasks is None:
        tasks = runner._context_input_wake_tasks = {}
    try:
        with runner.session_store._lock:
            runner.session_store._ensure_loaded_locked()
            entries = tuple(runner.session_store._entries.values())
    except Exception:
        return scheduled, enrolled
    for entry in entries:
        if entry.origin is None or platform is not None and entry.origin.platform != platform:
            continue
        try:
            with _profile_runtime_scope(runner._resolve_profile_home_for_source(entry.origin)):
                db = runner.session_store._db
                if not callable(getattr(type(db), "context_dispatch_required_for_session", None)):
                    continue
                if not db.context_dispatch_required_for_session(entry.session_id):
                    continue
                enrolled.add(entry.session_key)
                if entry.suspended or runner._is_session_running(entry.session_key) or entry.session_key in tasks:
                    continue
                work = db.read_context_input_work(entry.session_id)
                receipts, phase = work["receipts"], work["phase"]
                if not receipts:
                    continue
                if phase is not None and phase["state"] != "answered":
                    if phase["dispatch_attempts"] or phase["attempts"] >= 3 or time.time() >= phase["deadline_at"]:
                        continue
                    if phase["state"] == "active" and _process_identity(phase["controller_pid"]) == phase["process_identity"]:
                        continue
                origin = db.read_context_input_origin(entry.session_id, receipts[-1])
                if origin is None or not origin["cold_recoverable"]:
                    continue
                authorize = input_authorizer(runner, db, entry.session_key)
                authorize(entry.session_id, receipts)
                # A timer does not extend the construction budget, and a
                # successful reservation does not bypass the turn lease.
                db.reserve_context_input_wake(entry.session_id, receipt=receipts[-1])
                task = asyncio.create_task(_run_input_recovery(runner, db, entry.session_key,
                    entry.session_id, receipts[-1], origin, authorize))
                tasks[entry.session_key] = task
                task.add_done_callback(lambda finished, key=entry.session_key: tasks.pop(key, None))
                runner._background_tasks.add(task)
                task.add_done_callback(runner._background_tasks.discard)
                if getattr(runner, "_startup_restore_in_progress", False):
                    if getattr(runner, "_startup_restore_tasks", None) is None:
                        runner._startup_restore_tasks = []
                    runner._startup_restore_tasks.append(task)
                scheduled += 1
        except Exception as exc:
            logger.warning("Native input recovery parked for %s: %s", entry.session_key,
                getattr(exc, "code", type(exc).__name__))
    return scheduled, enrolled


async def _run_input_recovery(runner, db, session_key, session_id, receipt, origin, authorize):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.run import logger

    try:
        # Recheck after the scheduling await; source.origin may have changed.
        work = await asyncio.to_thread(db.read_context_input_work, session_id)
        await asyncio.to_thread(authorize, session_id, tuple(r for r in work["receipts"] if r.sequence <= receipt.sequence))
        source = SessionSource(**dict(origin["route"], platform=Platform(origin["route"]["platform"])))
        owner = origin["transport_profile"]
        adapters = (runner.adapters if owner == "transport:primary"
            else getattr(runner, "_profile_adapters", {}).get(owner, {}))
        adapter = adapters.get(source.platform)
        if adapter is None:
            raise ContextContinuationError("GATEWAY_INPUT_TRANSPORT_UNAVAILABLE")
        source._transport_adapter_ref = weakref.ref(adapter)
        event = MessageEvent(text=receipt.content, message_type=MessageType.TEXT, source=source,
            allow_gateway_control=False,
            metadata={"gateway_session_key": session_key, "gateway_session_id": session_id,
                "gateway_session_strict": True})
        event._context_input = GatewayContextInput(receipt, session_key, session_id, authorize)
        event._context_original_text = receipt.content
        event._context_pre_dispatch_applied = True
        event._hermes_startup_restore_replay = True
        await runner._run_startup_resume_event(adapter, event, session_key)
    except Exception as exc:
        logger.warning("Native input wake parked for %s: %s", session_key, getattr(exc, "code", type(exc).__name__))

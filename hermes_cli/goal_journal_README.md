# Goal Deferred-Write Journal (F0.2 of the goal-loop fix plan)

Per-PID, append-only. One JSONL file per process that could not persist a
goal write because the SessionDB was not available yet:

    <hermes_home>/goal-deferred/<pid>.jsonl

Line records (one JSON object each):

    {"kind": "write", "id": "<uuid>", "session_id": "...", "payload": "<goal raw json>", "at": <epoch>}
    {"kind": "tombstone", "id": "<uuid>"}         # that write is now durable (or was superseded)

Rules:
- Append-only — a drained line is tombstoned, never deleted, so concurrent
  processes never rewrite each other's file. Only a process's OWN file has
  its tombstones appended, and only by that same process.
- After a process dies, the NEXT gateway process with a healthy SessionDB
  drains dead-PID journals (the owning process is gone; nobody else will).
- Compaction (dropping fully-tombstoned files) is a maintenance command,
  never a turn path.

The old in-memory `_DEFERRED_GOAL_WRITES` buffer died with the process
(SIGKILL/crash-loop lost every buffered set); this journal survives it.

## Summary
Fix for [Bounty $6k] #3650 — Ensure ack timeout exceeds visibility — worker protocol settings

## Root cause
The `PayloadValidator` class did not enforce the worker protocol settings invariant that `ack_timeout` must exceed `visibility`. This allowed jobs to be duplicated, starved, lost, or delivered to workers under wrong conditions.

## Fix
- Added `WorkerProtocolError` exception for worker protocol violations
- Added `set_worker_protocol_settings(ack_timeout, visibility)` to configure the invariant
- Added `validate_worker_protocol(task)` to enforce `ack_timeout > visibility` at claim/enqueue/ack time
- Enforced the check in `dequeue()` (claim transaction) and `complete()` (ack transaction)
- Retries remain idempotent — no protocol settings configured = no validation

## Changes
- `src/orchestrator/scheduler.py`: Added `WorkerProtocolError`, `set_worker_protocol_settings()`, `validate_worker_protocol()`, enforcement in `dequeue()` and `complete()`
- `tests/test_scheduler.py`: Added `TestWorkerProtocolSettings` with 10 deterministic regression tests

## Testing
All 15 scheduler tests pass.

Closes #3650
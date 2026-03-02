# Scratchpad

## Current Goal
- [x] Fix: Trap signals instead of forwarding to Claude CLI (#10)

## Next Goals
- [ ] (none queued)

## Blockers
None

## Notes
- `start_new_session=True` added to all three `subprocess.Popen` calls in
  `loony_dev/agents/planning.py` and `loony_dev/agents/coding.py`.
- Signal isolation tests added in `loony_dev/tests/test_agent_signal_isolation.py`.

## Last Spec Read
2026-03-02

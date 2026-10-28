# PHASE 7 Follow-up Review

Reviewed commit `32896fdb831033eeee5a55e0ffd3c8c4f0203b39` against Phase 7 in `.agents/tasks/plan.md`, R13/R16.6 in the JobPilot specs, and `docs/decisions.md`. No prior Phase 7 review was present.

The implementation meets the approved behavior:

- `compute_follow_up_at` adds the configured `reminder_after_days` (`src/jobpilot/pipeline/follow_up.py:85-92`), whose default is 7 (`src/jobpilot/config.py:147-148`). Both current production application-recording paths persist that value (`src/jobpilot/pipeline/run.py:1008-1009`, `src/jobpilot/pipeline/run.py:1138-1139`).
- The Neon query selects only pending, due, not-yet-reminded applications (`src/jobpilot/storage/neon.py:501-506`). Delivery and reminder bookkeeping occur in `send_due_reminders` (`src/jobpilot/pipeline/follow_up.py:95-119`), backed by migration 004 (`migrations/004_follow_up.sql:10`). Outcome-bearing applications are excluded.
- Telegram parses `/entrevista id` and `/rechazo id` into the intended commands (`src/jobpilot/channels/telegram.py:217-219`). Dispatch maps them to `interview` and `rejected` (`src/jobpilot/pipeline/run.py:1319-1322`), and persistence updates the matching job's application outcome (`src/jobpilot/storage/neon.py:527-531`).
- `send_weekly_report` checks the configured ISO weekday, sends through the notifier, and records the sent date to prevent another report that day (`src/jobpilot/pipeline/follow_up.py:154-182`). `run_once` invokes reminders and the weekly report after Telegram update processing (`src/jobpilot/pipeline/run.py:1466-1467`).
- New owner-facing reminder, outcome-confirmation, and report text is Spanish (`src/jobpilot/pipeline/follow_up.py:145-150`, `src/jobpilot/pipeline/follow_up.py:187-210`).
- Required tests cover default/configured date calculation, one-time and outcome-filtered reminders, both command transitions, weekday gating, duplicate report prevention, and report content (`tests/test_follow_up.py:66-289`).

Verification evidence was present in the Phase 7 commit message: `uv run ruff check .` passed, `ruff format --check .` passed, `mypy .` succeeded for 102 files, and `pytest` reported 251 passed and 9 skipped because local WeasyPrint native libraries were absent. No suite was rerun. A narrow `git diff --check` spot-check passed.

Blocking findings: none.

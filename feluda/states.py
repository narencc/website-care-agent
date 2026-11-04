"""Job state machine. Application code is the only writer of job status."""

from __future__ import annotations

JOB_STATES = (
    "draft",
    "needs_review",
    "ready",
    "with_builder",
    "queued",
    "verifying",
    "passed",
    "failed",
    "needs_human",
    "verification_error",
)

# Edits that bump the revision may send any non-draft job back to needs_review;
# that path is handled by ``revise`` and is listed here too so it is validated.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"needs_review"},
    "needs_review": {"ready", "needs_human", "needs_review"},
    "ready": {"with_builder", "needs_review", "needs_human"},
    "with_builder": {"queued", "needs_review", "needs_human"},
    "queued": {"verifying", "verification_error", "needs_review"},
    "verifying": {"passed", "failed", "needs_human", "verification_error", "queued", "needs_review"},
    "passed": {"with_builder", "needs_review"},  # invalidated by a shared-component change or final gate
    "failed": {"with_builder", "needs_review"},
    "needs_human": {"needs_review", "with_builder", "failed"},  # a human may never mark it passed
    "verification_error": {"queued", "needs_review", "needs_human"},
}

# Jobs in these states hold an open builder edit on the site.
ACTIVE_EDIT_STATES = {"with_builder", "queued", "verifying"}


class TransitionError(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise TransitionError(f"job cannot move from {current!r} to {new!r}")


# Request-level statuses shown in the queue.
REQUEST_STATES = ("transcribing", "awaiting_transcript", "parsing", "plan_failed", "needs_review", "with_builder", "verifying", "needs_human", "ready_to_close", "closing", "done")


def derive_request_status(job_statuses: list[str], *, parsing: bool, closing: bool, closed: bool) -> str:
    if parsing:
        return "parsing"
    if closed:
        return "done"
    if closing:
        return "closing"
    if not job_statuses:
        return "needs_review"
    s = set(job_statuses)
    if s & {"needs_human", "verification_error"}:
        return "needs_human"
    if s & {"draft", "needs_review"}:
        return "needs_review"
    if s & {"queued", "verifying"}:
        return "verifying"
    if s <= {"passed"}:
        return "ready_to_close"  # waiting on the final consistency gate
    return "with_builder"

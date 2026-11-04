"""Durable database-backed task queue and worker.

Every task has a unique idempotency key, a timeout (lease), a bounded number
of executions and a recorded outcome. Enqueueing an existing key returns the
existing task, so duplicate clicks and HTTP retries never create a second
verification. Expired leases are reclaimed as a failed execution. Postgres
claims use ``FOR UPDATE SKIP LOCKED``; SQLite runs a single worker.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import redact
from .db import is_postgres, new_id, session, utcnow
from .records import Task
from .settings import app_settings

log = logging.getLogger("feluda.tasks")


class PermanentError(RuntimeError):
    """Do not retry: the input is wrong, not the infrastructure."""


def enqueue(
    s: Session,
    kind: str,
    key: str,
    payload: dict[str, Any],
    *,
    attempt_id: str | None = None,
    max_executions: int | None = None,
    timeout_s: int | None = None,
) -> Task:
    existing = s.scalar(select(Task).where(Task.idempotency_key == key))
    if existing:
        return existing
    cfg = app_settings()
    task = Task(
        id=new_id("task"),
        kind=kind,
        idempotency_key=key,
        payload=payload,
        attempt_id=attempt_id,
        max_executions=max_executions or cfg.task_max_executions,
        timeout_s=timeout_s or cfg.task_lease_s,
    )
    try:
        with s.begin_nested():
            s.add(task)
            s.flush()
    except IntegrityError:  # raced with another enqueue of the same key
        return s.scalar(select(Task).where(Task.idempotency_key == key))
    return task


def requeue(s: Session, task: Task, *, extra_executions: int = 1) -> Task:
    """Resume the same logical task (same attempt) with a fresh execution budget."""
    task.status = "queued"
    task.max_executions = task.executions + extra_executions
    task.run_after = utcnow()
    task.locked_by = None
    task.locked_until = None
    return task


def _reclaim_expired(s: Session) -> list[Task]:
    dead = []
    for t in s.scalars(select(Task).where(Task.status == "running", Task.locked_until < utcnow())):
        t.last_error = (t.last_error + "\nlease expired (timeout)").strip()
        t.locked_by = None
        if t.executions >= t.max_executions:
            t.status = "dead"
            dead.append(t)
        else:
            t.status = "queued"
    return dead


def claim(worker_id: str) -> Task | None:
    with session() as s:
        dead = _reclaim_expired(s)
        for t in dead:
            _on_dead(s, t)
        q = select(Task).where(Task.status == "queued", Task.run_after <= utcnow()).order_by(Task.created_at).limit(1)
        if is_postgres():
            q = q.with_for_update(skip_locked=True)
        task = s.scalar(q)
        if task is None:
            return None
        task.status = "running"
        task.executions += 1
        task.locked_by = worker_id
        task.locked_until = utcnow() + timedelta(seconds=task.timeout_s)
        s.flush()
        s.expunge(task)
        return task


HANDLERS: dict[str, Callable[[Task], dict[str, Any] | None]] = {}
DEAD_HANDLERS: dict[str, Callable[[Session, Task], None]] = {}


def handler(kind: str, *, on_dead: Callable[[Session, Task], None] | None = None):
    def deco(fn):
        HANDLERS[kind] = fn
        if on_dead:
            DEAD_HANDLERS[kind] = on_dead
        return fn

    return deco


def _on_dead(s: Session, task: Task) -> None:
    fn = DEAD_HANDLERS.get(task.kind)
    if fn:
        try:
            fn(s, task)
        except Exception:  # pragma: no cover - defensive
            log.exception("dead handler for %s failed", task.id)


def run_one(worker_id: str) -> bool:
    from . import pipeline  # noqa: F401  (registers handlers)

    task = claim(worker_id)
    if task is None:
        return False
    fn = HANDLERS.get(task.kind)
    try:
        if fn is None:
            raise PermanentError(f"no handler for task kind {task.kind!r}")
        outcome = fn(task) or {}
        with session() as s:
            t = s.get(Task, task.id)
            t.status, t.outcome, t.locked_by, t.locked_until = "succeeded", outcome, None, None
        return True
    except Exception as e:
        err = redact.for_logs(f"{type(e).__name__}: {e}")
        log.warning("task %s (%s) failed: %s", task.id, task.kind, err)
        with session() as s:
            t = s.get(Task, task.id)
            t.last_error = (t.last_error + "\n" + err + "\n" + redact.for_logs(traceback.format_exc(limit=3)))[-4000:]
            t.locked_by, t.locked_until = None, None
            if isinstance(e, PermanentError) or t.executions >= t.max_executions:
                t.status = "dead"
                t.outcome = {"error": err}
                _on_dead(s, t)
            else:
                t.status = "queued"
                t.run_after = utcnow() + timedelta(seconds=min(60, 2 ** t.executions))
        return True


def run_until_idle(max_tasks: int = 100, worker_id: str = "inline") -> int:
    """Drain the queue in this thread (tests and scripts). Returns tasks run."""
    n = 0
    while n < max_tasks:
        with session() as s:
            pending = s.scalar(select(Task).where(Task.status.in_(("queued", "running"))).order_by(Task.run_after).limit(1))
            if pending is None:
                break
            if pending.status == "queued" and pending.run_after > utcnow():
                wait = (pending.run_after - utcnow()).total_seconds()
            else:
                wait = 0
        if wait > 0:
            time.sleep(min(wait, 5))
        if run_one(worker_id):
            n += 1
        elif wait <= 0:
            time.sleep(0.2)
    from .pipeline import close_thread_capturer

    close_thread_capturer()
    return n


_last_scheduled_day: str | None = None


def schedule_daily() -> None:
    """Enqueue once-a-day maintenance (evidence retention); the key makes it once per day."""
    global _last_scheduled_day
    day = utcnow().strftime("%Y-%m-%d")
    if day == _last_scheduled_day:
        return
    with session() as s:
        enqueue(s, "retention", f"retention:{day}", {}, max_executions=2)
    _last_scheduled_day = day


def reclaim_host_tasks() -> int:
    """On worker start, resume tasks a previous worker process on this host left running.

    The prototype runs one worker per host, so a running task locked by this
    host belongs to a process that has exited (restart or crash); without this
    it would wait for its lease to expire. Counts as a failed execution.
    """
    prefix = f"{socket.gethostname()}:"
    with session() as s:
        stale = list(s.scalars(select(Task).where(Task.status == "running", Task.locked_by.like(prefix + "%"))))
        for t in stale:
            t.locked_until = utcnow()  # let the normal expiry path requeue or bury it
        dead = _reclaim_expired(s)
        for t in dead:
            _on_dead(s, t)
    return len(stale)


class Worker(threading.Thread):
    def __init__(self, poll_s: float = 1.0):
        super().__init__(daemon=True, name="feluda-worker")
        self.poll_s = poll_s
        self.stop_event = threading.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{self.name}"

    def run(self):
        from .pipeline import close_thread_capturer

        reclaim_host_tasks()
        try:
            while not self.stop_event.is_set():
                try:
                    schedule_daily()
                    if not run_one(self.worker_id):
                        self.stop_event.wait(self.poll_s)
                except Exception:  # pragma: no cover - keep the worker alive
                    log.exception("worker loop error")
                    self.stop_event.wait(self.poll_s)
        finally:
            close_thread_capturer()

    def stop(self):
        self.stop_event.set()


def main():  # python -m feluda.tasks
    from .db import init_db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()
    w = Worker()
    log.info("worker %s started", w.worker_id)
    w.run()


if __name__ == "__main__":
    main()

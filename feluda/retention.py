"""Evidence retention: delete screenshots and uploads after FELUDA_RETENTION_DAYS (default 90).

Files of a request are deleted once it has been closed, or it and all its
jobs have been untouched, for longer than the retention period. Evidence rows keep their metadata (verdicts,
check results, hashes, timestamps) with ``purged_at`` set, so the audit trail
survives while the images and attachments are gone. Runs daily from the
worker, or on demand:

    python -m feluda.retention [--days 90] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from . import storage
from .db import init_db, session, utcnow
from .records import Evidence, ImageDraft, Job, Request


def retention_days() -> int:
    return int(os.environ.get("FELUDA_RETENTION_DAYS") or 90)


def _delete(key: str | None) -> int:
    if not key:
        return 0
    try:
        path = storage.path_of(key)
    except storage.StorageError:
        return 0
    if path.exists():
        path.unlink()
        return 1
    return 0


def purge(days: int | None = None, *, dry_run: bool = False, now=None) -> dict[str, Any]:
    days = retention_days() if days is None else days
    cutoff = (now or utcnow()) - timedelta(days=days)
    out = {"days": days, "cutoff": cutoff.isoformat(), "requests": 0, "evidence_rows": 0, "files_deleted": 0, "dry_run": dry_run}
    with session() as s:
        expired = list(
            s.scalars(
                select(Request).where(
                    or_(Request.closed_at < cutoff, Request.closed_at.is_(None) & (Request.updated_at < cutoff))
                )
            )
        )
        for req in expired:
            if req.closed_at is None and s.scalar(select(Job.id).where(Job.request_id == req.id, Job.updated_at >= cutoff).limit(1)):
                continue  # open request with recent job activity: its baseline is still needed
            rows = list(s.scalars(select(Evidence).where(Evidence.request_id == req.id, Evidence.purged_at.is_(None))))
            drafts = list(s.scalars(select(ImageDraft).where(ImageDraft.request_id == req.id, ImageDraft.key.is_not(None))))
            atts = [a for a in req.attachments if a.get("key")]
            if not (rows or drafts or atts):
                continue
            out["requests"] += 1
            out["evidence_rows"] += len(rows)
            if dry_run:
                continue
            for ev in rows:
                for field in ("page_screenshot", "element_crop", "context_crop", "element_mask"):
                    out["files_deleted"] += _delete(getattr(ev, field))
                    setattr(ev, field, None)
                ev.purged_at = utcnow()
            for d in drafts:
                out["files_deleted"] += _delete(d.key)
                d.key = None
            kept = []
            for a in req.attachments:
                out["files_deleted"] += _delete(a.get("key"))
                kept.append({**a, "key": None, "purged_at": utcnow().isoformat()})
            req.attachments = kept
            req.status_reason = (req.status_reason + f" Evidence files deleted after {days} days (retention policy).").strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m feluda.retention")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    init_db()
    print(purge(args.days, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

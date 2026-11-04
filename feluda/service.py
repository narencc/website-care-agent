"""Application operations. Model output is only ever a proposal; these
functions validate permissions, state transitions, revisions and coverage
before writing records.
"""

from __future__ import annotations

import mimetypes
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import jobspec, netguard, profiles, states, storage, tasks, users
from .db import new_id, session, utcnow
from .records import ClosureRun, CoverageApproval, Evidence, FormOperation, ImageDraft, Job, JobEvent, Request, SiteLease, Submission, Task, Verdict
from .settings import app_settings

class ActionError(ValueError):
    """A user action is not allowed in the current state; message is user-facing."""


# --- Actors -------------------------------------------------------------------


def role_of(actor: str) -> str | None:
    return users.role_of(actor)


def require_role(actor: str, *roles: str) -> None:
    if role_of(actor) not in roles:
        raise ActionError(f"{actor or 'anonymous'} is not allowed to do this (needs {' or '.join(roles)}).")


def require_access(actor: str, req: Request | None) -> Request:
    """Per-customer separation: unknown and out-of-scope records look the same (not found)."""
    if req is None or not users.can_access(actor, req.account_id):
        raise ActionError("Not found.")
    return req


def load_request(s: Session, actor: str, request_id: str) -> Request:
    return require_access(actor, s.get(Request, request_id))


def load_job(s: Session, actor: str, job_id: str, *, lock: bool = False) -> Job:
    job = s.get(Job, job_id, with_for_update=lock) if job_id else None
    if job is None:
        raise ActionError("Not found.")
    require_access(actor, s.get(Request, job.request_id))
    return job


# --- Events, transitions, request status ----------------------------------------


def log_event(s: Session, job: Job, actor: str, action: str, **detail: Any) -> None:
    s.add(JobEvent(job_id=job.id, revision=job.revision, actor=actor, action=action, detail=detail))


def transition(s: Session, job: Job, new: str, actor: str, reason: str = "") -> None:
    states.check_transition(job.status, new)
    log_event(s, job, actor, "status", frm=job.status, to=new, reason=reason)
    job.status = new
    job.status_reason = reason


def refresh_request_status(s: Session, req: Request) -> None:
    if req.status in ("parsing", "plan_failed", "transcribing", "awaiting_transcript"):
        return
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
    closing = s.scalar(select(ClosureRun).where(ClosureRun.request_id == req.id, ClosureRun.outcome.in_(("pending", "running")))) is not None
    req.status = states.derive_request_status([j.status for j in jobs], parsing=False, closing=closing, closed=req.closed_at is not None)


def reopen_if_closed(s: Session, req: Request, actor: str, why: str) -> None:
    if req.closed_at is not None:
        req.closed_at = None
        req.done_message_valid = False
        req.status_reason = f"Reopened by {actor}: {why}. The drafted done message is no longer valid."


# --- Coverage -------------------------------------------------------------------


def coverage_map(s: Session, req: Request) -> dict[str, Any]:
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
    out = {}
    for r in req.requirements:
        mapped = sorted(j.id for j in jobs if r["id"] in (j.requirement_ids or []))
        disposition = r.get("disposition", "unmapped")
        if disposition == "jobs" and not mapped:
            disposition = "unmapped"
        out[r["id"]] = {"disposition": disposition, "jobs": mapped, "note": r.get("note", "")}
    return out


def unresolved_requirements(cov: dict[str, Any]) -> list[str]:
    return [rid for rid, c in cov.items() if c["disposition"] not in ("jobs", "excluded") or (c["disposition"] == "excluded" and not c["note"])]


def current_approval(s: Session, req: Request) -> CoverageApproval | None:
    approval = s.scalar(
        select(CoverageApproval).where(CoverageApproval.request_id == req.id).order_by(CoverageApproval.approved_at.desc()).limit(1)
    )
    if approval is None or approval.scope_revision != req.scope_revision:
        return None
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
    if approval.job_revisions != {j.id: j.revision for j in jobs}:
        return None
    if approval.coverage_map != coverage_map(s, req):
        return None
    return approval


# --- Leases (site edit freeze) -----------------------------------------------------


class LeaseLost(RuntimeError):
    pass


def active_lease(s: Session, host: str) -> SiteLease | None:
    lease = s.get(SiteLease, host)
    if lease and lease.holder and lease.expires_at and lease.expires_at > utcnow():
        return lease
    return None


def acquire_lease(s: Session, host: str, holder: str) -> int:
    lease = s.get(SiteLease, host, with_for_update=True)
    if lease is None:
        lease = SiteLease(site_host=host, token=0)
        s.add(lease)
        s.flush()
    if lease.holder and lease.expires_at and lease.expires_at > utcnow() and lease.holder != holder:
        raise ActionError(f"Site {host} is already frozen by closure run {lease.holder}.")
    lease.token += 1
    lease.holder = holder
    lease.expires_at = utcnow() + timedelta(seconds=app_settings().site_lease_ttl_s)
    return lease.token


def renew_lease(s: Session, host: str, token: int, *, allow_expired: bool = False) -> None:
    """Extend our lease. The fencing token proves nobody acquired it since; with
    ``allow_expired`` an expired but unclaimed lease (the run waited in the queue)
    may be resumed."""
    lease = s.get(SiteLease, host, with_for_update=True)
    if lease is None or lease.token != token or lease.holder is None:
        raise LeaseLost(f"site lease for {host} lost (token {token})")
    if not allow_expired and (not lease.expires_at or lease.expires_at <= utcnow()):
        raise LeaseLost(f"site lease for {host} expired (token {token})")
    lease.expires_at = utcnow() + timedelta(seconds=app_settings().site_lease_ttl_s)


def release_lease(s: Session, host: str, token: int) -> None:
    lease = s.get(SiteLease, host)
    if lease and lease.token == token:
        lease.holder = None
        lease.expires_at = None


def check_no_freeze(s: Session, host: str) -> None:
    lease = active_lease(s, host)
    if lease:
        raise ActionError(f"Site edit freeze in effect: final verification (run {lease.holder}) is in progress. Wait until it finishes.")


# --- Verdict currency -----------------------------------------------------------------


def current_verdict(s: Session, job: Job) -> Verdict | None:
    if not job.current_submission_id:
        return None
    return s.scalar(
        select(Verdict)
        .where(
            Verdict.job_id == job.id,
            Verdict.submission_id == job.current_submission_id,
            Verdict.job_revision == job.revision,
            Verdict.committed.is_(True),
            Verdict.invalidated.is_(False),
            Verdict.closure_run_id.is_(None),
        )
        .order_by(Verdict.created_at.desc())
        .limit(1)
    )


def invalidate_verdicts(s: Session, job: Job, why: str) -> None:
    for v in s.scalars(select(Verdict).where(Verdict.job_id == job.id, Verdict.invalidated.is_(False))):
        v.invalidated = True
        v.superseded_reason = why


# --- Intake ------------------------------------------------------------------------

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
AUDIO_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/ogg": "ogg", "audio/webm": "webm"}
AUDIO_MAX_BYTES = 25 * 1024 * 1024


def _media_type(filename: str, declared: str | None = None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    if declared in IMAGE_TYPES or declared in AUDIO_TYPES:
        return declared
    guessed = mimetypes.guess_type(filename)[0] or ""
    return {"audio/vnd.wave": "audio/wav", "audio/x-m4a": "audio/mp4"}.get(guessed, guessed)


def store_attachments(req_id: str, attachments: list, start: int = 1) -> list[dict[str, Any]]:
    """Validate and store screenshots and voice notes. Uploaded content is never executed."""
    cfg = app_settings()
    stored = []
    for i, item in enumerate(attachments or [], start=start):
        filename, data = item[0], item[1]
        media_type = _media_type(filename, item[2] if len(item) > 2 else None)
        if media_type in IMAGE_TYPES:
            limit, ext = cfg.attachment_max_bytes, IMAGE_TYPES[media_type]
            from . import imaging

            try:
                imaging.image_size(data)
            except Exception as e:
                raise ActionError(f"Attachment {filename!r} is not a readable image.") from e
        elif media_type in AUDIO_TYPES:
            limit, ext = AUDIO_MAX_BYTES, AUDIO_TYPES[media_type]
        else:
            raise ActionError(f"Attachment {filename!r}: only PNG/JPEG/WebP screenshots or WAV/MP3/M4A/OGG/WebM voice notes are accepted.")
        if len(data) > limit:
            raise ActionError(f"Attachment {filename!r} is larger than {limit // 1024 // 1024} MB.")
        key = storage.put(f"{req_id}/attachments/att_{i}.{ext}", data)
        entry = {"id": f"att_{i}", "filename": filename[:200], "media_type": media_type, "key": key, "sha256": storage.sha256(data)}
        if media_type in AUDIO_TYPES:
            entry["transcript_status"] = "pending"
        stored.append(entry)
    return stored


def create_request(
    *,
    site_url: str,
    raw_text: str,
    actor: str,
    customer: str = "",
    channel: str = "ticket",
    attachments: list | None = None,
    system_intake: bool = False,
) -> str:
    """Intake. ``system_intake`` is for authenticated integrations (email/ticket webhooks)."""
    if not system_intake:
        require_role(actor, "care_lead")
    url = netguard.normalize_site_url(site_url)
    try:
        netguard.check_url(url)
    except netguard.BlockedURL as e:
        raise ActionError(f"Website URL not allowed: {e}") from e
    if channel not in ("email", "ticket", "voice"):
        raise ActionError("Channel must be email, ticket or voice.")
    req_id = new_id("req")
    stored = store_attachments(req_id, attachments or [])
    audio = [a for a in stored if a["media_type"] in AUDIO_TYPES]
    if not raw_text.strip() and not audio:
        raise ActionError("Request text or a voice note is required.")
    with session() as s:
        acct = users.account_for_site(s, netguard.origin_of(url))
        if not system_intake and not users.can_access(actor, acct.id):
            raise ActionError(f"{actor} has no access to customer account {acct.name}.")
        profile = profiles.profile_for_url(url)
        req = Request(
            id=req_id,
            account_id=acct.id,
            site_url=url,
            site_host=netguard.origin_of(url),
            customer=customer.strip()[:255],
            channel="voice" if audio and not raw_text.strip() else channel,
            raw_text=raw_text.strip(),
            attachments=stored,
            site_profile_id=profile["id"] if profile else None,
            status="transcribing" if audio else "parsing",
        )
        s.add(req)
        s.flush()
        if audio:
            tasks.enqueue(s, "transcribe", f"transcribe:{req_id}", {"request_id": req_id}, max_executions=2)
        else:
            tasks.enqueue(s, "plan_request", f"plan:{req_id}:{req.scope_revision}", {"request_id": req_id, "scope_revision": req.scope_revision})
    return req_id


def submit_transcript(request_id: str, actor: str, attachment_id: str, text: str) -> None:
    """A Care lead types the transcript when Muse Voice Transcribe is unavailable."""
    require_role(actor, "care_lead")
    if not text.strip():
        raise ActionError("Paste the transcript text.")
    with session() as s:
        req = load_request(s, actor, request_id)
        if req.status not in ("awaiting_transcript", "transcribing"):
            raise ActionError("Planning has already started from the transcript; add corrections with Re-plan instead.")
        atts = [dict(a) for a in req.attachments]
        att = next((a for a in atts if a["id"] == attachment_id and a["media_type"] in AUDIO_TYPES), None)
        if att is None:
            raise ActionError("Unknown voice note.")
        att.update(transcript=text.strip(), transcript_status="manual", transcribed_by=actor)
        req.attachments = atts
        start_planning_if_transcribed(s, req)


def start_planning_if_transcribed(s: Session, req: Request) -> bool:
    audio = [a for a in req.attachments if a["media_type"] in AUDIO_TYPES]
    if any(a.get("transcript_status") not in ("done", "manual") for a in audio):
        return False
    notes = []
    for a in audio:
        source = "Muse Voice Transcribe" if a["transcript_status"] == "done" else "typed by " + a.get("transcribed_by", "Care lead")
        if a["transcript"] not in req.raw_text:
            notes.append(f"[Voice note {a['filename']} transcript ({source})]\n{a['transcript']}")
    if notes:
        req.raw_text = (req.raw_text + "\n\n" + "\n\n".join(notes)).strip()
    req.status, req.status_reason = "parsing", ""
    tasks.enqueue(s, "plan_request", f"plan:{req.id}:{req.scope_revision}", {"request_id": req.id, "scope_revision": req.scope_revision})
    return True


def replan(request_id: str, actor: str, clarification: str = "") -> None:
    """Re-run the planner (optionally with a Care lead clarification) before any builder work."""
    require_role(actor, "care_lead")
    with session() as s:
        req = load_request(s, actor, request_id)
        jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
        if any(s.scalar(select(Submission.id).where(Submission.job_id == j.id).limit(1)) for j in jobs) or any(j.baseline_locked for j in jobs):
            raise ActionError("Builder work has started on this request; edit jobs individually instead of re-planning.")
        if req.closed_at:
            raise ActionError("Request is closed.")
        for j in jobs:
            for model in (JobEvent, Evidence, Verdict, FormOperation):
                for row in s.scalars(select(model).where(model.job_id == j.id)):
                    s.delete(row)
            s.delete(j)
        if clarification.strip():
            req.raw_text += f"\n\n[Clarification from Care lead {actor} at {utcnow():%Y-%m-%d %H:%M} UTC]\n{clarification.strip()}"
        req.scope_revision += 1
        req.status = "parsing"
        req.status_reason = ""
        req.planner_notes = ""
        req.requirements = []
        s.flush()
        tasks.enqueue(s, "plan_request", f"plan:{req.id}:{req.scope_revision}", {"request_id": req.id, "scope_revision": req.scope_revision})


# --- Review and corrections -----------------------------------------------------------


def set_requirement(request_id: str, requirement_id: str, actor: str, *, disposition: str, note: str = "") -> None:
    require_role(actor, "care_lead")
    if disposition not in ("jobs", "clarification", "excluded"):
        raise ActionError("Disposition must be jobs, clarification or excluded.")
    if disposition == "excluded" and not note.strip():
        raise ActionError("An exclusion needs a reason.")
    with session() as s:
        req = load_request(s, actor, request_id)
        reqs = [dict(r) for r in req.requirements]
        match = next((r for r in reqs if r["id"] == requirement_id), None)
        if match is None:
            raise ActionError("Unknown requirement.")
        match.update(disposition=disposition, note=note.strip(), reviewed_by=actor, reviewed_at=utcnow().isoformat())
        req.requirements = reqs
        req.scope_revision += 1
        reopen_if_closed(s, req, actor, f"requirement {requirement_id} changed")
        refresh_request_status(s, req)


EDITABLE_TARGET_FIELDS = ("page_urls", "target_description", "region", "element_role", "current_value", "desired_value", "current_href", "desired_href", "viewports", "desired_asset_key", "menu_action", "expected_menu_order")


def edit_job(job_id: str, actor: str, changes: dict[str, Any], reason: str) -> int:
    """Correct scope/targets/checks/recipe. Bumps the revision and invalidates prior verdicts."""
    require_role(actor, "care_lead", "builder")
    if not reason.strip():
        raise ActionError("Say why you are changing the job.")
    with session() as s:
        job = load_job(s, actor, job_id)
        req = s.get(Request, job.request_id)
        if job.status in ("queued", "verifying"):
            raise ActionError("Wait for the running verification to finish before editing this job.")
        target = {k: v for k, v in job.targets[0].items()}
        before = {k: target.get(k) for k in EDITABLE_TARGET_FIELDS}
        for k in EDITABLE_TARGET_FIELDS:
            if k in changes and changes[k] is not None:
                target[k] = changes[k]
        if target.get("region") not in ("banner", "navigation", "main", "contentinfo", "complementary", "page"):
            raise ActionError("Region must be banner, navigation, main, contentinfo, complementary or page.")
        if target.get("element_role") not in ("link", "button", "text", "heading", "image", "form"):
            raise ActionError("Element role must be link, button, text, heading, image or form.")
        for url in target["page_urls"]:
            if not netguard.same_origin(url, req.site_url):
                raise ActionError(f"Page {url} is outside the site origin.")
        recipe_id = changes.get("recipe_id", (job.recipe_snapshot or {}).get("recipe_id"))
        require_absent = bool(changes.get("require_old_value_absent", target.get("require_old_value_absent", True)))
        derived = jobspec.derive_job(
            job_type=changes.get("type") or job.type,
            target=target,
            recipe_id=recipe_id,
            profile=profiles.get_profile(req.site_profile_id),
            require_old_value_absent=require_absent,
            page_outlines=(req.crawl_manifest or {}).get("outlines"),
        )
        if job.baseline_locked:
            # Keep the original baseline's structural identity; never silently replace it.
            derived["targets"][0]["structural_anchor"] = job.targets[0].get("structural_anchor")
        job.type = changes.get("type") or job.type
        job.title = changes.get("title") or job.title
        if "exclusions" in changes:
            job.exclusions = [e for e in changes["exclusions"] if e.strip()]
        if "depends_on" in changes:
            job.depends_on = list(changes["depends_on"])
        job.targets = derived["targets"]
        job.acceptance_checks = derived["acceptance_checks"]
        job.recipe_snapshot = derived["recipe_snapshot"]
        job.builder_steps = derived["builder_steps"]
        job.recipe_values = derived["recipe_values"]
        job.blockers = derived["blockers"]
        job.revision += 1
        job.current_submission_id = None
        invalidate_verdicts(s, job, f"job revised to r{job.revision}")
        log_event(s, job, actor, "edit", reason=reason, before=before, after={k: target.get(k) for k in EDITABLE_TARGET_FIELDS}, recipe_id=recipe_id)
        if job.status != "needs_review":
            transition(s, job, "needs_review", actor, f"Revised to r{job.revision}: {reason}")
        reopen_if_closed(s, req, actor, f"job {job.id} revised")
        refresh_request_status(s, req)
        return job.revision


def approve_coverage(request_id: str, actor: str) -> str:
    require_role(actor, "care_lead")
    with session() as s:
        req = load_request(s, actor, request_id)
        if req.status in ("parsing", "plan_failed", "transcribing", "awaiting_transcript"):
            raise ActionError("The request has no plan to approve yet.")
        cov = coverage_map(s, req)
        unresolved = unresolved_requirements(cov)
        if unresolved:
            raise ActionError(
                "Every requirement must map to a job or a reviewed exclusion before approval. Unresolved: " + ", ".join(unresolved)
            )
        jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
        approval = CoverageApproval(
            id=new_id("appr"),
            request_id=req.id,
            scope_revision=req.scope_revision,
            job_revisions={j.id: j.revision for j in jobs},
            coverage_map=cov,
            approved_by=actor,
        )
        s.add(approval)
        for j in jobs:
            if j.status == "draft":
                transition(s, j, "needs_review", actor, "coverage approved")
            if j.status != "needs_review":
                continue
            if j.blockers:
                continue  # stays in review until a correction clears its blockers
            if j.baseline_locked and j.baseline_revision is not None:
                transition(s, j, "ready", actor, "approved; original baseline kept")
            else:
                task = tasks.enqueue(s, "capture_baseline", f"baseline:{j.id}:{j.revision}", {"job_id": j.id, "revision": j.revision})
                if task.status in ("succeeded", "dead"):
                    # Same revision was captured under an approval that has since changed
                    # (or the job went back to review): capture again rather than stall.
                    tasks.requeue(s, task, extra_executions=app_settings().task_max_executions)
        refresh_request_status(s, req)
        return approval.id


def recapture_baseline(job_id: str, actor: str) -> None:
    require_role(actor, "care_lead")
    with session() as s:
        job = load_job(s, actor, job_id)
        req = s.get(Request, job.request_id)
        if job.baseline_locked:
            raise ActionError("The builder has started; the original baseline is kept.")
        if current_approval(s, req) is None:
            raise ActionError("Approve the coverage map first.")
        if job.status == "needs_human":
            transition(s, job, "needs_review", actor, "baseline recapture requested")
        task = tasks.enqueue(s, "capture_baseline", f"baseline:{job.id}:{job.revision}", {"job_id": job.id, "revision": job.revision})
        if task.status in ("dead", "succeeded"):
            tasks.requeue(s, task, extra_executions=app_settings().task_max_executions)


# --- Builder actions -----------------------------------------------------------------


def _deps_passed(s: Session, job: Job) -> list[str]:
    pending = []
    for dep_id in job.depends_on or []:
        dep = s.get(Job, dep_id)
        if dep is None or dep.status != "passed":
            pending.append(dep_id)
    return pending


def start_job(job_id: str, actor: str) -> None:
    require_role(actor, "builder")
    with session() as s:
        job = load_job(s, actor, job_id, lock=True)
        req = s.get(Request, job.request_id)
        check_no_freeze(s, req.site_host)
        if job.status != "ready":
            raise ActionError(f"Job is {job.status}; only ready jobs can be started.")
        if current_approval(s, req) is None:
            raise ActionError("The coverage approval is no longer current; the Care lead must re-approve.")
        pending = _deps_passed(s, job)
        if pending:
            raise ActionError("Waiting on prerequisite jobs: " + ", ".join(pending))
        region = job.targets[0]["region"]
        if job.scope == "shared_component":
            others = s.scalars(
                select(Job).join(Request, Request.id == Job.request_id).where(
                    Request.site_host == req.site_host, Job.id != job.id, Job.status.in_(tuple(states.ACTIVE_EDIT_STATES)), Job.scope == "shared_component"
                )
            )
            for other in others:
                if other.targets and other.targets[0]["region"] == region:
                    raise ActionError(f"Job {other.id} is editing the same shared {region} component; finish it first.")
            # Passing jobs on the same shared component must be re-verified after this edit.
            for other in s.scalars(
                select(Job)
                .join(Request, Request.id == Job.request_id)
                # Closed requests keep their evidence and verification interval; they are history.
                .where(Request.site_host == req.site_host, Request.closed_at.is_(None), Job.id != job.id, Job.status == "passed")
            ):
                if other.targets and other.targets[0]["region"] == region:
                    invalidate_verdicts(s, other, f"shared {region} component is being edited by job {job.id}")
                    transition(s, other, "with_builder", "system", f"Re-verify: the shared {region} component is being edited by job {job.id}.")
                    refresh_request_status(s, s.get(Request, other.request_id))
        job.baseline_locked = True
        job.assigned_to = actor
        transition(s, job, "with_builder", actor, "started")
        refresh_request_status(s, req)


def mark_done(job_id: str, actor: str, client_token: str) -> tuple[str, bool]:
    """Builder asserts the site is ready. Returns (submission_id, created).

    Duplicate clicks and HTTP retries (same client token, or a submission
    already queued/verifying for this revision) return the existing submission.
    """
    require_role(actor, "builder")
    client_token = (client_token or "").strip()[:80] or new_id("tok")
    with session() as s:
        job = load_job(s, actor, job_id, lock=True)  # serialize concurrent clicks
        req = s.get(Request, job.request_id)
        dup = s.scalar(select(Submission).where(Submission.job_id == job.id, Submission.client_token == client_token))
        if dup:
            return dup.id, False
        if job.status in ("queued", "verifying") and job.current_submission_id:
            sub = s.get(Submission, job.current_submission_id)
            if sub and sub.job_revision == job.revision:
                return sub.id, False
        check_no_freeze(s, req.site_host)
        if job.status not in ("with_builder", "failed"):
            raise ActionError(f"Job is {job.status}; Mark done is only available while the job is with the builder.")
        if job.status == "failed":
            transition(s, job, "with_builder", actor, "builder corrected the site")
        sub = Submission(
            id=new_id("sub"), job_id=job.id, job_revision=job.revision, attempt_id=new_id("att"), client_token=client_token, submitted_by=actor
        )
        s.add(sub)
        s.flush()
        job.current_submission_id = sub.id
        transition(s, job, "queued", actor, f"Mark done (submission {sub.id})")
        tasks.enqueue(s, "verify_submission", f"verify:{sub.id}", {"submission_id": sub.id}, attempt_id=sub.attempt_id)
        refresh_request_status(s, req)
        return sub.id, True


def retry_verification(job_id: str, actor: str) -> None:
    """Infrastructure retry: resume the same attempt through a new task execution."""
    require_role(actor, "builder", "care_lead")
    with session() as s:
        job = load_job(s, actor, job_id)
        if job.status != "verification_error":
            raise ActionError("Only a verification error can be retried.")
        check_no_freeze(s, s.get(Request, job.request_id).site_host)
        task = s.scalar(select(Task).where(Task.idempotency_key == f"verify:{job.current_submission_id}"))
        if task is None:
            raise ActionError("No verification task found for the current submission.")
        tasks.requeue(s, task, extra_executions=app_settings().task_max_executions)
        transition(s, job, "queued", actor, "retry after verification error")
        refresh_request_status(s, s.get(Request, job.request_id))


def resolve_needs_human(job_id: str, actor: str, action: str, note: str) -> None:
    """A human resolves review. There is deliberately no action that marks a job passed."""
    require_role(actor, "care_lead")
    if not note.strip():
        raise ActionError("Add a note explaining the decision.")
    with session() as s:
        job = load_job(s, actor, job_id)
        if job.status != "needs_human":
            raise ActionError("Job is not waiting for human review.")
        target = {"return_to_builder": "with_builder", "mark_failed": "failed", "send_to_review": "needs_review"}.get(action)
        if target is None:
            raise ActionError("Unknown action.")
        if target in ("with_builder", "failed") and not job.baseline_locked:
            raise ActionError("Builder work has not started; send the job back to review instead.")
        transition(s, job, target, actor, note)
        log_event(s, job, actor, "human_review", decision=action, note=note)
        refresh_request_status(s, s.get(Request, job.request_id))


# --- Closure ----------------------------------------------------------------------------


def closure_preconditions(s: Session, req: Request) -> list[str]:
    problems = []
    approval = current_approval(s, req)
    if approval is None:
        problems.append("Requirement coverage approval is missing or out of date.")
    cov = coverage_map(s, req)
    for rid in unresolved_requirements(cov):
        problems.append(f"Requirement {rid} is unresolved.")
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
    if not jobs:
        problems.append("Request has no jobs.")
    for j in jobs:
        if j.status != "passed":
            problems.append(f"Job {j.id} is {j.status}, not passed.")
            continue
        v = current_verdict(s, j)
        if v is None or v.result != "pass":
            problems.append(f"Job {j.id} has no current passing verdict for revision {j.revision}.")
        if _deps_passed(s, j):
            problems.append(f"Job {j.id} has unfinished prerequisites.")
    return problems


def start_closure(request_id: str, actor: str, freeze_acknowledged: bool) -> str:
    require_role(actor, "care_lead")
    if not freeze_acknowledged:
        raise ActionError("Confirm that every participating builder has finished editing and acknowledged the edit freeze.")
    with session() as s:
        req = load_request(s, actor, request_id)
        if req.closed_at:
            raise ActionError("Request is already closed.")
        problems = closure_preconditions(s, req)
        if problems:
            raise ActionError("Cannot start the final consistency gate: " + " ".join(problems))
        run_id = new_id("close")
        token = acquire_lease(s, req.site_host, run_id)
        jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
        cfg = app_settings()
        run = ClosureRun(
            id=run_id,
            request_id=req.id,
            scope_revision=req.scope_revision,
            approval_id=current_approval(s, req).id,
            job_revisions={j.id: j.revision for j in jobs},
            submission_ids={j.id: j.current_submission_id for j in jobs},
            lease_token=token,
            started_by=actor,
            freeze_acknowledged=True,
            settings={
                "closure_window_s": cfg.closure_window_s,
                "fingerprint_max_age_s": cfg.fingerprint_max_age_s,
                "site_lease_ttl_s": cfg.site_lease_ttl_s,
                "normalization": "capture.REGIONS_JS v1 (whitespace-collapsed innerText per landmark)",
                "dynamic_masks": (profiles.get_profile(req.site_profile_id) or {}).get("dynamic_masks", []),
            },
        )
        s.add(run)
        s.flush()
        tasks.enqueue(s, "closure_run", f"closure:{run_id}", {"closure_run_id": run_id}, max_executions=1)
        refresh_request_status(s, req)
        return run_id


# --- Approved replacement images -------------------------------------------------------------


def request_image_draft(job_id: str, actor: str, prompt: str) -> str:
    """Ask Muse Image 1.0 for a replacement-image draft (customer asked for new visuals)."""
    require_role(actor, "care_lead")
    if len(prompt.strip()) < 10:
        raise ActionError("Describe the image you want in a sentence or two.")
    with session() as s:
        job = load_job(s, actor, job_id)
        if job.type not in jobspec.IMAGE_TYPES:
            raise ActionError("Image drafts are only for image or logo jobs.")
        draft = ImageDraft(id=new_id("draft"), request_id=job.request_id, job_id=job.id, prompt=prompt.strip()[:2000], created_by=actor)
        s.add(draft)
        s.flush()
        tasks.enqueue(s, "image_draft", f"image_draft:{draft.id}", {"draft_id": draft.id}, max_executions=2)
        return draft.id


def _set_job_asset(job_id: str, actor: str, key: str, why: str) -> int:
    with session() as s:
        job = load_job(s, actor, job_id)
        target = dict(job.targets[0])
    return edit_job(job_id, actor, {"desired_asset_key": key, "desired_value": target["desired_value"]}, why)


def approve_image_draft(draft_id: str, actor: str) -> int:
    """A Care lead approves a draft as the job's target image (new job revision)."""
    require_role(actor, "care_lead")
    with session() as s:
        d = s.get(ImageDraft, draft_id)
        if d is None or d.status != "draft":
            raise ActionError("Only a finished draft can be approved.")
        load_job(s, actor, d.job_id)
        job_id, key = d.job_id, d.key
    # Revise the job first: a draft is only marked approved once the job really uses it.
    revision = _set_job_asset(job_id, actor, key, f"Approved Muse Image draft {draft_id} as the replacement image")
    with session() as s:
        for other in s.scalars(select(ImageDraft).where(ImageDraft.job_id == job_id, ImageDraft.status == "approved")):
            other.status = "draft"
        s.get(ImageDraft, draft_id).status = "approved"
    return revision


def reject_image_draft(draft_id: str, actor: str) -> None:
    require_role(actor, "care_lead")
    with session() as s:
        d = s.get(ImageDraft, draft_id)
        if d is None:
            raise ActionError("Not found.")
        load_job(s, actor, d.job_id)
        d.status = "rejected"


def upload_job_asset(job_id: str, actor: str, filename: str, data: bytes) -> int:
    """Attach the customer's approved replacement image to an image/logo job."""
    require_role(actor, "care_lead")
    with session() as s:
        job = load_job(s, actor, job_id)
        if job.type not in jobspec.IMAGE_TYPES:
            raise ActionError("Only image or logo jobs take an approved image.")
        req = s.get(Request, job.request_id)
        start = len(req.attachments) + 1
    if _media_type(filename) not in IMAGE_TYPES:
        raise ActionError("Upload a PNG, JPEG or WebP image.")
    stored = store_attachments(job.request_id, [(filename, data)], start=start)
    with session() as s:
        req = s.get(Request, job.request_id)
        req.attachments = list(req.attachments) + stored
    return _set_job_asset(job_id, actor, stored[0]["key"], f"Uploaded approved image {filename}")

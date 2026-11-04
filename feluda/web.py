"""FastAPI app: login, Care lead queue, builder job card, evidence report,
metrics, and the authenticated email/ticket intake webhooks.

    uvicorn feluda.web:app --port 8000
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request as HttpRequest, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import intake, metrics, redact, service, storage, users
from .db import init_db, session, utcnow
from .records import ClosureRun, Evidence, FormOperation, ImageDraft, Job, JobEvent, ModelCall, Request, Submission, Task, Verdict
from .settings import app_settings  # noqa: F401

log = logging.getLogger("feluda.web")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals["signed"] = storage.signed_url

STATUS_TONE = {
    "passed": "good", "done": "good", "closed": "good", "ready_to_close": "good", "pass": "good",
    "failed": "bad", "fail": "bad", "plan_failed": "bad", "blocked": "bad",
    "needs_human": "warn", "verification_error": "warn", "error": "warn", "needs_review": "warn",
    "queued": "busy", "verifying": "busy", "parsing": "busy", "closing": "busy", "running": "busy", "pending": "busy",
    "transcribing": "busy", "awaiting_transcript": "warn", "received": "good", "not_received": "bad", "uncertain": "warn",
    "submitted": "busy", "submitting": "busy", "approved": "good", "draft": "neutral", "rejected": "bad",
}
TEMPLATES.env.globals["tone"] = lambda status: STATUS_TONE.get(status, "neutral")
BUSY = {"parsing", "queued", "verifying", "closing", "transcribing"}
PUBLIC_PREFIXES = ("/login", "/intake/", "/favicon.ico")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redact.install_log_redaction()
    init_db()
    worker = None
    if app_settings().inline_worker:
        from .tasks import Worker

        worker = Worker()
        worker.start()
    yield
    if worker:
        worker.stop()
        worker.join(timeout=10)


app = FastAPI(title="Feluda Care Agent", lifespan=lifespan)


@app.middleware("http")
async def same_origin_posts(request: HttpRequest, call_next):
    """CSRF guard: browsers send Origin (or Referer) on cross-site POSTs; reject foreign ones."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlsplit(source).netloc != request.headers.get("host", ""):
            return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def require_login(request: HttpRequest, call_next):
    if request.url.path.startswith(PUBLIC_PREFIXES) or actor_of(request):
        return await call_next(request)
    if request.method == "GET":
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
    return PlainTextResponse("login required", status_code=401)


def actor_of(request: HttpRequest) -> str:
    """The logged-in user from the signed session cookie; anonymous is never a Care lead."""
    return users.read_session(request.cookies.get("feluda_session")) or ""


def visible(actor: str, req: Request | None) -> Request:
    """Per-customer separation for pages: out-of-scope records are simply not found."""
    if req is None or not users.can_access(actor, req.account_id):
        raise HTTPException(404)
    return req


def safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) else "/"


def render(request: HttpRequest, name: str, **ctx: Any) -> HTMLResponse:
    actor = actor_of(request)
    return TEMPLATES.TemplateResponse(
        request,
        name,
        {"actor": actor, "role": service.role_of(actor), "msg": request.query_params.get("msg"), "err": request.query_params.get("err"), **ctx},
    )


def back(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg[:400])}"
    elif err:
        url += f"{sep}err={quote(err[:600])}"
    return RedirectResponse(url, status_code=303)


def act(url: str, fn, *args, ok: str = "Done.", **kwargs) -> RedirectResponse:
    try:
        fn(*args, **kwargs)
    except service.ActionError as e:
        return back(url, err=str(e))
    except Exception as e:  # state-machine and other validation errors are user-facing too
        log.exception("action failed")
        return back(url, err=f"{type(e).__name__}: {e}")
    return back(url, msg=ok)


# --- Login -------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_page(request: HttpRequest, next: str = "/"):
    return render(request, "login.html", next=safe_next(next))


@app.post("/login")
def login(username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    user = users.authenticate(username, password)
    if not user:
        return back(f"/login?next={quote(safe_next(next))}", err="Wrong username or password.")
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie("feluda_session", users.make_session(user), httponly=True, samesite="lax", max_age=users.SESSION_TTL_S)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("feluda_session")
    return resp


# --- Request queue ------------------------------------------------------------------


def _age(dt) -> str:
    secs = int((utcnow() - dt).total_seconds())
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


@app.get("/", response_class=HTMLResponse)
def queue(request: HttpRequest):
    actor = actor_of(request)
    with session() as s:
        rows = []
        for req in s.scalars(select(Request).order_by(Request.received_at.desc())):
            if not users.can_access(actor, req.account_id):
                continue
            jobs = list(s.scalars(select(Job).where(Job.request_id == req.id)))
            rows.append(
                {
                    "req": req,
                    "age": _age(req.received_at),
                    "jobs": len(jobs),
                    "passed": sum(j.status == "passed" for j in jobs),
                    "stuck": [j for j in jobs if j.status in ("needs_human", "verification_error", "failed")],
                    "cost": s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(ModelCall.request_id == req.id)) or 0.0,
                }
            )
        busy = any(r["req"].status in BUSY for r in rows)
        return render(request, "queue.html", rows=rows, busy=busy)


@app.post("/requests")
async def create_request(
    request: HttpRequest,
    site_url: str = Form(""),
    customer: str = Form(""),
    channel: str = Form("ticket"),
    raw_text: str = Form(""),
    files: list[UploadFile] | None = None,
):
    attachments = []
    for f in files or []:
        if f.filename:
            attachments.append((f.filename, await f.read()))
    try:
        rid = service.create_request(
            site_url=site_url, raw_text=raw_text, actor=actor_of(request), customer=customer, channel=channel, attachments=attachments
        )
    except service.ActionError as e:
        return back("/", err=str(e))
    return back(f"/requests/{rid}", msg="Request received.")


# --- Intake webhooks (email / ticket systems) ----------------------------------------------


def _intake_authorised(request: HttpRequest) -> None:
    expected = os.environ.get("FELUDA_INTAKE_TOKEN")
    if not expected:
        raise HTTPException(503, "intake webhooks are disabled (set FELUDA_INTAKE_TOKEN)")
    if not hmac.compare_digest(request.headers.get("x-feluda-intake-token", ""), expected):
        raise HTTPException(401, "bad intake token")


@app.post("/intake/email")
async def intake_email(request: HttpRequest):
    """JSON {from, subject, text, site_url?, attachments:[{filename, content_type, data_b64}]} or raw RFC 822 (message/rfc822)."""
    _intake_authorised(request)
    try:
        if request.headers.get("content-type", "").startswith("message/rfc822"):
            payload = intake.from_mime(await request.body())
        else:
            payload = intake.from_email_payload(await request.json())
        rid = intake.intake(payload)
    except (service.ActionError, ValueError, KeyError) as e:
        raise HTTPException(422, str(e))
    return JSONResponse({"request_id": rid}, status_code=201)


@app.post("/intake/ticket")
async def intake_ticket(request: HttpRequest):
    """JSON {site_url, text, customer?, channel?, attachments:[{filename, content_type, data_b64}]}."""
    _intake_authorised(request)
    try:
        body = await request.json()
        payload = intake.from_email_payload({"from": body.get("customer", ""), "subject": body.get("subject", ""), "text": body.get("text", ""), "site_url": body.get("site_url"), "attachments": body.get("attachments")})
        payload["channel"] = body.get("channel") if body.get("channel") in ("ticket", "voice", "email") else "ticket"
        payload["raw_text"] = body.get("text", "") if not body.get("subject") else payload["raw_text"]
        rid = intake.intake(payload)
    except (service.ActionError, ValueError, KeyError) as e:
        raise HTTPException(422, str(e))
    return JSONResponse({"request_id": rid}, status_code=201)


# --- Request detail -----------------------------------------------------------------


def request_context(s, req: Request) -> dict[str, Any]:
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id).order_by(Job.created_at)))
    cov = service.coverage_map(s, req) if req.requirements else {}
    approval = service.current_approval(s, req)
    last_approval = s.scalar(select(service.CoverageApproval).where(service.CoverageApproval.request_id == req.id).order_by(service.CoverageApproval.approved_at.desc()).limit(1))
    runs = list(s.scalars(select(ClosureRun).where(ClosureRun.request_id == req.id).order_by(ClosureRun.created_at.desc())))
    calls = list(s.scalars(select(ModelCall).where(ModelCall.request_id == req.id).order_by(ModelCall.at)))
    tasks = list(s.scalars(select(Task).order_by(Task.created_at.desc()).limit(500)))
    job_ids = {j.id for j in jobs}
    sub_ids = set(s.scalars(select(Submission.id).where(Submission.job_id.in_(job_ids)))) if job_ids else set()
    run_ids = {r.id for r in s.scalars(select(ClosureRun).where(ClosureRun.request_id == req.id))}
    req_tasks = [
        t for t in tasks
        if (t.payload or {}).get("request_id") == req.id
        or (t.payload or {}).get("job_id") in job_ids
        or (t.payload or {}).get("submission_id") in sub_ids
        or (t.payload or {}).get("closure_run_id") in run_ids
    ]
    freeze = service.active_lease(s, req.site_host)
    return {
        "req": req,
        "jobs": jobs,
        "jobs_by_id": {j.id: j for j in jobs},
        "coverage": cov,
        "unresolved": service.unresolved_requirements(cov) if cov else [],
        "approval": approval,
        "last_approval": last_approval,
        "closure_problems": [] if req.closed_at else service.closure_preconditions(s, req),
        "runs": runs,
        "calls": calls,
        "cost": sum(c.cost_usd for c in calls),
        "tasks": req_tasks,
        "freeze": freeze,
        "busy": req.status in BUSY
        or any(j.status in BUSY for j in jobs)
        or any(r.outcome in ("pending", "running") for r in runs)
        or any(t.status in ("queued", "running") for t in req_tasks),
    }


@app.get("/requests/{rid}", response_class=HTMLResponse)
def request_detail(request: HttpRequest, rid: str):
    with session() as s:
        req = visible(actor_of(request), s.get(Request, rid))
        return render(request, "request.html", **request_context(s, req))


@app.post("/requests/{rid}/requirements/{req_key}")
def set_requirement(request: HttpRequest, rid: str, req_key: str, disposition: str = Form(...), note: str = Form("")):
    return act(f"/requests/{rid}", service.set_requirement, rid, req_key, actor_of(request), disposition=disposition, note=note, ok="Requirement updated; re-approve the coverage map.")


@app.post("/requests/{rid}/approve")
def approve(request: HttpRequest, rid: str):
    return act(f"/requests/{rid}", service.approve_coverage, rid, actor_of(request), ok="Coverage approved. Capturing before-evidence.")


@app.post("/requests/{rid}/replan")
def replan(request: HttpRequest, rid: str, clarification: str = Form("")):
    return act(f"/requests/{rid}", service.replan, rid, actor_of(request), clarification, ok="Re-planning with your clarification.")


@app.post("/requests/{rid}/transcript")
def transcript(request: HttpRequest, rid: str, attachment_id: str = Form(...), text: str = Form("")):
    return act(f"/requests/{rid}", service.submit_transcript, rid, actor_of(request), attachment_id, text, ok="Transcript saved; planning starts now.")


@app.post("/requests/{rid}/close")
def close(request: HttpRequest, rid: str, freeze_ack: str = Form("")):
    return act(f"/requests/{rid}", service.start_closure, rid, actor_of(request), freeze_ack == "yes", ok="Final consistency gate started; the site is frozen until it finishes.")


# --- Builder job card ------------------------------------------------------------------


def job_context(s, job: Job) -> dict[str, Any]:
    req = s.get(Request, job.request_id)
    baseline = list(
        s.scalars(select(Evidence).where(Evidence.job_id == job.id, Evidence.phase == "baseline").order_by(Evidence.captured_at.desc()))
    )
    seen, latest_baseline = set(), []
    for e in baseline:
        if (e.requested_url, e.viewport) not in seen and e.job_revision == job.baseline_revision:
            seen.add((e.requested_url, e.viewport))
            latest_baseline.append(e)
    latest_baseline.sort(key=lambda e: (e.requested_url, e.viewport))
    submissions = list(s.scalars(select(Submission).where(Submission.job_id == job.id).order_by(Submission.submitted_at.desc())))
    verdicts = list(s.scalars(select(Verdict).where(Verdict.job_id == job.id).order_by(Verdict.created_at.desc())))
    current = service.current_verdict(s, job)
    shown = current or next((v for v in verdicts if v.committed and v.closure_run_id is None), None)
    after = []
    if shown:
        evs = [s.get(Evidence, e) for e in shown.evidence_ids]
        after = sorted([e for e in evs if e is not None and e.phase == "after"], key=lambda e: (e.requested_url, e.viewport))
    pairs = []
    for a in after:
        b = next((e for e in latest_baseline if e.requested_url == a.requested_url and e.viewport == a.viewport), None)
        pairs.append((b, a))
    events = list(s.scalars(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.at.desc()).limit(40)))
    deps = [s.get(Job, d) for d in job.depends_on or []]
    profile = service.profiles.get_profile(req.site_profile_id)
    drafts = list(s.scalars(select(ImageDraft).where(ImageDraft.job_id == job.id).order_by(ImageDraft.created_at.desc())))
    form_ops = list(s.scalars(select(FormOperation).where(FormOperation.job_id == job.id).order_by(FormOperation.created_at.desc())))
    return {
        "drafts": drafts,
        "form_ops": form_ops,
        "job": job,
        "req": req,
        "target": job.targets[0] if job.targets else {},
        "baseline": latest_baseline,
        "submissions": submissions,
        "verdicts": verdicts,
        "current": current,
        "shown": shown,
        "pairs": pairs,
        "events": events,
        "deps": deps,
        "freeze": service.active_lease(s, req.site_host),
        "approval": service.current_approval(s, req),
        "profile": profile,
        "client_token": uuid.uuid4().hex,
        "busy": job.status in BUSY or any(d.status == "pending" for d in drafts),
    }


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_card(request: HttpRequest, jid: str):
    with session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        visible(actor_of(request), s.get(Request, job.request_id))
        return render(request, "job.html", **job_context(s, job))


@app.post("/jobs/{jid}/start")
def start(request: HttpRequest, jid: str):
    return act(f"/jobs/{jid}", service.start_job, jid, actor_of(request), ok="Job started. Follow the steps, then select Mark done.")


@app.post("/jobs/{jid}/done")
def done(request: HttpRequest, jid: str, client_token: str = Form("")):
    try:
        sub_id, created = service.mark_done(jid, actor_of(request), client_token)
    except service.ActionError as e:
        return back(f"/jobs/{jid}", err=str(e))
    return back(f"/jobs/{jid}", msg=("Verification queued." if created else "Already submitted; showing the same verification.") + f" (submission {sub_id})")


@app.post("/jobs/{jid}/retry")
def retry(request: HttpRequest, jid: str):
    return act(f"/jobs/{jid}", service.retry_verification, jid, actor_of(request), ok="Verification retried.")


@app.post("/jobs/{jid}/recapture")
def recapture(request: HttpRequest, jid: str):
    return act(f"/jobs/{jid}", service.recapture_baseline, jid, actor_of(request), ok="Baseline capture queued.")


@app.post("/jobs/{jid}/resolve")
def resolve(request: HttpRequest, jid: str, action: str = Form(...), note: str = Form("")):
    return act(f"/jobs/{jid}", service.resolve_needs_human, jid, actor_of(request), action, note, ok="Review recorded.")


@app.post("/jobs/{jid}/asset")
async def job_asset(request: HttpRequest, jid: str, file: UploadFile):
    data = await file.read()
    return act(f"/jobs/{jid}", service.upload_job_asset, jid, actor_of(request), file.filename or "image", data, ok="Approved image attached; the job needs re-approval.")


@app.post("/jobs/{jid}/drafts")
def job_draft(request: HttpRequest, jid: str, prompt: str = Form("")):
    return act(f"/jobs/{jid}", service.request_image_draft, jid, actor_of(request), prompt, ok="Muse Image is drafting; this page refreshes when it is ready.")


@app.post("/drafts/{did}/approve")
def draft_approve(request: HttpRequest, did: str, job_id: str = Form(...)):
    return act(f"/jobs/{job_id}", service.approve_image_draft, did, actor_of(request), ok="Draft approved as the replacement image; the job needs re-approval.")


@app.post("/drafts/{did}/reject")
def draft_reject(request: HttpRequest, did: str, job_id: str = Form(...)):
    return act(f"/jobs/{job_id}", service.reject_image_draft, did, actor_of(request), ok="Draft rejected.")


@app.post("/jobs/{jid}/edit")
def edit(
    request: HttpRequest,
    jid: str,
    reason: str = Form(""),
    title: str = Form(""),
    page_urls: str = Form(""),
    region: str = Form(""),
    element_role: str = Form(""),
    current_value: str = Form(""),
    desired_value: str = Form(""),
    current_href: str = Form(""),
    desired_href: str = Form(""),
    viewports: list[str] = Form([]),
    recipe_id: str = Form(""),
    require_old_value_absent: str = Form(""),
    exclusions: str = Form(""),
    expected_menu_order: str = Form(""),
):
    changes = {
        "title": title.strip() or None,
        "page_urls": [u.strip() for u in page_urls.splitlines() if u.strip()] or None,
        "region": region or None,
        "element_role": element_role or None,
        "current_value": current_value.strip() or None,
        "desired_value": desired_value.strip() or None,
        "current_href": current_href.strip() or None,
        "desired_href": desired_href.strip() or None,
        "viewports": viewports or None,
        "recipe_id": recipe_id or None,
        "require_old_value_absent": require_old_value_absent == "yes",
        "exclusions": [e for e in exclusions.splitlines()],
        "expected_menu_order": [x.strip() for x in expected_menu_order.split("|") if x.strip()] or None,
    }
    return act(f"/jobs/{jid}", service.edit_job, jid, actor_of(request), changes, reason, ok="Job revised; it needs review and re-approval.")


# --- Evidence report -------------------------------------------------------------------


def report_data(s, req: Request) -> dict[str, Any]:
    jobs = list(s.scalars(select(Job).where(Job.request_id == req.id).order_by(Job.created_at)))
    items = []
    for j in jobs:
        ctx = job_context(s, j)
        items.append(ctx)
    run = s.scalar(select(ClosureRun).where(ClosureRun.request_id == req.id).order_by(ClosureRun.created_at.desc()).limit(1))
    calls = list(s.scalars(select(ModelCall).where(ModelCall.request_id == req.id)))
    return {"req": req, "items": items, "run": run, "cost": sum(c.cost_usd for c in calls), "calls": calls, "coverage": service.coverage_map(s, req) if req.requirements else {}}


@app.get("/requests/{rid}/report", response_class=HTMLResponse)
def report(request: HttpRequest, rid: str):
    with session() as s:
        req = visible(actor_of(request), s.get(Request, rid))
        return render(request, "report.html", **report_data(s, req))


@app.get("/requests/{rid}/report.json")
def report_json(request: HttpRequest, rid: str):
    with session() as s:
        req = visible(actor_of(request), s.get(Request, rid))
        data = report_data(s, req)
        out = {
            "request": {"id": req.id, "site_url": req.site_url, "status": req.status, "closed_at": req.closed_at and req.closed_at.isoformat(), "requirements": req.requirements},
            "coverage": data["coverage"],
            "closure_run": data["run"] and {"id": data["run"].id, "outcome": data["run"].outcome, "window_started_at": str(data["run"].window_started_at), "finished_at": str(data["run"].finished_at), "reasons": data["run"].reasons, "settings": data["run"].settings},
            "jobs": [
                {
                    "id": it["job"].id,
                    "title": it["job"].title,
                    "revision": it["job"].revision,
                    "status": it["job"].status,
                    "target": it["target"],
                    "verdict": it["shown"] and {"id": it["shown"].id, "result": it["shown"].result, "reasons": it["shown"].reasons, "checks": it["shown"].check_results, "regressions": it["shown"].regressions, "model": it["shown"].model},
                }
                for it in data["items"]
            ],
            "model_cost_usd": round(data["cost"], 4),
        }
        return JSONResponse(out)


@app.get("/api/requests/{rid}")
def api_request(request: HttpRequest, rid: str):
    with session() as s:
        req = visible(actor_of(request), s.get(Request, rid))
        jobs = list(s.scalars(select(Job).where(Job.request_id == rid)))
        return {"id": req.id, "status": req.status, "jobs": [{"id": j.id, "key": j.key, "status": j.status, "revision": j.revision} for j in jobs]}


@app.get("/evidence/{key:path}")
def evidence(request: HttpRequest, key: str, exp: int, sig: str):
    if not storage.verify(key, exp, sig):
        raise HTTPException(403, "link expired or invalid")
    with session() as s:  # signed and customer-scoped: the key's first segment is the request id
        visible(actor_of(request), s.get(Request, key.split("/", 1)[0]))
    try:
        path = storage.path_of(key)
    except storage.StorageError:
        raise HTTPException(404)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


# --- Metrics -----------------------------------------------------------------------------


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: HttpRequest):
    actor = actor_of(request)
    return render(request, "metrics.html", m=metrics.compute(users.accounts_of(actor)))


@app.get("/metrics.json")
def metrics_json(request: HttpRequest):
    return JSONResponse(metrics.compute(users.accounts_of(actor_of(request))))

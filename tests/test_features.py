"""End-to-end tests for image/logo, menu and contact-form verification, voice and
webhook intake, image drafts, per-customer access, redaction, retention and metrics.

Real browser captures against the demo site; Meta model calls are scripted.
"""

import base64
import io
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from feluda import llm, metrics, retention, service, storage, tasks, users
from feluda.db import session, utcnow
from feluda.records import Evidence, FormOperation, ImageDraft, Job, ModelCall, Request

from .test_e2e import judge_responder


def _text(messages):
    return " ".join(c.get("text", "") for m in messages for c in (m["content"] if isinstance(m["content"], list) else []) if isinstance(c, dict))


def _origin(messages):
    return re.search(r"SITE URL: (\S+?)/?\n", _text(messages)).group(1)


def _pages(o):
    return [f"{o}/", f"{o}/services/", f"{o}/contact/"]


def plan_with(*jobs_for_origin, requirements=None):
    def responder(model, messages):
        o = _origin(messages)
        jobs = [j(o) for j in jobs_for_origin]
        reqs = requirements or [{"key": k, "text": k, "source": k} for k in sorted({r for j in jobs for r in j["requirement_keys"]})]
        return {"summary": "test plan", "requirements": reqs, "jobs": jobs}

    return responder


def logo_job(o):
    return {
        "key": "job_logo", "title": "Replace logo", "type": "logo_change", "scope": "shared_component", "requirement_keys": ["req_1"], "recipe_id": "demo.logo",
        "targets": [{"key": "site_logo", "page_urls": _pages(o), "target_description": "site logo in the header", "region": "banner", "element_role": "image",
                     "current_text": f"{o}/media/logo-teal.png", "desired_text": "new orange logo", "desired_asset_attachment": 1, "viewports": ["desktop", "mobile"]}],
    }


def menu_rename_job(o):
    return {
        "key": "job_menu_rename", "title": "Rename Services", "type": "menu_change", "scope": "shared_component", "requirement_keys": ["req_1"], "recipe_id": "demo.menu_edit",
        "targets": [{"key": "menu_services", "page_urls": _pages(o), "target_description": "Services item in the main menu", "region": "navigation", "element_role": "link",
                     "current_text": "Services", "desired_text": "Treatments", "current_href": f"{o}/services/", "desired_href": f"{o}/services/", "viewports": ["desktop", "mobile"]}],
    }


def menu_add_job(o):
    return {
        "key": "job_menu_add", "title": "Add Book online", "type": "menu_change", "scope": "shared_component", "requirement_keys": ["req_1"], "recipe_id": "demo.menu_add",
        "targets": [{"key": "menu_book", "page_urls": [f"{o}/"], "target_description": "Book online item in the main menu", "region": "navigation", "element_role": "link",
                     "current_text": "", "desired_text": "Book online", "desired_href": f"{o}/contact/", "menu_action": "add",
                     "expected_menu_order": ["Home", "Services", "Book online", "Contact"], "viewports": ["desktop", "mobile"]}],
    }


def form_job(o):
    return {
        "key": "job_form", "title": "Fix contact form delivery", "type": "form_check", "scope": "page", "requirement_keys": ["req_1"], "recipe_id": "demo.contact_form_delivery",
        "targets": [{"key": "contact_form", "page_urls": [f"{o}/contact/"], "target_description": "contact form", "region": "main", "element_role": "form",
                     "current_text": "contact form", "desired_text": "frontdesk@brightsmile.example", "viewports": ["desktop"]}],
    }


def _start(world, scripted, plan, text="request", attachments=None):
    scripted({"submit_plan": plan, "submit_judgement": judge_responder})
    rid = service.create_request(site_url=world.url + "/", raw_text=text, actor="care.lead", attachments=attachments)
    tasks.run_until_idle()
    with session() as s:
        jobs = list(s.scalars(select(Job).where(Job.request_id == rid)))
    assert jobs and all(not j.blockers for j in jobs), [j.blockers for j in jobs]
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    with session() as s:
        jobs = list(s.scalars(select(Job).where(Job.request_id == rid)))
    assert {j.status for j in jobs} == {"ready"}, [(j.key, j.status, j.status_reason) for j in jobs]
    for j in jobs:
        service.start_job(j.id, "builder.one")
    return rid, jobs


def _verify(job_id, token):
    service.mark_done(job_id, "builder.one", token)
    tasks.run_until_idle()
    with session() as s:
        return s.get(Job, job_id)


def _media(name):
    from demo_site import app as demo

    demo.media_files()
    return (demo.MEDIA_DIR / name).read_bytes()


def test_logo_change_wrong_then_approved_image(demo_site, scripted):
    world = demo_site
    rid, (job,) = _start(world, scripted, plan_with(logo_job), attachments=[("new-logo.png", _media("logo-orange.png"))])
    assert job.targets[0]["structural_anchor"]["baseline_fps"]
    world.set(logo_src="/media/hero-smile.png")  # builder picks the wrong file
    job = _verify(job.id, "a")
    assert job.status == "failed" and "different image" in job.status_reason
    world.set(logo_src="/media/logo-orange.png")
    job = _verify(job.id, "b")
    assert job.status == "passed", job.status_reason


def test_logo_unchanged_fails_as_original(demo_site, scripted):
    rid, (job,) = _start(demo_site, scripted, plan_with(logo_job), attachments=[("new-logo.png", _media("logo-orange.png"))])
    job = _verify(job.id, "a")
    assert job.status == "failed" and "original image" in job.status_reason


def test_menu_rename_checked_on_mobile_behind_toggle(demo_site, scripted):
    world = demo_site
    rid, (job,) = _start(world, scripted, plan_with(menu_rename_job))
    world.set(menu_items=[{"label": "Home", "href": "/"}, {"label": "Treatments", "href": "/services/"}, {"label": "Contact", "href": "/contact/"}])
    job = _verify(job.id, "a")
    assert job.status == "passed", job.status_reason
    with session() as s:
        mobile = [e for e in s.scalars(select(Evidence).where(Evidence.job_id == job.id, Evidence.phase == "after", Evidence.viewport == "mobile"))]
    assert mobile and all(e.capture_settings.get("mobile_menu", {}).get("opened") for e in mobile)


def test_menu_add_wrong_order_fails_then_passes(demo_site, scripted):
    world = demo_site
    rid, (job,) = _start(world, scripted, plan_with(menu_add_job))
    world.set(menu_items=[{"label": "Home", "href": "/"}, {"label": "Services", "href": "/services/"}, {"label": "Contact", "href": "/contact/"}, {"label": "Book online", "href": "/contact/"}])
    job = _verify(job.id, "a")
    assert job.status == "failed" and "menu order" in job.status_reason
    world.set(menu_items=[{"label": "Home", "href": "/"}, {"label": "Services", "href": "/services/"}, {"label": "Book online", "href": "/contact/"}, {"label": "Contact", "href": "/contact/"}])
    job = _verify(job.id, "b")
    assert job.status == "passed", job.status_reason


def test_contact_form_delivery_and_no_resubmission(demo_site, scripted):
    world = demo_site
    world.set(form_delivery="off")  # the customer's complaint: the form shows success but no email arrives
    rid, (job,) = _start(world, scripted, plan_with(form_job))
    job = _verify(job.id, "a")
    assert job.status == "failed" and "no message" in job.status_reason.lower(), job.status_reason
    world.set(form_delivery="on")
    job = _verify(job.id, "b")
    assert job.status == "passed", job.status_reason
    inbox = httpx.get(world.url + "/__demo/inbox").json()
    assert len(inbox) == 1 and inbox[0]["to"] == "frontdesk@brightsmile.example"
    with session() as s:
        ops = list(s.scalars(select(FormOperation).where(FormOperation.job_id == job.id)))
        sub_attempt = {o.status for o in ops}
    assert len(ops) == 2 and sub_attempt == {"not_received", "received"}

    # Re-running the same attempt (a worker retry) must not submit again.
    from feluda import flowcheck, profiles
    from feluda.pipeline import job_dict, thread_capturer

    with session() as s:
        j = s.get(Job, job.id)
        latest = max(ops, key=lambda o: o.created_at)
        results, _ = flowcheck.run(
            thread_capturer(), job=job_dict(j), request_id=rid, site_url=world.url + "/", profile=profiles.get_profile("demo_dental_local"),
            submission_id=latest.submission_id, attempt_id=latest.attempt_id, task_id=None,
        )
    assert {r["result"] for r in results} == {"pass"}
    assert len(httpx.get(world.url + "/__demo/inbox").json()) == 1


def test_form_test_requires_test_account(demo_site, scripted, monkeypatch):
    from feluda import jobspec, profiles, secrets_store

    monkeypatch.setattr(secrets_store, "test_account", lambda ref: None)
    target = {"id": "f", "page_urls": [demo_site.url + "/contact/"], "target_description": "contact form", "region": "main", "element_role": "form",
              "current_value": "contact form", "desired_value": "x@y.example", "viewports": ["desktop"]}
    d = jobspec.derive_job(job_type="form_check", target=target, recipe_id="demo.contact_form_delivery", profile=profiles.get_profile("demo_dental_local"))
    assert any("test account" in b for b in d["blockers"])


def test_voice_note_transcribed_or_typed(app_env, scripted, monkeypatch):
    from feluda import intake

    calls = {}

    def plan(model, messages):
        calls["prompt"] = _text(messages)
        return {"summary": "x", "requirements": [{"key": "req_1", "text": "x", "source": "x"}], "jobs": []}

    scripted({"submit_plan": plan})
    monkeypatch.setattr(intake, "transcribe", lambda *a, **k: (_ for _ in ()).throw(intake.MediaModelUnavailable("HTTP 404")))
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64
    rid = service.create_request(site_url="https://example.com/", raw_text="", actor="care.lead", attachments=[("note.wav", wav)])
    tasks.run_until_idle()
    with session() as s:
        req = s.get(Request, rid)
        assert req.status == "awaiting_transcript" and req.channel == "voice"
        att = req.attachments[0]["id"]
    with pytest.raises(service.ActionError):
        service.submit_transcript(rid, "builder.one", att, "text")
    service.submit_transcript(rid, "care.lead", att, "Please change our phone to (555) 201-9900. My wp password: hunter2hunter2")
    tasks.run_until_idle()
    with session() as s:
        assert s.get(Request, rid).status in ("needs_review", "plan_failed")
    assert "(555) 201-9900" in calls["prompt"] and "hunter2hunter2" not in calls["prompt"]  # redacted before the model
    with session() as s:
        status = s.get(Request, rid).status
    with pytest.raises(service.ActionError, match="Re-plan"):  # a second transcript must not strand the request in parsing
        service.submit_transcript(rid, "care.lead", att, "Sorry, also the footer.")
    with session() as s:
        assert s.get(Request, rid).status == status

    monkeypatch.setattr(intake, "transcribe", lambda *a, **k: "Speaker 1: update the footer email")
    rid2 = service.create_request(site_url="https://example.com/", raw_text="", actor="care.lead", attachments=[("note.wav", wav)])
    tasks.run_until_idle()
    with session() as s:
        assert "update the footer email" in s.get(Request, rid2).raw_text


def test_email_webhook_intake(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    from feluda import web

    monkeypatch.setenv("FELUDA_INTAKE_TOKEN", "intake-test-token")
    png = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (4, 4), "red").save(png, "PNG")
    payload = {"from": "Priya <priya@brightsmile.example>", "subject": "Phone change", "text": "Please update https://example.com/ header phone.",
               "attachments": [{"filename": "shot.png", "content_type": "image/png", "data_b64": base64.b64encode(png.getvalue()).decode()}]}
    with TestClient(web.app) as c:
        assert c.post("/intake/email", json=payload).status_code == 401
        r = c.post("/intake/email", json=payload, headers={"x-feluda-intake-token": "intake-test-token"})
        assert r.status_code == 201, r.text
        no_url = c.post("/intake/email", json={**payload, "text": "update the phone please", "attachments": []}, headers={"x-feluda-intake-token": "intake-test-token"})
        assert no_url.status_code == 422
    with session() as s:
        req = s.get(Request, r.json()["request_id"])
        assert req.channel == "email" and req.site_url == "https://example.com/" and req.attachments[0]["media_type"] == "image/png"


def test_per_customer_access(app_env):
    users.ensure_user("builder.other", "builder", accounts=["acct_someone_else"])
    rid = service.create_request(site_url="https://example.com/", raw_text="x", actor="care.lead")
    with pytest.raises(service.ActionError, match="Not found"):
        service.start_job("job_unknown", "builder.other")
    users.ensure_user("lead.other", "care_lead", accounts=["acct_someone_else"])
    with pytest.raises(service.ActionError, match="Not found"):
        service.set_requirement(rid, "req_1", "lead.other", disposition="jobs")
    with pytest.raises(service.ActionError, match="no access"):
        service.create_request(site_url="https://example.com/", raw_text="x", actor="lead.other")
    assert metrics.compute(["acct_someone_else"])["requests"] == 0


def test_image_draft_approval_sets_job_asset(demo_site, scripted, monkeypatch):
    from feluda import intake

    scripted({"submit_plan": plan_with(logo_job), "submit_judgement": judge_responder})
    monkeypatch.setattr(intake, "generate_image", lambda ctx, prompt: (_media("logo-orange.png"), "image/png"))
    rid = service.create_request(site_url=demo_site.url + "/", raw_text="new logo please", actor="care.lead")
    tasks.run_until_idle()
    with session() as s:
        job = s.scalar(select(Job).where(Job.request_id == rid))
    assert any("No approved replacement image" in b for b in job.blockers)
    draft_id = service.request_image_draft(job.id, "care.lead", "A white tooth on an orange circle")
    tasks.run_until_idle()
    rev = service.approve_image_draft(draft_id, "care.lead")
    with session() as s:
        job = s.get(Job, job.id)
        assert job.revision == rev == 2 and job.blockers == [] and job.targets[0]["desired_asset_key"]
        assert s.get(ImageDraft, draft_id).status == "approved"


def test_retention_purges_old_files_and_metrics(demo_site, scripted):
    rid, (job,) = _start(demo_site, scripted, plan_with(menu_rename_job))
    demo_site.set(menu_items=[{"label": "Home", "href": "/"}, {"label": "Treatments", "href": "/services/"}, {"label": "Contact", "href": "/contact/"}])
    _verify(job.id, "a")
    m = metrics.compute(None)
    assert m["latency_s"]["all"]["n"] == 1 and m["results"]["pass"] == 1 and m["cost_usd"]["per_request"]["n"] == 1
    with session() as s:
        keys = [e.page_screenshot for e in s.scalars(select(Evidence).where(Evidence.request_id == rid)) if e.page_screenshot]
    assert keys and storage.path_of(keys[0]).exists()
    assert retention.purge(90)["requests"] == 0  # fresh evidence is kept
    out = retention.purge(90, now=utcnow() + timedelta(days=91))
    assert out["requests"] == 1 and out["files_deleted"] >= len(keys)
    assert not storage.path_of(keys[0]).exists()
    with session() as s:
        assert all(e.purged_at for e in s.scalars(select(Evidence).where(Evidence.request_id == rid)))


def test_model_call_errors_are_redacted(app_env, scripted):
    def boom(model, messages):
        raise RuntimeError("upstream rejected token sk-abcdefghijklmnopqrstuvwx for priya@brightsmile.example")

    scripted({"_text": boom})
    with pytest.raises(RuntimeError):
        llm.call(llm.CallContext("t", "v1", request_id="r"), "muse-spark-1.3", "s", "hi")
    with session() as s:
        err = s.scalar(select(ModelCall.error).order_by(ModelCall.id.desc()))
    assert "sk-abc" not in err and "priya@" not in err and "REDACTED" in err


def test_ajax_contact_form_verified_without_navigation(demo_site, scripted):
    demo_site.set(form_mode="ajax")  # Contact Form 7 style: the reply replaces the form in place
    _rid, (job,) = _start(demo_site, scripted, plan_with(form_job))
    job = _verify(job.id, "a")
    assert job.status == "passed", job.status_reason
    assert len(httpx.get(demo_site.url + "/__demo/inbox").json()) == 1
    with session() as s:
        ev = s.scalar(select(Evidence).where(Evidence.job_id == job.id, Evidence.phase == "after"))
        assert ev.final_url.endswith("/contact/")  # no navigation happened: the AJAX path ran


def test_form_recipient_compared_by_address():
    from feluda.flowcheck import _recipient_ok

    assert _recipient_ok("Front Desk <FrontDesk@brightsmile.example>", "frontdesk@brightsmile.example")
    assert _recipient_ok("a@x.example, frontdesk@brightsmile.example", "frontdesk@brightsmile.example")
    assert not _recipient_ok("Front Desk <other@brightsmile.example>", "frontdesk@brightsmile.example")


def test_email_intake_prefers_sender_account_site(app_env):
    from feluda import intake
    from feluda.records import Account

    with session() as s:
        s.add(Account(id="acct_bright", name="Bright Smile", site_origins=["https://brightsmile.example"], email_domains=["brightsmile.example"]))
    text = "Our Facebook page https://facebook.com/brightsmile shows the old phone, please fix the website too."
    assert intake.resolve_site_url(None, text, "Priya <priya@brightsmile.example>") == "https://brightsmile.example/"
    assert intake.resolve_site_url(None, "see https://brightsmile.example/contact/", "priya@brightsmile.example") == "https://brightsmile.example/contact/"
    assert intake.resolve_site_url(None, text, "someone@unknown.example") == "https://facebook.com/brightsmile"
    assert intake.find_site_url("write to info@clinic.example or visit clinic.example") == "https://clinic.example"


def test_retention_keeps_open_request_with_recent_job_activity(demo_site, scripted):
    rid, (job,) = _start(demo_site, scripted, plan_with(menu_rename_job))
    later = utcnow() + timedelta(days=200)
    with session() as s:
        s.get(Request, rid).updated_at = utcnow() - timedelta(days=200)
    with session() as s:
        s.get(Job, job.id).updated_at = later - timedelta(days=1)  # a builder touched the job recently
    assert retention.purge(90, now=later)["requests"] == 0


def test_logged_tracebacks_are_redacted():
    import logging

    from feluda.redact import RedactingFilter

    try:
        raise RuntimeError("login failed for priya@brightsmile.example password: hunter2hunter2")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), __import__("sys").exc_info())
    RedactingFilter().filter(record)
    text = logging.Formatter().format(record)
    assert "hunter2hunter2" not in text and "priya@brightsmile.example" not in text and "RuntimeError" in text


def test_redaction_keeps_uvicorn_access_log_format():
    import logging

    from uvicorn.logging import AccessFormatter

    from feluda.redact import RedactingFilter

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", "/x?email=priya@brightsmile.example", "1.1", 200), None)
    RedactingFilter().filter(record)
    text = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert "GET /x?email=[REDACTED:email]" in text and "200" in text

"""End-to-end loop against the local demo WordPress site with a scripted model.

Real Playwright captures, real database, real worker task handlers; only the
Meta model responses are scripted so the run is deterministic and free.
"""

import re

import pytest
from sqlalchemy import select

from feluda import service, tasks
from feluda.db import session
from feluda.pipeline import commit_verdict, job_dict
from datetime import timedelta

from feluda.db import utcnow
from feluda.records import ClosureRun, Job, Request, SiteLease, Submission, Verdict

REQUEST = "Hi! Our phone number changed to (555) 201-9900. Please update it in the header and the footer of the website. Thanks, Priya"


def plan_responder(model, messages):
    text = " ".join(c.get("text", "") for m in messages for c in (m["content"] if isinstance(m["content"], list) else []) if isinstance(c, dict))
    origin = re.search(r"SITE URL: (\S+?)/?\n", text).group(1)
    pages = [f"{origin}/", f"{origin}/services/", f"{origin}/contact/"]

    def job(key, region, recipe, req):
        return {
            "key": key, "title": f"Update {region} phone", "type": "text_update", "scope": "shared_component",
            "requirement_keys": [req], "recipe_id": recipe,
            "targets": [{
                "key": key.replace("job_", ""), "page_urls": pages,
                "target_description": f"phone number link in the shared site {'header' if region == 'banner' else 'footer'}",
                "region": region, "element_role": "link", "current_text": "(555) 201-4410", "desired_text": "(555) 201-9900",
                "current_href": "tel:+15552014410", "desired_href": "tel:+15552019900", "viewports": ["desktop", "mobile"],
            }],
        }

    return {
        "summary": "Change the phone number in header and footer.",
        "requirements": [
            {"key": "req_1", "text": "Update the phone number in the header to (555) 201-9900", "source": "update it in the header"},
            {"key": "req_2", "text": "Update the phone number in the footer to (555) 201-9900", "source": "and the footer"},
        ],
        "jobs": [job("job_header_phone", "banner", "demo.header_phone", "req_1"), job("job_footer_phone", "contentinfo", "demo.footer_phone", "req_2")],
    }


def judge_responder(model, messages):
    text = " ".join(c.get("text", "") for m in messages for c in (m["content"] if isinstance(m["content"], list) else []) if isinstance(c, dict))
    ids = re.findall(r"(\w+\.visual_layout)", text)
    return {"checks": [{"check_id": i, "result": "pass", "explanation": "Target shows the new number; layout intact.", "confidence": 0.92} for i in dict.fromkeys(ids)]}


def done_responder(model, messages):
    return {"subject": "Your phone number is updated", "body": "Hi Priya, the new number now shows in the header and footer. Screenshots attached."}


@pytest.fixture
def world(demo_site, scripted):
    scripted({"submit_plan": plan_responder, "submit_judgement": judge_responder, "submit_message": done_responder})
    return demo_site


def jobs_of(rid):
    with session() as s:
        return {j.key: j for j in s.scalars(select(Job).where(Job.request_id == rid))}


def test_full_loop_catches_forgotten_footer_and_closes(world):
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead", customer="Priya")
    tasks.run_until_idle()

    jobs = jobs_of(rid)
    assert set(jobs) == {"job_header_phone", "job_footer_phone"}
    for j in jobs.values():
        assert j.status == "needs_review" and j.blockers == [], j.blockers
        assert len(j.builder_steps) == 6 and "(555) 201-9900" in j.builder_steps[2]["instruction"]

    with pytest.raises(service.ActionError):
        service.approve_coverage(rid, "builder.one")  # role check
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    jobs = jobs_of(rid)
    assert {j.status for j in jobs.values()} == {"ready"}, [(j.key, j.status, j.status_reason) for j in jobs.values()]
    header, footer = jobs["job_header_phone"], jobs["job_footer_phone"]
    assert header.targets[0]["structural_anchor"]["path"]

    service.start_job(header.id, "builder.one")
    service.start_job(footer.id, "builder.two")

    # Builder fixes the header correctly but forgets the footer, then double-clicks Mark done.
    world.set(header_phone_text="(555) 201-9900", header_phone_href="tel:+15552019900")
    sub_h, created = service.mark_done(header.id, "builder.one", "tok-h1")
    assert created
    assert service.mark_done(header.id, "builder.one", "tok-h1") == (sub_h, False)
    assert service.mark_done(header.id, "builder.one", "tok-h2") == (sub_h, False)  # still queued: same attempt
    service.mark_done(footer.id, "builder.two", "tok-f1")
    tasks.run_until_idle()

    jobs = jobs_of(rid)
    assert jobs["job_header_phone"].status == "passed", jobs["job_header_phone"].status_reason
    assert jobs["job_footer_phone"].status == "failed"
    assert "still shows the old value" in jobs["job_footer_phone"].status_reason.lower()

    with pytest.raises(service.ActionError, match="not passed"):
        service.start_closure(rid, "care.lead", True)

    # Footer corrected on the same revision: a new submission, a new attempt.
    world.set(footer_phone_text="(555) 201-9900", footer_phone_href="tel:+15552019900")
    sub_f2, created = service.mark_done(footer.id, "builder.two", "tok-f2")
    assert created
    tasks.run_until_idle()
    jobs = jobs_of(rid)
    assert jobs["job_footer_phone"].status == "passed", jobs["job_footer_phone"].status_reason
    with session() as s:
        assert s.scalar(select(Request).where(Request.id == rid)).status == "ready_to_close"
        subs = list(s.scalars(select(Submission).where(Submission.job_id == footer.id)))
        assert len(subs) == 2 and len({x.attempt_id for x in subs}) == 2

    with pytest.raises(service.ActionError, match="acknowledged"):
        service.start_closure(rid, "care.lead", False)
    run_id = service.start_closure(rid, "care.lead", True)
    with pytest.raises(service.ActionError, match="freeze"):
        service.mark_done(header.id, "builder.one", "tok-during-freeze")
    with session() as s:  # the run waited in the queue past its lease TTL; nobody else took the site
        s.get(SiteLease, s.get(Request, rid).site_host).expires_at = utcnow() - timedelta(seconds=5)
    tasks.run_until_idle()
    with session() as s:
        run = s.get(ClosureRun, run_id)
        req = s.get(Request, rid)
        assert run.outcome == "closed", run.reasons
        assert req.closed_at is not None and req.status == "done"
        assert req.done_message_valid and "Priya" in req.done_message
        assert service.active_lease(s, req.site_host) is None

    # A later request editing the same shared header must not reopen this closed request.
    world.reset()
    rid2 = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    service.approve_coverage(rid2, "care.lead")
    tasks.run_until_idle()
    service.start_job(jobs_of(rid2)["job_header_phone"].id, "builder.one")
    assert {j.status for j in jobs_of(rid).values()} == {"passed"}


def test_reapproval_after_finished_baseline_does_not_stall(world):
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    service.approve_coverage(rid, "care.lead")
    service.set_requirement(rid, "req_1", "care.lead", disposition="jobs", note="")  # scope changes mid-capture
    tasks.run_until_idle()
    assert {j.status for j in jobs_of(rid).values()} == {"needs_review"}
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    assert {j.status for j in jobs_of(rid).values()} == {"ready"}


def test_capture_blocks_private_redirect_hop(world):
    from feluda.capture import Capturer
    from feluda.jobspec import locator_candidates

    t = {"id": "h", "region": "banner", "element_role": "link", "current_value": "(555) 201-4410", "desired_value": "x"}
    t["locator_candidates"] = locator_candidates(t)
    with Capturer() as cap:
        ok = cap.capture_page(world.url + "/contact", "desktop", [t], phases=["before"], masks=[])[0]
        assert ok.error is None and ok.final_url.endswith("/contact/")
        bad = cap.capture_page(world.url + "/__demo/redirect?to=http://169.254.169.254/latest/meta-data/", "desktop", [t], phases=["before"], masks=[])[0]
        assert bad.error and bad.page_png is None


def test_final_gate_catches_change_after_pass(world):
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    jobs = jobs_of(rid)
    for j in jobs.values():
        service.start_job(j.id, "builder.one")
    world.set(header_phone_text="(555) 201-9900", header_phone_href="tel:+15552019900", footer_phone_text="(555) 201-9900", footer_phone_href="tel:+15552019900")
    for j in jobs.values():
        service.mark_done(j.id, "builder.one", f"tok-{j.id}")
    tasks.run_until_idle()
    assert {j.status for j in jobs_of(rid).values()} == {"passed"}

    world.set(header_phone_text="(555) 201-990")  # someone edits the header after verification
    service.start_closure(rid, "care.lead", True)
    tasks.run_until_idle()
    jobs = jobs_of(rid)
    assert jobs["job_header_phone"].status == "with_builder"
    assert "Final consistency gate" in jobs["job_header_phone"].status_reason
    with session() as s:
        req = s.get(Request, rid)
        assert req.closed_at is None and req.status != "done"
        assert service.active_lease(s, req.site_host) is None


def test_typo_and_stale_submission_cannot_overwrite(world):
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    header = jobs_of(rid)["job_header_phone"]
    service.start_job(header.id, "builder.one")
    world.set(header_phone_text="(555) 201-990", header_phone_href="tel:+1555201990")
    sub1, _ = service.mark_done(header.id, "builder.one", "a")
    tasks.run_until_idle()
    job = jobs_of(rid)["job_header_phone"]
    assert job.status == "failed"
    assert "shows '(555) 201-990'" in job.status_reason.lower() and "link destination" in job.status_reason

    # Revising the job invalidates verdicts; a late verdict for the old submission is not committed.
    service.edit_job(header.id, "care.lead", {"desired_value": "(555) 201-9900"}, "re-review after failure")
    with session() as s:
        jd = job_dict(s.get(Job, header.id))
    jd["revision"] -= 1
    att = None
    with session() as s:
        att = s.get(Submission, sub1).attempt_id
    assert not commit_verdict(jd, sub1, att, result="pass", check_results=[], regressions=[], reasons=[], evidence_ids=[], model="", confidence=None, escalated=False)
    with session() as s:
        assert s.get(Job, header.id).status == "needs_review"
        late = s.scalar(select(Verdict).where(Verdict.submission_id == sub1, Verdict.committed.is_(False)))
        assert late is not None and late.superseded_reason
        assert service.current_approval(s, s.get(Request, rid)) is None  # revision change invalidates approval


def test_unmapped_requirement_blocks_approval(world, scripted):
    def partial_plan(model, messages):
        p = plan_responder(model, messages)
        p["jobs"] = p["jobs"][:1]  # planner forgot the footer
        return p

    scripted({"submit_plan": partial_plan, "submit_judgement": judge_responder})
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    with pytest.raises(service.ActionError, match="req_2"):
        service.approve_coverage(rid, "care.lead")
    with pytest.raises(service.ActionError, match="reason"):
        service.set_requirement(rid, "req_2", "care.lead", disposition="excluded")
    service.set_requirement(rid, "req_2", "care.lead", disposition="excluded", note="Customer confirmed footer is handled separately")
    service.approve_coverage(rid, "care.lead")


def _ready_header(world):
    rid = service.create_request(site_url=world.url + "/", raw_text=REQUEST, actor="care.lead")
    tasks.run_until_idle()
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    header = jobs_of(rid)["job_header_phone"]
    service.start_job(header.id, "builder.one")
    return rid, header


def test_infrastructure_error_then_retry(world, monkeypatch):
    from feluda import capture

    rid, header = _ready_header(world)
    world.set(header_phone_text="(555) 201-9900", header_phone_href="tel:+15552019900")
    real = capture.Capturer.capture_page

    def broken(self, page_url, viewport, targets, **kw):
        out = real(self, page_url, viewport, targets, **kw)
        for r in out:
            r.error = "TimeoutError: simulated browser outage"
        return out

    monkeypatch.setattr(capture.Capturer, "capture_page", broken)
    service.mark_done(header.id, "builder.one", "t1")
    tasks.run_until_idle()
    job = jobs_of(rid)["job_header_phone"]
    assert job.status == "verification_error" and job.attempts == 0  # not counted as a builder failure
    with session() as s:
        assert service.current_verdict(s, job).result == "verification_error"

    monkeypatch.setattr(capture.Capturer, "capture_page", real)
    service.retry_verification(header.id, "builder.one")
    tasks.run_until_idle()
    job = jobs_of(rid)["job_header_phone"]
    assert job.status == "passed", job.status_reason
    with session() as s:  # same attempt resumed, not a new submission
        assert len(list(s.scalars(select(Submission).where(Submission.job_id == header.id)))) == 1


def test_needs_human_cannot_be_passed_manually(world):
    rid, header = _ready_header(world)
    world.set(header_phone_text="(555) 201-9900", header_phone_href="tel:+15552019900", hours_text="Mon–Fri 9am–5pm")
    service.mark_done(header.id, "builder.one", "t1")
    tasks.run_until_idle()
    job = jobs_of(rid)["job_header_phone"]
    assert job.status == "needs_human" and "Unexplained change" in job.status_reason
    with pytest.raises(service.ActionError, match="Unknown action"):
        service.resolve_needs_human(header.id, "care.lead", "mark_passed", "looks fine")
    with pytest.raises(service.ActionError):
        service.resolve_needs_human(header.id, "builder.one", "return_to_builder", "x")
    service.resolve_needs_human(header.id, "care.lead", "return_to_builder", "Hours were changed by mistake; restore them")
    world.set(hours_text="Mon–Fri 8am–6pm, Sat 9am–1pm")
    service.mark_done(header.id, "builder.one", "t2")
    tasks.run_until_idle()
    assert jobs_of(rid)["job_header_phone"].status == "passed"

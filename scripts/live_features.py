"""Exercise the image/logo, menu, contact-form, Muse Image and voice paths against
live Meta models and the local demo site (``uvicorn demo_site.app:app --port 8100``).

    python -m scripts.live_features
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

SITE = "http://localhost:8100"


def main() -> int:
    os.environ.setdefault("FELUDA_DATA_DIR", "var/live_features")
    os.environ.setdefault("FELUDA_APPROVED_TEST_ORIGINS", SITE)
    from sqlalchemy import func, select

    from feluda import secrets_store, service, tasks
    from feluda.db import init_db, session
    from feluda.records import FormOperation, ImageDraft, Job, ModelCall, Request
    from feluda.users import ensure_dev_users

    init_db()
    ensure_dev_users()
    secrets_store.write_secrets({"test_accounts": {"demo_dental_local": {"name": "Website Care Test", "email": "care-test@feluda.example"}}})
    httpx.post(SITE + "/__demo/reset").raise_for_status()
    ok = True

    def jobs(rid):
        with session() as s:
            return list(s.scalars(select(Job).where(Job.request_id == rid).order_by(Job.created_at)))

    def show(rid, label):
        with session() as s:
            req = s.get(Request, rid)
            print(f"\n--- {label}: request {req.status} | {req.planner_notes[:160]} {req.status_reason[:200]}")
        for j in jobs(rid):
            t = j.targets[0]
            print(f"  {j.type:<14} {j.status:<18} r{j.revision} {t['region']:<11} {t['current_value'][:40]!r} -> {t['desired_value'][:40]!r} recipe={(j.recipe_snapshot or {}).get('recipe_id')}")
            for b in j.blockers:
                print(f"      blocker: {b}")
            if j.status not in ("ready", "passed", "with_builder"):
                print(f"      {j.status_reason[:300]}")

    def plan_and_approve(text, attachments=None):
        rid = service.create_request(site_url=SITE + "/", raw_text=text, actor="care.lead", customer="Priya", attachments=attachments)
        tasks.run_until_idle()
        with session() as s:
            for r in s.get(Request, rid).requirements:
                if r["disposition"] != "jobs":
                    service.set_requirement(rid, r["id"], "care.lead", disposition="excluded", note=r.get("note") or "reviewed")
        try:
            service.approve_coverage(rid, "care.lead")
        except service.ActionError as e:
            print("  approve failed:", e)
        tasks.run_until_idle()
        return rid

    def verify_all(rid, token):
        """Start and verify jobs in dependency order (a job waits for its prerequisites).
        Each job is marked done at most once per call, as a builder would."""
        submitted: set[str] = set()
        for round_ in range(8):
            progressed = False
            for j in jobs(rid):
                if j.status == "ready":
                    try:
                        service.start_job(j.id, "builder.one")
                        progressed = True
                    except service.ActionError as e:
                        if "prerequisite" not in str(e) and "same shared" not in str(e):
                            raise
            for j in jobs(rid):
                # A shared-component edit can send a passed job back for re-verification once.
                if j.status == "with_builder" or (j.status == "failed" and j.id not in submitted):
                    service.mark_done(j.id, "builder.one", f"{j.id}-{token}-{round_}")
                    submitted.add(j.id)
                    progressed = True
            tasks.run_until_idle()
            if not progressed:
                break

    # 1. Logo from an attached image ------------------------------------------------------
    httpx.get(SITE + "/media/logo-orange.png")
    logo = (Path("demo_site/media") / "logo-orange.png").read_bytes()
    rid = plan_and_approve("Hi! We've refreshed our branding. Please replace the logo at the top of the site with the attached orange logo. Thanks, Priya", [("new-logo.png", logo)])
    show(rid, "Logo (planned)")
    httpx.post(SITE + "/__demo/set", json={"logo_src": "/media/logo-orange.png"}).raise_for_status()
    verify_all(rid, "1")
    show(rid, "Logo (after correct swap)")
    ok &= all(j.status == "passed" for j in jobs(rid))
    logo_job = next((j for j in jobs(rid) if j.type in ("logo_change", "image_swap")), None)
    if logo_job:
        draft_id = service.request_image_draft(logo_job.id, "care.lead", "A simple flat logo: a white tooth on an orange circle, no text, white background")
        tasks.run_until_idle()
        with session() as s:
            d = s.get(ImageDraft, draft_id)
            print(f"  Muse Image draft: {d.status} {d.key or d.error}")
            ok &= d.status == "draft"

    # 2. Menu rename + addition -----------------------------------------------------------
    rid = plan_and_approve("Please rename the 'Services' menu item to 'Treatments', and add a new menu item 'Book online' that goes to the contact page, placed right after Treatments.")
    show(rid, "Menu (planned)")
    httpx.post(SITE + "/__demo/set", json={"menu_items": [
        {"label": "Home", "href": "/"}, {"label": "Treatments", "href": "/services/"}, {"label": "Book online", "href": "/contact/"}, {"label": "Contact", "href": "/contact/"}]}).raise_for_status()
    verify_all(rid, "1")
    show(rid, "Menu (after edit)")
    ok &= all(j.status == "passed" for j in jobs(rid))

    # 3. Contact form that shows success but never delivers --------------------------------
    httpx.post(SITE + "/__demo/set", json={"form_delivery": "off"}).raise_for_status()
    rid = plan_and_approve("Our contact form says 'sent' but we never receive the messages at frontdesk@brightsmile.example. Can you fix it?")
    show(rid, "Form (planned)")
    verify_all(rid, "1")
    show(rid, "Form (builder forgot to switch delivery on)")
    httpx.post(SITE + "/__demo/set", json={"form_delivery": "on"}).raise_for_status()
    verify_all(rid, "2")
    show(rid, "Form (after fix)")
    ok &= all(j.status == "passed" for j in jobs(rid))
    with session() as s:
        ops = list(s.scalars(select(FormOperation)))
        print("  form operations:", [(o.correlation_id, o.status) for o in ops])
    print("  test inbox messages:", len(httpx.get(SITE + "/__demo/inbox").json()))

    # 4. Voice note -------------------------------------------------------------------------
    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if wav and wav.exists():
        rid = service.create_request(site_url=SITE + "/", raw_text="", actor="care.lead", attachments=[(wav.name, wav.read_bytes())])
        tasks.run_until_idle()
        with session() as s:
            req = s.get(Request, rid)
            print(f"\n--- Voice note: {req.status} | {req.status_reason[:200]}")

    with session() as s:
        rows = s.execute(select(ModelCall.purpose, ModelCall.model, func.count(), func.sum(ModelCall.cost_usd)).group_by(ModelCall.purpose, ModelCall.model)).all()
    print("\nModel calls:")
    for purpose, model, n, cost in rows:
        print(f"  {purpose:<20} {model:<26} {n:>3} ${cost or 0:.4f}")
    print("\nALL PASSED" if ok else "\nSOME SCENARIOS DID NOT PASS")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

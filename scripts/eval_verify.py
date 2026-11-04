"""Paired correct/planted-error verification suite on the demo site.

Each case resets the demo site, plans a fixed header-phone job (scripted plan,
so only verification is under test), captures the baseline, applies a builder
outcome, marks done and records the verdict. The release gate is zero false
passes; review/error outcomes on bad changes block them but are reported
separately from explicit failures, as the spec requires.

    python -m scripts.eval_verify                 # live Spark 1.3 judge (+ SAM)
    python -m scripts.eval_verify --offline       # scripted judge, no API spend
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

SITE = "http://localhost:8100"
NEW = {"header_phone_text": "(555) 201-9900", "header_phone_href": "tel:+15552019900"}

# (name, defect type, expected outcome, site changes, enable cache first)
CASES = [
    ("correct change", "none", "pass", NEW, False),
    ("typo in number", "wrong_value", "fail", {**NEW, "header_phone_text": "(555) 201-990"}, False),
    ("extra digit", "wrong_value", "fail", {**NEW, "header_phone_text": "(555) 201-99001"}, False),
    ("text right, old tel: link", "wrong_link", "fail", {"header_phone_text": "(555) 201-9900"}, False),
    ("link right, old text", "wrong_value", "fail", {"header_phone_href": "tel:+15552019900"}, False),
    ("nothing changed", "omitted", "fail", {}, False),
    ("correct + unrelated footer hours edit", "regression", "needs_human", {**NEW, "hours_text": "Mon–Fri 9am–5pm"}, False),
    ("correct + home button relabelled", "regression", "needs_human", {**NEW, "home_button_label": "Call us now"}, False),
    ("correct but stale page cache", "stale_cache", "needs_human", NEW, True),
]


def plan(origin):
    pages = [f"{origin}/", f"{origin}/services/", f"{origin}/contact/"]
    return {
        "summary": "Change the header phone.",
        "requirements": [{"key": "req_1", "text": "Header phone to (555) 201-9900", "source": "header"}],
        "jobs": [{
            "key": "job_header_phone", "title": "Update header phone", "type": "text_update", "scope": "shared_component",
            "requirement_keys": ["req_1"], "recipe_id": "demo.header_phone",
            "targets": [{
                "key": "header_phone", "page_urls": pages, "target_description": "phone number link in the shared site header",
                "region": "banner", "element_role": "link", "current_text": "(555) 201-4410", "desired_text": "(555) 201-9900",
                "current_href": "tel:+15552014410", "desired_href": "tel:+15552019900", "viewports": ["desktop", "mobile"],
            }],
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="", help="only run cases whose name contains this text")
    ap.add_argument("--offline", action="store_true", help="scripted judge (always passes visuals): tests the deterministic layer only")
    args = ap.parse_args()
    os.environ.setdefault("FELUDA_DATA_DIR", "var/eval_verify")
    os.environ.setdefault("FELUDA_APPROVED_TEST_ORIGINS", SITE)
    os.environ.setdefault("FELUDA_FRESHNESS_RETRY_DELAY_S", "1")
    if args.offline:
        os.environ["FELUDA_USE_SAM"] = "0"

    from sqlalchemy import func, select

    from feluda import db, llm, service, settings, tasks
    from feluda.users import ensure_dev_users
    from feluda.db import init_db, session
    from feluda.records import Job, ModelCall, Verdict

    real = llm.gateway()

    class Gateway:
        def create(self, *, model, system, messages, tools, max_tokens):
            name = tools[0]["name"] if tools else ""
            if name == "submit_plan":
                return llm.ModelReply(model=model, text="", tool_calls=[{"name": name, "input": plan(SITE)}])
            if name == "submit_judgement" and args.offline:
                checks = [{"check_id": "header_phone.visual_layout", "result": "pass", "explanation": "scripted", "confidence": 0.9}]
                return llm.ModelReply(model=model, text="", tool_calls=[{"name": name, "input": {"checks": checks}}])
            return real.create(model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens)

    llm.set_gateway(Gateway())
    base_dir = os.environ["FELUDA_DATA_DIR"]
    rows = []
    total_cost = 0.0
    for index, (name, defect, expected, changes, cache) in enumerate(CASES):
        if args.case and args.case not in name:
            continue
        # Each case gets its own database: cases share one site, and a new job on the
        # same shared header would (correctly) invalidate and block the previous case's job.
        settings.use_settings(settings.load_app_settings({**os.environ, "FELUDA_DATA_DIR": f"{base_dir}/{time.strftime('%Y%m%d-%H%M%S')}-{index}"}))
        db.reset_engine()
        init_db()
        ensure_dev_users()
        httpx.post(SITE + "/__demo/reset").raise_for_status()
        t0 = time.time()
        rid = service.create_request(site_url=SITE + "/", raw_text=f"[eval] {name}", actor="care.lead")
        tasks.run_until_idle()
        service.approve_coverage(rid, "care.lead")
        tasks.run_until_idle()
        with session() as s:
            job = s.scalar(select(Job).where(Job.request_id == rid))
        if job.status != "ready":
            rows.append((name, defect, expected, f"setup:{job.status}", 0.0))
            continue
        service.start_job(job.id, "builder.one")
        if cache:
            httpx.post(SITE + "/__demo/set", json={"cache_enabled": True}).raise_for_status()
            for p in ("/", "/services/", "/contact/"):
                httpx.get(SITE + p)  # warm the cache with the old page
        if changes:
            httpx.post(SITE + "/__demo/set", json=changes).raise_for_status()
        service.mark_done(job.id, "builder.one", f"eval-{rid}")
        tasks.run_until_idle()
        with session() as s:
            v = s.scalar(select(Verdict).where(Verdict.job_id == job.id, Verdict.committed.is_(True)).order_by(Verdict.created_at.desc()))
            got = v.result if v else "none"
        with session() as s:
            total_cost += s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0))) or 0.0
        rows.append((name, defect, expected, got, time.time() - t0))
        print(f"{name:<40} expected={expected:<12} got={got:<18} {time.time() - t0:5.1f}s", flush=True)

    bad = [r for r in rows if r[2] != "pass"]
    false_pass = [r for r in bad if r[3] == "pass"]
    explicit = [r for r in bad if r[2] == "fail" and r[3] == "fail"]
    correct = [r for r in rows if r[2] == "pass"]
    print("\nCorrect changes passed:      %d/%d" % (sum(r[3] == "pass" for r in correct), len(correct)))
    print("Planted errors explicitly failed: %d/%d (expected-fail cases)" % (len(explicit), sum(r[2] == "fail" for r in rows)))
    print("Regression/stale cases routed to review: %d/%d" % (sum(r[3] == "needs_human" for r in rows if r[2] == "needs_human"), sum(r[2] == "needs_human" for r in rows)))
    print("FALSE PASSES: %d" % len(false_pass))
    print(f"Model cost for this run: ${total_cost:.4f}")
    mismatches = [r for r in rows if r[2] != r[3]]
    for r in mismatches:
        print("  mismatch:", r)
    return 1 if false_pass else 0


if __name__ == "__main__":
    sys.exit(main())

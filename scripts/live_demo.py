"""Run the three-minute demo script end to end against live Meta models.

Requires the demo site (``uvicorn demo_site.app:app --port 8100``) and
FELUDA_APPROVED_TEST_ORIGINS=http://localhost:8100. Uses its own data dir
(var/live_demo) so it does not touch the dashboard database unless asked:

    python -m scripts.live_demo                 # isolated run
    python -m scripts.live_demo --dashboard     # write into the dashboard database (var/)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
from sqlalchemy import func, select

SITE = "http://localhost:8100"
REQUEST = (
    "Hi team, our clinic phone number has changed to (555) 201-9900. "
    "Can you please update it at the top of the website and in the footer at the bottom? "
    "Keep the email as it is. Thanks! – Priya, Bright Smile Dental"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", action="store_true", help="use the dashboard database instead of var/live_demo")
    args = ap.parse_args()
    if not args.dashboard:
        os.environ.setdefault("FELUDA_DATA_DIR", "var/live_demo")
    os.environ.setdefault("FELUDA_APPROVED_TEST_ORIGINS", SITE)

    from feluda import service, tasks
    from feluda.db import init_db, session
    from feluda.records import ClosureRun, Job, ModelCall, Request

    def step(msg):
        print(f"\n=== {msg}", flush=True)

    def show(rid):
        with session() as s:
            for j in s.scalars(select(Job).where(Job.request_id == rid).order_by(Job.created_at)):
                t = j.targets[0]
                print(f"  {j.key:<28} {j.status:<18} r{j.revision} {t['region']:<12} {t['current_value']!r} -> {t['desired_value']!r} recipe={(j.recipe_snapshot or {}).get('recipe_id')}")
                for b in j.blockers:
                    print(f"      blocker: {b}")
                if j.status_reason and j.status not in ("ready", "passed"):
                    print(f"      reason: {j.status_reason[:400]}")

    init_db()
    from feluda.users import ensure_dev_users

    ensure_dev_users()
    httpx.post(SITE + "/__demo/reset").raise_for_status()
    t0 = time.time()

    step("1. Customer request in; planner (Muse Spark 1.3) reads the site")
    rid = service.create_request(site_url=SITE + "/", raw_text=REQUEST, actor="care.lead", customer="Priya")
    tasks.run_until_idle()
    with session() as s:
        req = s.get(Request, rid)
        print(" ", req.status, "|", req.planner_notes, "|", req.status_reason)
        for r in req.requirements:
            print(f"  {r['id']}: {r['text']}  [{r['disposition']}] {r.get('note', '')}")
    show(rid)

    step("2. Care lead approves coverage; baseline capture (DOM + SAM 3.1)")
    with session() as s:
        req = s.get(Request, rid)
        for r in req.requirements:
            if r["disposition"] != "jobs":
                note = r.get("note") or "Reviewed: customer asked to keep this unchanged"
                service.set_requirement(rid, r["id"], "care.lead", disposition="excluded", note=note)
    service.approve_coverage(rid, "care.lead")
    tasks.run_until_idle()
    show(rid)

    with session() as s:
        jobs = {j.targets[0]["region"]: j.id for j in s.scalars(select(Job).where(Job.request_id == rid))}
    header, footer = jobs.get("banner"), jobs.get("contentinfo")
    if not header or not footer:
        print("Planner did not produce separate header and footer jobs; stopping.")
        return 1

    step("3. Builder fixes the header correctly but forgets the footer; marks both done")
    for jid in (header, footer):
        service.start_job(jid, "builder.one")
    httpx.post(SITE + "/__demo/set", json={"header_phone_text": "(555) 201-9900", "header_phone_href": "tel:+15552019900"}).raise_for_status()
    for jid in (header, footer):
        service.mark_done(jid, "builder.one", f"{jid}-1")
    tasks.run_until_idle()
    show(rid)

    step("4. Closure is blocked; builder corrects the footer and re-verifies")
    try:
        service.start_closure(rid, "care.lead", True)
        print("  UNEXPECTED: closure allowed")
    except service.ActionError as e:
        print("  closure blocked:", str(e)[:200])
    httpx.post(SITE + "/__demo/set", json={"footer_phone_text": "(555) 201-9900", "footer_phone_href": "tel:+15552019900"}).raise_for_status()
    service.mark_done(footer, "builder.one", f"{footer}-2")
    tasks.run_until_idle()
    show(rid)

    step("5. Final consistency gate and drafted done message")
    run_id = service.start_closure(rid, "care.lead", True)
    tasks.run_until_idle()
    with session() as s:
        run = s.get(ClosureRun, run_id)
        req = s.get(Request, rid)
        print("  closure:", run.outcome, run.reasons)
        print("  request:", req.status)
        print("\n" + (req.done_message or "(no message)"))
        calls = list(s.scalars(select(ModelCall).where(ModelCall.request_id == rid)))
        total = s.scalar(select(func.sum(ModelCall.cost_usd)).where(ModelCall.request_id == rid)) or 0
    step(f"Model calls: {len(calls)}, estimated cost ${total:.4f}, wall time {time.time() - t0:.0f}s")
    by = {}
    for c in calls:
        k = (c.purpose, c.model)
        n, cost, ok = by.get(k, (0, 0.0, 0))
        by[k] = (n + 1, cost + c.cost_usd, ok + (0 if c.ok else 1))
    for (purpose, model), (n, cost, failed) in sorted(by.items()):
        print(f"  {purpose:<20} {model:<18} calls={n:<3} failed={failed:<2} ${cost:.4f}")
    print(f"\nDashboard: http://localhost:8000/requests/{rid}" if args.dashboard else "")
    return 0 if run.outcome == "closed" else 2


if __name__ == "__main__":
    sys.exit(main())

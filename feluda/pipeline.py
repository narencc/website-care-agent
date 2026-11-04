"""Background task handlers: plan, baseline capture, verification and the final consistency gate."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from . import checks, judge, llm, planner, profiles, redact, sam, storage
from .capture import Capturer, PageCapture
from .db import new_id, session, utcnow
from .jobspec import derive_job, normalize_href, target_from_plan
from .records import ClosureRun, Evidence, Job, Request, Submission, Task, Verdict
from .schemas import DoneMessage
from .service import (
    LeaseLost,
    current_approval,
    current_verdict,
    invalidate_verdicts,
    log_event,
    refresh_request_status,
    release_lease,
    renew_lease,
    transition,
)
from .settings import app_settings
from .sitefetch import crawl
from .tasks import PermanentError, handler

log = logging.getLogger("feluda.pipeline")

_local = threading.local()


def thread_capturer() -> Capturer:
    cap = getattr(_local, "capturer", None)
    if cap is None:
        cap = Capturer().__enter__()
        _local.capturer = cap
    return cap


def close_thread_capturer() -> None:
    cap = getattr(_local, "capturer", None)
    if cap is not None:
        _local.capturer = None
        try:
            cap.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "request_id": job.request_id,
        "title": job.title,
        "revision": job.revision,
        "type": job.type,
        "scope": job.scope,
        "targets": [dict(t) for t in job.targets],
        "acceptance_checks": [dict(c) for c in job.acceptance_checks],
        "builder_steps": list(job.builder_steps),
        "attempts": job.attempts,
        "escalations": job.escalations,
    }


# --- Capture + evidence ----------------------------------------------------------


def _obs(pc: PageCapture, evidence_id: str | None) -> checks.Observation:
    el = pc.element
    return checks.Observation(
        target_id=pc.target_id,
        page_url=pc.page_url,
        viewport=pc.viewport,
        final_url=pc.final_url,
        error=pc.error,
        matched_phase=el.matched_phase if el else None,
        match_count=el.match_count if el else 0,
        ambiguous=bool(el and el.ambiguous),
        info=el.info if el else None,
        interactable=el.interactable if el else None,
        regions_text=pc.regions_text,
        http=pc.http,
        evidence_id=evidence_id,
    )


def evidence_observation(ev: Evidence) -> checks.Observation:
    return checks.Observation(
        target_id=ev.target_id,
        page_url=ev.requested_url,
        viewport=ev.viewport,
        final_url=ev.final_url,
        error=ev.error,
        matched_phase=ev.locate.get("matched_phase"),
        match_count=ev.locate.get("match_count", 0),
        ambiguous=bool(ev.locate.get("ambiguous")),
        info=ev.locate.get("info"),
        interactable=ev.element_state.get("interactable"),
        regions_text=ev.regions_text,
        http=ev.http,
        evidence_id=ev.id,
    )


NAMED_COLOURS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128), "red": (200, 30, 30), "orange": (230, 110, 20),
    "yellow": (240, 200, 40), "green": (30, 140, 60), "teal": (15, 110, 120), "blue": (30, 90, 200), "navy": (20, 40, 90),
    "purple": (120, 50, 160), "pink": (230, 100, 170), "brown": (120, 70, 30),
}


def _rgb(css: str | None) -> tuple[int, int, int, float] | None:
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", css or "")
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3]), float(m[4]) if m[4] is not None else 1.0


def sam_prompt(info: dict[str, Any]) -> str | None:
    """A short visual noun phrase for SAM 3.1, or None when SAM cannot help.

    Probing showed SAM 3.1 grounds visual concepts ("orange button") but finds
    nothing for semantic phrases or small plain-text links in screenshots, so
    plain text targets rely on the DOM alone (the spec's DOM-first rule).
    """
    bg = _rgb(info.get("background"))
    if not bg or bg[3] < 0.5:
        return None
    name = min(NAMED_COLOURS, key=lambda n: sum((a - b) ** 2 for a, b in zip(NAMED_COLOURS[n], bg[:3])))
    return f"{name} button"


def _sam_supplement(ctx: llm.CallContext, pc: PageCapture) -> dict[str, Any] | None:
    if not app_settings().use_sam or not pc.context_png or not (pc.element and pc.element.info):
        return None
    info = pc.element.info
    b = info["bbox"]
    ox, oy = pc.context_offset
    dom = {"x1": b["x"] - ox, "y1": b["y"] - oy, "x2": b["x"] - ox + b["w"], "y2": b["y"] - oy + b["h"]}
    prompt = sam_prompt(info)
    if prompt is None:
        return {"skipped": "plain-text element; SAM 3.1 does not localize small text reliably, DOM location used", "dom_box": dom}
    try:
        boxes = sam.detect(ctx, pc.context_png, prompt)
    except llm.BudgetExceeded:
        raise
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "dom_box": dom, "prompt": prompt}
    scored = sorted(({**bx.as_dict(), "iou": round(sam.iou(bx.as_dict(), dom), 3)} for bx in boxes), key=lambda d: -d["iou"])
    best = scored[0]["iou"] if scored else 0.0
    return {"boxes": scored[:5], "dom_box": dom, "best_iou": best, "agrees_with_dom": best >= 0.3, "prompt": prompt}


def _persist(
    pc: PageCapture,
    *,
    request_id: str,
    job: dict[str, Any],
    phase: str,
    submission_id: str | None,
    attempt_id: str | None,
    closure_run_id: str | None,
    task_id: str | None,
    sam_box: dict[str, Any] | None,
    retries: int,
) -> str:
    ev_id = new_id("ev")
    base = f"{request_id}/{job['id']}/{ev_id}"
    page_key = storage.put(f"{base}/page.png", pc.page_png) if pc.page_png else None
    crop_key = storage.put(f"{base}/element.png", pc.crop_png) if pc.crop_png else None
    ctx_key = storage.put(f"{base}/context.png", pc.context_png) if pc.context_png else None
    el = pc.element
    target = next(t for t in job["targets"] if t["id"] == pc.target_id)
    obs = _obs(pc, ev_id)
    with session() as s:
        s.add(
            Evidence(
                id=ev_id,
                request_id=request_id,
                job_id=job["id"],
                job_revision=job["revision"],
                submission_id=submission_id,
                attempt_id=attempt_id,
                closure_run_id=closure_run_id,
                task_id=task_id,
                phase=phase,
                target_id=pc.target_id,
                requested_url=pc.page_url,
                final_url=pc.final_url,
                viewport=pc.viewport,
                page_screenshot=page_key,
                element_crop=crop_key,
                context_crop=ctx_key,
                sam_box=sam_box,
                dom_snippet=(el.info or {}).get("outer_html", "") if el else "",
                element_text=(el.info or {}).get("text") if el else None,
                element_href=(el.info or {}).get("href") if el else None,
                element_state={"interactable": el.interactable if el else None, "visible": (el.info or {}).get("visible") if el else None},
                locate={
                    "matched_phase": el.matched_phase if el else None,
                    "match_count": el.match_count if el else 0,
                    "ambiguous": bool(el and el.ambiguous),
                    "tried": el.tried if el else [],
                    "info": el.info if el else None,
                },
                regions_text=pc.regions_text,
                content_hash=checks.content_hash(pc.regions_text) if pc.regions_text else "",
                fingerprint=checks.fingerprint(obs, target["region"]) if not pc.error else "",
                http={**pc.http, "requested_with": pc.requested_url, "freshness_retries": retries},
                capture_settings=pc.settings,
                error=pc.error,
            )
        )
    return ev_id


def capture_job(
    ctx: llm.CallContext,
    job: dict[str, Any],
    *,
    request_id: str,
    phase: str,
    masks: list[str],
    submission_id: str | None = None,
    attempt_id: str | None = None,
    closure_run_id: str | None = None,
    task_id: str | None = None,
    freshness: bool = False,
    use_sam: bool = True,
) -> list[checks.Observation]:
    cfg = app_settings()
    cap = thread_capturer()
    phases = ["before"] if phase == "baseline" else ["after", "before", "structural"]
    results: list[checks.Observation] = []
    retry_budget = cfg.freshness_retries if freshness else 0  # shared: a stale cache affects every page alike
    for target in job["targets"]:
        for page_index, page_url in enumerate(target["page_urls"]):
            for vp in target.get("viewports") or ["desktop"]:
                pc = cap.capture_page(page_url, vp, [target], phases=phases, masks=masks)[0]
                retries = 0
                while retry_budget > 0 and checks.needs_fresh_capture(_obs(pc, None), target):
                    retries += 1
                    retry_budget -= 1
                    time.sleep(cfg.freshness_retry_delay_s)
                    pc = cap.capture_page(page_url, vp, [target], phases=phases, masks=masks, cache_bust=f"{attempt_id or 'x'}-{retries}")[0]
                # SAM is a bounded visual supplement: first page per viewport only.
                sam_box = _sam_supplement(ctx, pc) if use_sam and page_index == 0 and not pc.error else None
                ev_id = _persist(
                    pc,
                    request_id=request_id,
                    job=job,
                    phase=phase,
                    submission_id=submission_id,
                    attempt_id=attempt_id,
                    closure_run_id=closure_run_id,
                    task_id=task_id,
                    sam_box=sam_box,
                    retries=retries,
                )
                results.append(_obs(pc, ev_id))
    return results


def _masks(request_profile_id: str | None) -> list[str]:
    return list((profiles.get_profile(request_profile_id) or {}).get("dynamic_masks", []))


# --- Plan ----------------------------------------------------------------------------


def _plan_failed(request_id: str, why: str) -> None:
    with session() as s:
        req = s.get(Request, request_id)
        req.status = "plan_failed"
        req.status_reason = why


@handler("plan_request", on_dead=lambda s, t: _mark_plan_dead(s, t))
def handle_plan(task: Task) -> dict[str, Any]:
    request_id = task.payload["request_id"]
    with session() as s:
        req = s.get(Request, request_id)
        if req.scope_revision != task.payload["scope_revision"]:
            return {"superseded": True}
        site_url, raw_text, profile_id, attachments = req.site_url, req.raw_text, req.site_profile_id, list(req.attachments)
    profile = profiles.get_profile(profile_id)
    outlines, manifest = crawl(site_url, page_limit=app_settings().crawl_page_limit)
    if not outlines:
        raise RuntimeError(f"could not fetch any page of {site_url}: {manifest.get('errors')}")
    region_texts: dict[str, dict[str, str]] = {}
    for o in outlines:
        region_texts[o.final_url] = o.region_texts()
        region_texts[o.url] = o.region_texts()
    manifest["outlines"] = region_texts
    images = [(storage.get(a["key"]), a["media_type"]) for a in attachments if a["media_type"].startswith("image/")][:4]
    ctx = llm.CallContext("plan", planner.PROMPT_VERSION, request_id=request_id)
    try:
        result = planner.plan(ctx, redact.for_model(raw_text), site_url, outlines, profile, images)
    except (llm.BudgetExceeded, llm.ModelOutputError) as e:
        _plan_failed(request_id, f"Planner could not produce a valid plan: {e}")
        return {"failed": str(e)}

    out = result.output
    final_for = {o.url: o.final_url for o in outlines}
    with session() as s:
        req = s.get(Request, request_id)
        if req.scope_revision != task.payload["scope_revision"]:
            return {"superseded": True}
        job_reqs = {k for j in out.jobs for k in j.requirement_keys}
        clar = {c.requirement_key: c.question for c in out.clarifications}
        excl = {e.requirement_key: e.reason for e in out.exclusions}
        requirements = []
        for r in out.requirements:
            if r.key in job_reqs:
                disp, note = "jobs", ""
            elif r.key in excl:
                disp, note = "excluded", f"Planner proposed exclusion (needs Care lead review): {excl[r.key]}"
            elif r.key in clar:
                disp, note = "clarification", clar[r.key]
            else:
                disp, note = "unmapped", "Planner did not map this requirement."
            requirements.append({"id": r.key, "text": r.text, "source": r.source, "disposition": disp, "note": note})
        # Planner-proposed exclusions still need a human decision before approval.
        for r in requirements:
            if r["disposition"] == "excluded":
                r["disposition"] = "clarification"
        req.requirements = requirements
        req.crawl_manifest = manifest
        req.planner_notes = f"{out.summary} (planned by {result.model}{', escalated' if result.escalated else ''})"

        key_to_id: dict[str, str] = {}
        created: list[tuple[Job, list[str]]] = []
        for pj in out.jobs:
            target = target_from_plan(pj.targets[0], attachments)
            target["page_urls"] = [final_for.get(u, u) for u in target["page_urls"]]
            derived = derive_job(
                job_type=pj.type,
                target=target,
                recipe_id=pj.recipe_id,
                profile=profile,
                require_old_value_absent=pj.require_old_value_absent,
                page_outlines=region_texts,
            )
            job = Job(
                id=new_id("job"),
                request_id=request_id,
                key=pj.key,
                title=pj.title,
                type=pj.type,
                scope=pj.scope,
                site_profile_id=profile_id,
                requirement_ids=[k for k in pj.requirement_keys if any(r["id"] == k for r in requirements)],
                targets=derived["targets"],
                acceptance_checks=derived["acceptance_checks"],
                recipe_snapshot=derived["recipe_snapshot"],
                builder_steps=derived["builder_steps"],
                recipe_values=derived["recipe_values"],
                blockers=derived["blockers"],
                status="draft",
                status_reason=pj.notes,
            )
            s.add(job)
            key_to_id[pj.key] = job.id
            created.append((job, pj.depends_on_keys))
        s.flush()
        for job, deps in created:
            job.depends_on = [key_to_id[d] for d in deps if d in key_to_id]
            log_event(s, job, "planner", "planned", model=result.model, prompt_version=planner.PROMPT_VERSION, blockers=job.blockers)
            transition(s, job, "needs_review", "planner", "planner proposal awaiting review")
        req.status = "needs_review"
        refresh_request_status(s, req)
    return {"jobs": len(out.jobs), "model": result.model}


def _mark_plan_dead(s, task: Task) -> None:
    req = s.get(Request, task.payload["request_id"])
    if req and req.status == "parsing":
        req.status = "plan_failed"
        req.status_reason = f"Planning failed after retries: {task.outcome.get('error') or task.last_error[-300:]}"


# --- Baseline -----------------------------------------------------------------------


@handler("capture_baseline", on_dead=lambda s, t: _mark_baseline_dead(s, t))
def handle_baseline(task: Task) -> dict[str, Any]:
    job_id = task.payload["job_id"]
    with session() as s:
        job = s.get(Job, job_id)
        req = s.get(Request, job.request_id)
        if job.revision != task.payload["revision"] or job.status != "needs_review":
            return {"superseded": True}
        if job.baseline_locked:
            return {"skipped": "baseline locked"}
        jd, request_id, masks = job_dict(job), req.id, _masks(req.site_profile_id)
    ctx = llm.CallContext("sam_locate", "sam.v1", request_id=request_id, job_id=job_id, job_revision=jd["revision"])
    obs = capture_job(ctx, jd, request_id=request_id, phase="baseline", masks=masks, task_id=task.id)
    errors = [o for o in obs if o.error]
    if errors:
        raise RuntimeError("baseline capture failed: " + "; ".join(f"{o.where}: {o.error}" for o in errors[:3]))
    problems = []
    is_add = jd["targets"][0].get("menu_action") == "add"
    for o in obs:
        if is_add:
            if o.info is not None or o.ambiguous:
                problems.append(f"{o.where}: a menu item named {jd['targets'][0]['desired_value']!r} already exists.")
        elif o.ambiguous:
            problems.append(f"{o.where}: {o.match_count} visible matches for the reviewed target; identity is ambiguous.")
        elif o.matched_phase != "before":
            problems.append(f"{o.where}: the current value was not found in the reviewed region.")
    image_hashes: dict[str, str] = {}
    if jd["targets"][0]["element_role"] == "image" and not problems:
        from . import imaging

        cache: dict[str, Any] = {}
        for o in obs:
            src = ((o.info or {}).get("img") or {}).get("current_src")
            try:
                image_hashes[f"{o.page_url}|{o.viewport}"] = imaging.fingerprint(checks.fetch_image(src, cache))
            except Exception as e:
                raise RuntimeError(f"could not fetch the original image {src}: {e}") from e
    with session() as s:
        job = s.get(Job, job_id)
        if job.revision != jd["revision"] or job.status != "needs_review":
            return {"superseded": True}
        if not problems and not is_add:
            target = dict(job.targets[0])
            first = obs[0].info or {}
            # Per page and viewport: templates can differ, so one page's DOM path is not another's.
            target["structural_anchor"] = {
                "landmark_role": target["region"],
                "element_role": target["element_role"],
                "baseline_match_count": 1,
                "path": first.get("path"),
                "paths": {f"{o.page_url}|{o.viewport}": (o.info or {}).get("path") for o in obs},
                "baseline_texts": {f"{o.page_url}|{o.viewport}": (o.info or {}).get("text") for o in obs},
                "baseline_fps": image_hashes,
                "context": f"{first.get('tag')} inside {first.get('landmark') or 'body'}",
            }
            job.targets = [target]
        job.baseline_revision = jd["revision"]
        req = s.get(Request, job.request_id)
        if problems:
            transition(s, job, "needs_human", "system", "Target could not be located uniquely before work: " + " ".join(problems))
        elif job.blockers:
            job.status_reason = "Baseline captured; resolve blockers before the job can be ready."
        elif current_approval(s, req) is None:
            job.status_reason = "Baseline captured; coverage approval changed, re-approve to continue."
        else:
            transition(s, job, "ready", "system", "baseline captured and target located at every page and viewport")
        refresh_request_status(s, req)
    return {"evidence": [o.evidence_id for o in obs], "problems": problems}


def _mark_baseline_dead(s, task: Task) -> None:
    job = s.get(Job, task.payload["job_id"])
    if job and job.status == "needs_review" and job.revision == task.payload["revision"]:
        transition(s, job, "needs_human", "system", f"Baseline capture failed (infrastructure): {task.last_error.strip().splitlines()[0] if task.last_error else 'unknown'}. Use Recapture baseline.")
        refresh_request_status(s, s.get(Request, job.request_id))


# --- Verify -------------------------------------------------------------------------


def _load_baseline(s, job: Job) -> list[Evidence]:
    return list(
        s.scalars(
            select(Evidence)
            .where(Evidence.job_id == job.id, Evidence.phase == "baseline", Evidence.job_revision == job.baseline_revision)
            .order_by(Evidence.captured_at)
        )
    )


def _judge_images(job: dict[str, Any], baseline: list[Evidence], after: list[checks.Observation]) -> list[judge.ViewportImages]:
    images = []
    target = job["targets"][0]
    first_page = target["page_urls"][0]
    with session() as s:
        for vp in target.get("viewports") or ["desktop"]:
            b = next((e for e in baseline if e.requested_url == first_page and e.viewport == vp), None)
            a_obs = next((o for o in after if o.page_url == first_page and o.viewport == vp), None)
            a = s.get(Evidence, a_obs.evidence_id) if a_obs else None
            images.append(
                judge.ViewportImages(
                    viewport=vp,
                    page_url=first_page,
                    before_crop=storage.get(b.element_crop) if b and b.element_crop else None,
                    after_crop=storage.get(a.element_crop) if a and a.element_crop else None,
                    after_context=storage.get(a.context_crop) if a and a.context_crop else None,
                    evidence_ids=[x.id for x in (b, a) if x],
                )
            )
    return images


def run_checks(
    ctx: llm.CallContext,
    job: dict[str, Any],
    observations: list[checks.Observation],
    baseline: list[Evidence],
    attributed: list[dict[str, Any]],
    *,
    allow_escalation: bool,
    use_judge: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[str], str, float | None, bool]:
    """Return (check_results, regressions, result, reasons, model, confidence, escalated)."""
    det = checks.evaluate(job, observations)
    regs = checks.regressions(
        {(e.requested_url, e.viewport): e.regions_text for e in baseline},
        {(o.page_url, o.viewport): o.regions_text for o in observations if not o.error},
        attributed,
    )
    model, confidence, escalated = "", None, False
    visual = [c for c in job["acceptance_checks"] if c["kind"] == "visual_layout"]
    # The judge only adds signal on a fresh, uniquely located target; with any required
    # deterministic check unresolved (fail, error, stale cache, ambiguity) it is skipped.
    det_unresolved = any(c.required and c.result != "pass" for c in det if c.kind != "visual_layout")
    if visual and use_judge and not det_unresolved:
        summary = "; ".join(f"{c.check_id}={c.result}" for c in det if c.kind != "visual_layout")
        images = _judge_images(job, baseline, observations)
        if not any(vi.after_crop for vi in images):
            for c in det:
                if c.kind == "visual_layout":
                    c.result, c.explanation = "needs_human", "No after-crop of the target was available for the visual judge."
        else:
            try:
                key = job["targets"][0].get("desired_asset_key")
                reference = storage.get(key) if key else None
                out, model, escalated = judge.judge(ctx, job, visual, images, summary, allow_escalation=allow_escalation, reference_image=reference)
                by_id = {jc.check_id: jc for jc in out.checks}
                for c in det:
                    if c.kind != "visual_layout":
                        continue
                    jc = by_id.get(c.check_id)
                    c.model = model
                    c.evidence_ids = sorted({e for vi in images for e in vi.evidence_ids})
                    if jc is None:
                        c.result, c.explanation = "needs_human", "The visual judge returned no result for this check."
                    else:
                        c.result, c.explanation, c.confidence = jc.result, jc.explanation, jc.confidence
                confs = [jc.confidence for jc in out.checks]
                confidence = min(confs) if confs else None
            except llm.BudgetExceeded as e:
                for c in det:
                    if c.kind == "visual_layout":
                        c.result, c.explanation = "needs_human", f"Spending cap reached; visual check routed to review ({e})."
            except Exception as e:
                for c in det:
                    if c.kind == "visual_layout":
                        c.result, c.explanation = "error", f"Visual judge unavailable: {type(e).__name__}: {e}"
    results = [c.as_dict() for c in det]
    result, reasons = checks.overall(results, regs)
    return results, regs, result, reasons, model, confidence, escalated


def _attributed_values(s, request_id: str) -> list[dict[str, Any]]:
    out = []
    for j in s.scalars(select(Job).where(Job.request_id == request_id)):
        for t in j.targets:
            added = list(t.get("expected_menu_order") or [])
            if t.get("menu_action") == "add":
                added.append(t["desired_value"])
            out.append({"page_urls": t["page_urls"], "region": t["region"], "value": t["current_value"], "added": added})
    return out


@handler("verify_submission", on_dead=lambda s, t: _mark_verify_dead(s, t))
def handle_verify(task: Task) -> dict[str, Any]:
    sub_id = task.payload["submission_id"]
    with session() as s:
        sub = s.get(Submission, sub_id)
        job = s.get(Job, sub.job_id)
        req = s.get(Request, job.request_id)
        if job.current_submission_id != sub.id or job.revision != sub.job_revision:
            return {"superseded": True}
        if job.status == "queued":
            transition(s, job, "verifying", "system", f"verifying submission {sub.id}")
            refresh_request_status(s, req)
        elif job.status != "verifying":
            return {"superseded": True, "status": job.status}
        jd, request_id, masks = job_dict(job), req.id, _masks(req.site_profile_id)
        baseline = _load_baseline(s, job)
        for e in baseline:
            s.expunge(e)
        attributed = _attributed_values(s, request_id)
        allow_escalation = job.escalations < app_settings().max_escalations_per_job
    ctx = llm.CallContext("verify", judge.PROMPT_VERSION, request_id=request_id, job_id=jd["id"], job_revision=jd["revision"], attempt_id=sub.attempt_id)
    if not baseline:
        raise PermanentError("no baseline evidence for this job revision")
    if jd["type"] == "form_check":
        return _verify_form(task, jd, sub, request_id)
    obs = capture_job(
        llm.CallContext(**{**ctx.__dict__, "purpose": "sam_locate"}),
        jd,
        request_id=request_id,
        phase="after",
        masks=masks,
        submission_id=sub_id,
        attempt_id=sub.attempt_id,
        task_id=task.id,
        freshness=True,
    )
    if all(o.error for o in obs):
        raise RuntimeError("every after-capture failed: " + obs[0].error)
    results, regs, result, reasons, model, confidence, escalated = run_checks(
        llm.CallContext(**{**ctx.__dict__, "purpose": "judge"}), jd, obs, baseline, attributed, allow_escalation=allow_escalation
    )
    if result == "verification_error" and task.executions < task.max_executions:
        raise RuntimeError("verification infrastructure error: " + "; ".join(reasons)[:500])
    diagnosis_model = ""
    if result == "fail" and jd["attempts"] + 1 >= 2 and allow_escalation:
        try:
            note, diagnosis_model = judge.diagnose_repeated_failure(llm.CallContext(**{**ctx.__dict__, "purpose": "diagnose_escalation"}), jd, reasons)
            if note:
                reasons.append(f"Escalation review ({diagnosis_model}): {note}")
        except Exception as e:  # escalation is best-effort
            log.warning("diagnosis escalation failed: %s", e)
    committed = commit_verdict(
        jd,
        sub_id,
        sub.attempt_id,
        result=result,
        check_results=results,
        regressions=regs,
        reasons=reasons,
        evidence_ids=[o.evidence_id for o in obs if o.evidence_id] + [e.id for e in baseline],
        model=model,
        confidence=confidence,
        escalated=escalated or bool(diagnosis_model),
    )
    return {"result": result, "committed": committed}


def verdict_summary(result: str, check_results: list[dict[str, Any]], reasons: list[str]) -> str:
    """One builder-readable line; the full per-check detail stays on the verdict."""
    if result == "pass":
        return "All required checks passed."
    head = {"fail": "Change failed", "needs_human": "Needs review", "verification_error": "Verification unavailable"}[result]
    required = [c for c in check_results if c["required"]]
    bad = [c for c in required if c["result"] not in ("pass", "skipped")]
    if not bad:
        return f"{head}: {reasons[0] if reasons else 'see verdict'}"[:600]
    kinds = ", ".join(dict.fromkeys(c["kind"].replace("_", " ") for c in bad))
    return f"{head} ({len(bad)} of {len(required)} required checks: {kinds}). {bad[0]['explanation']}"[:600]


def _verify_form(task: Task, jd: dict[str, Any], sub: Submission, request_id: str) -> dict[str, Any]:
    from . import flowcheck

    with session() as s:
        req = s.get(Request, request_id)
        site_url, profile = req.site_url, profiles.get_profile(req.site_profile_id) or {}
    results, evidence_ids = flowcheck.run(
        thread_capturer(), job=jd, request_id=request_id, site_url=site_url, profile=profile,
        submission_id=sub.id, attempt_id=sub.attempt_id, task_id=task.id,
    )
    result, reasons = checks.overall(results, [])
    if result == "verification_error" and task.executions < task.max_executions:
        raise RuntimeError("form check infrastructure error: " + "; ".join(reasons)[:500])
    committed = commit_verdict(
        jd, sub.id, sub.attempt_id, result=result, check_results=results, regressions=[], reasons=reasons,
        evidence_ids=evidence_ids, model="", confidence=None, escalated=False,
    )
    return {"result": result, "committed": committed}


def commit_verdict(
    jd: dict[str, Any],
    submission_id: str,
    attempt_id: str,
    *,
    result: str,
    check_results: list[dict[str, Any]],
    regressions: list[dict[str, Any]],
    reasons: list[str],
    evidence_ids: list[str],
    model: str,
    confidence: float | None,
    escalated: bool,
) -> bool:
    """Write the verdict; only the current submission on the current revision changes job status."""
    with session() as s:
        job = s.get(Job, jd["id"], with_for_update=True)
        v = Verdict(
            id=new_id("ver"),
            job_id=job.id,
            job_revision=jd["revision"],
            submission_id=submission_id,
            attempt_id=attempt_id,
            evidence_ids=evidence_ids,
            check_results=check_results,
            result=result,
            reasons=reasons,
            regressions=regressions,
            model=model,
            prompt_version=judge.PROMPT_VERSION,
            confidence=confidence,
        )
        if job.revision != jd["revision"] or job.current_submission_id != submission_id or job.status != "verifying":
            v.committed = False
            v.superseded_reason = f"job moved on (revision r{job.revision}, submission {job.current_submission_id or 'none'}, status {job.status})"
            s.add(v)
            return False
        v.committed = True
        s.add(v)
        new_status = {"pass": "passed", "fail": "failed", "needs_human": "needs_human", "verification_error": "verification_error"}[result]
        if result == "fail":
            job.attempts += 1
        if escalated:
            job.escalations += 1
        transition(s, job, new_status, "verifier", verdict_summary(result, check_results, reasons))
        refresh_request_status(s, s.get(Request, job.request_id))
        return True


def _mark_verify_dead(s, task: Task) -> None:
    sub = s.get(Submission, task.payload["submission_id"])
    job = s.get(Job, sub.job_id) if sub else None
    if not job or job.current_submission_id != sub.id or job.status not in ("queued", "verifying"):
        return
    reason = f"Verification unavailable (infrastructure): {(task.last_error or '').strip().splitlines()[0] if task.last_error else 'unknown error'}"
    s.add(
        Verdict(
            id=new_id("ver"),
            job_id=job.id,
            job_revision=job.revision,
            submission_id=sub.id,
            attempt_id=sub.attempt_id,
            result="verification_error",
            reasons=[reason, "This is not evidence that the change is wrong. Use Retry verification."],
            committed=True,
        )
    )
    if job.status == "queued":
        transition(s, job, "verifying", "system", "")
    transition(s, job, "verification_error", "system", reason)
    refresh_request_status(s, s.get(Request, job.request_id))


# --- Final consistency gate --------------------------------------------------------------


def _element_fp(o: checks.Observation) -> tuple:
    info = o.info or {}
    return (o.matched_phase, info.get("text"), normalize_href(info.get("href"), o.final_url or o.page_url))


def _block_run(run_id: str, reasons: list[str], invalidate: dict[str, str] | None = None) -> None:
    with session() as s:
        run = s.get(ClosureRun, run_id)
        req = s.get(Request, run.request_id)
        for job_id, why in (invalidate or {}).items():
            job = s.get(Job, job_id)
            invalidate_verdicts(s, job, why)
            if job.status == "passed":
                transition(s, job, "with_builder", "closure", f"Final consistency gate: {why}")
        run.outcome = "blocked"
        run.reasons = reasons
        run.finished_at = utcnow()
        release_lease(s, req.site_host, run.lease_token)
        req.status_reason = "Final consistency gate blocked closure: " + " ".join(reasons)[:1500]
        refresh_request_status(s, req)


@handler("closure_run", on_dead=lambda s, t: _mark_closure_dead(s, t))
def handle_closure(task: Task) -> dict[str, Any]:
    run_id = task.payload["closure_run_id"]
    cfg = app_settings()
    with session() as s:
        run = s.get(ClosureRun, run_id)
        if run.outcome != "pending":
            return {"skipped": run.outcome}
        req = s.get(Request, run.request_id)
        try:
            renew_lease(s, req.site_host, run.lease_token, allow_expired=True)
        except LeaseLost as e:
            lost = str(e)
        else:
            lost = None
        if lost:
            run.outcome = "blocked"
            run.reasons = [f"{lost}; another final scan took the site. Start a fresh final scan."]
            run.finished_at = utcnow()
            req.status_reason = run.reasons[0]
            refresh_request_status(s, req)
            return {"outcome": "blocked", "lease_lost": True}
        run.outcome = "running"
        run.window_started_at = utcnow()
        run.window_deadline = run.window_started_at + timedelta(seconds=cfg.closure_window_s)
        jobs = [job_dict(s.get(Job, jid)) for jid in run.job_revisions]
        passing = {}
        for jid in run.job_revisions:
            v = current_verdict(s, s.get(Job, jid))
            passing[jid] = [s.get(Evidence, e) for e in (v.evidence_ids if v else [])]
        for evs in passing.values():
            for e in evs:
                if e is not None:
                    s.expunge(e)
        request_id, host, token, masks = req.id, req.site_host, run.lease_token, _masks(req.site_profile_id)
        deadline = run.window_deadline

    ctx = llm.CallContext("closure", "closure.v1", request_id=request_id)
    first: dict[str, list[checks.Observation]] = {}
    all_results: list[dict[str, Any]] = []
    invalid: dict[str, str] = {}
    try:
        for jd in jobs:
            obs = capture_job(ctx, jd, request_id=request_id, phase="final", masks=masks, closure_run_id=run_id, task_id=task.id, freshness=True, use_sam=False)
            first[jd["id"]] = obs
            # Visual and form-flow checks are not repeated at closure (no second form submission).
            det = [c.as_dict() for c in checks.evaluate(jd, obs) if c.kind != "visual_layout" and c.kind not in checks.FLOW_KINDS]
            all_results += [{**c, "job_id": jd["id"], "pass": 1} for c in det]
            bad = [c for c in det if c["required"] and c["result"] != "pass"]
            if bad:
                invalid[jd["id"]] = "; ".join(f"[{c['check_id']}] {c['explanation']}" for c in bad)
                continue
            verified = {(e.target_id, e.requested_url, e.viewport): e for e in passing[jd["id"]] if e is not None and e.phase == "after"}
            for o in obs:
                e = verified.get(o.key)
                if e is not None and _element_fp(o) != _element_fp(evidence_observation(e)):
                    invalid[jd["id"]] = f"{o.where} changed since it was verified ({_element_fp(evidence_observation(e))[1]!r} -> {_element_fp(o)[1]!r})"
            with session() as s:
                renew_lease(s, host, token)
        if invalid:
            _block_run(run_id, [f"Job {k}: {v}" for k, v in invalid.items()], invalid)
            return {"outcome": "blocked", "invalid": invalid}

        # Second pass: revisit the same targets and compare fingerprints just before commit.
        recheck_started = time.time()
        fingerprints = {}
        for jd in jobs:
            region = jd["targets"][0]["region"]
            obs2 = capture_job(ctx, jd, request_id=request_id, phase="final_recheck", masks=masks, closure_run_id=run_id, task_id=task.id, use_sam=False)
            for o1, o2 in zip(first[jd["id"]], obs2):
                f1, f2 = checks.fingerprint(o1, region), checks.fingerprint(o2, region)
                fingerprints[f"{jd['id']}|{o1.page_url}|{o1.viewport}"] = {"first": f1, "recheck": f2}
                if f1 != f2:
                    invalid[jd["id"]] = f"{o2.where} changed during the final capture window"
            with session() as s:
                renew_lease(s, host, token)
        if invalid:
            _block_run(run_id, [f"Job {k}: {v}" for k, v in invalid.items()], invalid)
            return {"outcome": "blocked", "invalid": invalid}
    except LeaseLost as e:
        _block_run(run_id, [f"Site lease lost: {e}. Start a fresh final scan."])
        return {"outcome": "blocked", "lease_lost": True}

    # Atomic close: everything recorded at the start of the run must still hold.
    with session() as s:
        run = s.get(ClosureRun, run_id)
        req = s.get(Request, run.request_id)
        reasons = []
        now = utcnow()
        if now > deadline:
            reasons.append(f"capture window of {cfg.closure_window_s}s expired")
        if time.time() - recheck_started > cfg.fingerprint_max_age_s:
            reasons.append(f"final fingerprint recheck started more than {cfg.fingerprint_max_age_s}s before commit")
        try:
            renew_lease(s, req.site_host, run.lease_token)
        except LeaseLost as e:
            reasons.append(str(e))
        approval = current_approval(s, req)
        if approval is None or approval.id != run.approval_id or req.scope_revision != run.scope_revision:
            reasons.append("coverage approval or scope changed during the run")
        for jid, rev in run.job_revisions.items():
            job = s.get(Job, jid)
            if job.revision != rev or job.current_submission_id != run.submission_ids.get(jid) or job.status != "passed":
                reasons.append(f"job {jid} changed during the run")
        run.fingerprints = fingerprints
        run.check_results = all_results
        run.finished_at = now
        if reasons:
            run.outcome = "blocked"
            run.reasons = reasons
            release_lease(s, req.site_host, run.lease_token)
            req.status_reason = "Final consistency gate blocked closure: " + "; ".join(reasons)
            refresh_request_status(s, req)
            return {"outcome": "blocked", "reasons": reasons}
        for jid in run.job_revisions:
            s.add(
                Verdict(
                    id=new_id("ver"),
                    job_id=jid,
                    job_revision=run.job_revisions[jid],
                    submission_id=run.submission_ids[jid],
                    closure_run_id=run.id,
                    evidence_ids=[o.evidence_id for o in first[jid]],
                    check_results=[c for c in all_results if c["job_id"] == jid],
                    result="pass",
                    reasons=["final consistency gate"],
                    model="",
                    prompt_version="closure.v1",
                    committed=True,
                )
            )
        run.outcome = "closed"
        release_lease(s, req.site_host, run.lease_token)
        req.closed_at = now
        req.status_reason = f"Closed after the final consistency gate (run {run.id})."
        refresh_request_status(s, req)
    draft_done_message(request_id, run_id)
    return {"outcome": "closed"}


def _mark_closure_dead(s, task: Task) -> None:
    run = s.get(ClosureRun, task.payload["closure_run_id"])
    if run and run.outcome in ("pending", "running"):
        req = s.get(Request, run.request_id)
        run.outcome = "error"
        run.reasons = [f"Final verification unavailable (infrastructure): {(task.last_error or '').strip()[:300]}"]
        run.finished_at = utcnow()
        release_lease(s, req.site_host, run.lease_token)
        req.status_reason = run.reasons[0] + " Start the final gate again."
        refresh_request_status(s, req)


DONE_SYSTEM = """You draft a short, friendly 'your change is done' note from a Website Care team to a small-business customer.
Plain language, no jargon, under 140 words. Say exactly what changed and where. Mention that screenshots are attached.
Include the verification time window you are given and do not promise that the site cannot change later. Never invent details."""


def draft_done_message(request_id: str, run_id: str) -> None:
    with session() as s:
        req = s.get(Request, request_id)
        run = s.get(ClosureRun, run_id)
        jobs = list(s.scalars(select(Job).where(Job.request_id == request_id)))
        lines = []
        for j in jobs:
            t = j.targets[0]
            pages = ", ".join(t["page_urls"])
            lines.append(f"- {t['target_description']}: {t['current_value']} -> {t['desired_value']} on {pages} ({' and '.join(t['viewports'])})")
        window = f"{run.window_started_at:%Y-%m-%d %H:%M} to {run.finished_at:%H:%M} UTC"
        customer, site = req.customer or "there", req.site_url
    facts = f"Customer: {customer}\nSite: {site}\nVerified between: {window}\nChanges verified:\n" + "\n".join(lines)
    try:
        msg, _ = llm.structured(
            llm.CallContext("done_message", "done.v1", request_id=request_id),
            app_settings().planner_model,
            DONE_SYSTEM,
            facts,
            DoneMessage,
            tool_name="submit_message",
            tool_description="Submit the drafted customer message.",
            max_tokens=3000,
        )
        text = f"Subject: {msg.subject}\n\n{msg.body}"
    except Exception as e:
        log.warning("done message drafting failed, using template: %s", e)
        text = (
            f"Subject: Your website update is done\n\nHi {customer},\n\nWe've made the changes you asked for on {site}:\n"
            + "\n".join(lines)
            + f"\n\nWe checked them on desktop and mobile between {window}; before/after screenshots are attached.\n\nThe Website Care team"
        )
    with session() as s:
        req = s.get(Request, request_id)
        if req.closed_at is not None:
            req.done_message = text + f"\n\n[Verification interval {window}; reviewed scope as listed. Drafted for a human to review and send.]"
            req.done_message_valid = True


# --- Voice transcription and image drafts -------------------------------------------------


@handler("transcribe", on_dead=lambda s, t: _mark_transcribe_dead(s, t))
def handle_transcribe(task: Task) -> dict[str, Any]:
    from . import intake
    from .service import AUDIO_TYPES, start_planning_if_transcribed

    request_id = task.payload["request_id"]
    with session() as s:
        req = s.get(Request, request_id)
        if req.status != "transcribing":  # a Care lead already typed it, or planning started
            return {"skipped": req.status}
        atts = [dict(a) for a in req.attachments]
    unavailable = None
    for att in atts:
        if att["media_type"] not in AUDIO_TYPES or att.get("transcript_status") in ("done", "manual"):
            continue
        ctx = llm.CallContext("transcribe", "transcribe.v1", request_id=request_id)
        try:
            att["transcript"] = intake.transcribe(ctx, storage.get(att["key"]), att["filename"], att["media_type"])
            att["transcript_status"] = "done"
        except intake.MediaModelUnavailable as e:
            att["transcript_status"], unavailable = "unavailable", str(e)
    with session() as s:
        req = s.get(Request, request_id, with_for_update=True)
        if req.status != "transcribing":
            return {"skipped": req.status}
        req.attachments = atts
        if unavailable:
            req.status = "awaiting_transcript"
            req.status_reason = f"Voice note could not be transcribed automatically ({unavailable}). A Care lead must listen and type the transcript."
            return {"awaiting_transcript": True}
        start_planning_if_transcribed(s, req)
    return {"transcribed": True}


def _mark_transcribe_dead(s, task: Task) -> None:
    req = s.get(Request, task.payload["request_id"])
    if req and req.status == "transcribing":
        req.status = "awaiting_transcript"
        req.status_reason = "Automatic transcription failed; a Care lead must type the transcript."


@handler("image_draft", on_dead=lambda s, t: _mark_draft_dead(s, t))
def handle_image_draft(task: Task) -> dict[str, Any]:
    from . import intake
    from .records import ImageDraft

    draft_id = task.payload["draft_id"]
    with session() as s:
        d = s.get(ImageDraft, draft_id)
        prompt, request_id, job_id = d.prompt, d.request_id, d.job_id
    ctx = llm.CallContext("image_draft", "image.v1", request_id=request_id, job_id=job_id)
    try:
        data, media_type = intake.generate_image(ctx, redact.for_model(prompt))
    except (intake.MediaModelUnavailable, llm.BudgetExceeded) as e:
        with session() as s:
            d = s.get(ImageDraft, draft_id)
            d.status, d.error = "failed", str(e)
        return {"failed": str(e)}
    ext = {"image/webp": "webp", "image/jpeg": "jpg"}.get(media_type, "png")
    key = storage.put(f"{request_id}/drafts/{draft_id}.{ext}", data)
    with session() as s:
        d = s.get(ImageDraft, draft_id)
        d.key, d.media_type, d.status = key, media_type, "draft"
    return {"key": key}


def _mark_draft_dead(s, task: Task) -> None:
    from .records import ImageDraft

    d = s.get(ImageDraft, task.payload["draft_id"])
    if d and d.status == "pending":
        d.status, d.error = "failed", (task.last_error or "")[-300:]


@handler("retention")
def handle_retention(task: Task) -> dict[str, Any]:
    from . import retention

    return retention.purge()

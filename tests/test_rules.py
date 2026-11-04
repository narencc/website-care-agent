"""Unit tests for the deterministic rules: states, recipes, network policy, checks, model I/O."""

import pytest

from feluda import checks, jobspec, llm, netguard, profiles, sam, states
from feluda.checks import Observation
from feluda.schemas import JudgeOutput


# --- State machine ------------------------------------------------------------------


def test_no_manual_path_to_passed():
    for src in ("needs_human", "failed", "verification_error", "with_builder", "ready"):
        with pytest.raises(states.TransitionError):
            states.check_transition(src, "passed")
    states.check_transition("verifying", "passed")


def test_failed_returns_to_builder_and_error_retries():
    states.check_transition("failed", "with_builder")
    states.check_transition("verification_error", "queued")


def test_request_status_derivation():
    d = states.derive_request_status
    assert d([], parsing=True, closing=False, closed=False) == "parsing"
    assert d(["passed", "passed"], parsing=False, closing=False, closed=False) == "ready_to_close"
    assert d(["passed", "verifying"], parsing=False, closing=False, closed=False) == "verifying"
    assert d(["passed", "needs_human"], parsing=False, closing=False, closed=False) == "needs_human"
    assert d(["passed"], parsing=False, closing=False, closed=True) == "done"


# --- Recipes ------------------------------------------------------------------------


def _demo():
    return profiles.load_profiles()["demo_dental_local"]


def test_fixture_hashes_match():
    for r in _demo()["recipes"]:
        assert r["source_hash"] == profiles.recipe_source_hash(r)


def test_snapshot_substitutes_every_placeholder():
    snap, steps, blockers = profiles.build_snapshot(
        _demo(), "demo.header_phone", "text_update",
        {"current_text": "(555) 201-4410", "new_text": "(555) 201-9900", "new_href": "tel:+15552019900"},
        page_urls=["http://localhost:8100/"], viewports=["desktop", "mobile"],
    )
    assert blockers == []
    assert all("{" not in s["instruction"] for s in steps)
    assert "(555) 201-9900" in steps[2]["instruction"]
    assert profiles.steps_match_snapshot(steps, snap)
    assert snap["source_hash"] and snap["validation_date"]


def test_missing_value_blocks_ready():
    _, _, blockers = profiles.build_snapshot(
        _demo(), "demo.header_phone", "text_update", {"current_text": "a", "new_text": "b"}, page_urls=["u"], viewports=["desktop"]
    )
    assert any("new_href" in b for b in blockers)


def test_unvalidated_recipe_blocks_ready():
    p = profiles.load_profiles()["example_dental_blocktheme"]
    _, _, blockers = profiles.build_snapshot(
        p, "demo_site.header_phone", "text_update", {"new_text": "x", "new_href": "tel:+1"}, page_urls=["u"], viewports=["desktop"]
    )
    assert any("requires_site_validation" in b for b in blockers)
    assert any("validation date" in b for b in blockers)
    assert any("source hash" in b for b in blockers)


def test_tampered_recipe_blocks_ready():
    p = {**_demo(), "recipes": [dict(_demo()["recipes"][0])]}
    p["recipes"][0]["steps"] = p["recipes"][0]["steps"] + ["Also delete the footer."]
    _, _, blockers = profiles.build_snapshot(
        p, "demo.header_phone", "text_update", {"current_text": "a", "new_text": "b", "new_href": "tel:+1"}, page_urls=["u"], viewports=["desktop"]
    )
    assert any("hash mismatch" in b for b in blockers)


def test_no_profile_means_human_review():
    _, steps, blockers = profiles.build_snapshot(None, None, "text_update", {}, page_urls=[], viewports=[])
    assert steps == [] and "needs human review" in blockers[0]


def test_derive_job_checks_and_grounding():
    target = {
        "id": "header_phone", "page_urls": ["http://x/"], "target_description": "header phone", "region": "banner",
        "element_role": "link", "current_value": "(555) 201-4410", "desired_value": "(555) 201-9900",
        "current_href": "tel:+15552014410", "desired_href": "tel:+15552019900", "viewports": ["desktop", "mobile"],
    }
    d = jobspec.derive_job(job_type="text_update", target=target, recipe_id="demo.header_phone", profile=_demo(),
                           page_outlines={"http://x/": {"banner": "Home | (555) 201-4410"}})
    kinds = [c["kind"] for c in d["acceptance_checks"]]
    assert kinds == ["visible_text", "link_destination", "interactable", "old_value_absent", "visual_layout"]
    assert d["blockers"] == []
    phases = {c["phase"]: c for c in d["targets"][0]["locator_candidates"]}
    assert phases["before"]["name"] == "(555) 201-4410" and phases["after"]["within_role"] == "banner"

    ungrounded = jobspec.derive_job(job_type="text_update", target=target, recipe_id="demo.header_phone", profile=_demo(),
                                    page_outlines={"http://x/": {"contentinfo": "(555) 201-4410"}})
    assert any("ungrounded" in b for b in ungrounded["blockers"])
    wrong_region = jobspec.derive_job(job_type="text_update", target=target, recipe_id="demo.footer_phone", profile=_demo())
    assert any("region" in b for b in wrong_region["blockers"])
    unsupported = jobspec.derive_job(job_type="other", target=target, recipe_id="demo.header_phone", profile=_demo())
    assert any("not supported" in b for b in unsupported["blockers"])


def test_normalize_href():
    n = jobspec.normalize_href
    assert n("tel:(555) 201-9900") == "tel:5552019900"
    assert n("tel:+1 555 201 9900") == "tel:+15552019900"
    assert n("MAILTO:Hello@Example.com?subject=x") == "mailto:hello@example.com"
    assert n("/contact", "http://Site.test/a/") == "http://site.test/contact/"
    assert n("https://site.test/contact/#form") == "https://site.test/contact/"


# --- Network policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://localhost/", "http://127.0.0.1:8100/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data", "http://[::1]/",
     "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/", "http://metadata.google.internal/", "http://100.64.1.1/"],
)
def test_blocked_urls(app_env, url):
    with pytest.raises(netguard.BlockedURL):
        netguard.check_url(url, resolve=False)


def test_approved_test_origin_allowed(app_env):
    from dataclasses import replace

    from feluda import settings

    settings.use_settings(replace(app_env, approved_test_origins=("http://localhost:8100",)))
    assert netguard.check_url("http://localhost:8100/contact/")
    with pytest.raises(netguard.BlockedURL):
        netguard.check_url("http://localhost:8101/")


def test_public_ip_literal_allowed(app_env):
    assert netguard.check_url("https://93.184.216.34/", resolve=False)


# --- Checks -----------------------------------------------------------------------------


def test_contains_value_respects_boundaries():
    assert checks.contains_value("Call (555) 201-9900 now", "(555) 201-9900")
    assert not checks.contains_value("Call (555) 201-99001", "(555) 201-9900")
    assert not checks.contains_value("(555) 201-990", "(555) 201-9900")


JOB = {
    "targets": [{"id": "t", "region": "banner", "current_value": "OLD-1", "desired_value": "NEW-2", "element_role": "link"}],
    "acceptance_checks": [
        {"id": "t.visible_text", "kind": "visible_text", "target_id": "t", "expected": "NEW-2", "required": True},
        {"id": "t.link_destination", "kind": "link_destination", "target_id": "t", "expected": "tel:+12", "required": True},
        {"id": "t.old_value_absent", "kind": "old_value_absent", "target_id": "t", "expected": "OLD-1", "region": "banner", "required": True},
        {"id": "t.visual_layout", "kind": "visual_layout", "target_id": "t", "expected": "ok", "required": True},
    ],
}


def obs(phase="after", text="NEW-2", href="tel:+12", banner="Home NEW-2", http=None, ambiguous=False, error=None, vp="desktop"):
    return Observation(
        target_id="t", page_url="http://x/", viewport=vp, final_url="http://x/", error=error, matched_phase=phase,
        match_count=2 if ambiguous else 1, ambiguous=ambiguous,
        info=None if (ambiguous or phase is None) else {"text": text, "href": href, "visible": True},
        interactable={"ok": True}, regions_text={"banner": banner, "page": banner}, http=http or {},
    )


def by_id(results):
    return {r.check_id: r.result for r in results}


def test_correct_change_passes_deterministic_checks():
    r = by_id(checks.evaluate(JOB, [obs(), obs(vp="mobile")]))
    assert r == {"t.visible_text": "pass", "t.link_destination": "pass", "t.old_value_absent": "pass", "t.visual_layout": "skipped"}


def test_old_value_fails_and_mobile_only_failure_fails():
    r = by_id(checks.evaluate(JOB, [obs(), obs(phase="before", text="OLD-1", banner="Home OLD-1", vp="mobile")]))
    assert r["t.visible_text"] == "fail" and r["t.old_value_absent"] == "fail"


def test_cached_old_value_is_needs_human_not_fail():
    stale = obs(phase="before", text="OLD-1", href="tel:+11", banner="OLD-1", http={"headers": {"x-cache": "HIT"}})
    r = by_id(checks.evaluate(JOB, [stale]))
    assert r["t.visible_text"] == r["t.link_destination"] == r["t.old_value_absent"] == "needs_human"
    mk = lambda c: {"check_id": c.check_id, "result": c.result, "required": c.required, "explanation": c.explanation}  # noqa: E731
    assert checks.overall([mk(c) for c in checks.evaluate(JOB, [stale]) if c.kind != "visual_layout"], [])[0] == "needs_human"


def test_typo_and_wrong_link_fail():
    r = by_id(checks.evaluate(JOB, [obs(phase="structural", text="NEW-", href="tel:+13")]))
    assert r["t.visible_text"] == "fail" and r["t.link_destination"] == "fail"


def test_ambiguous_target_needs_human():
    assert by_id(checks.evaluate(JOB, [obs(ambiguous=True)]))["t.visible_text"] == "needs_human"


def test_overall_precedence():
    mk = lambda r: {"check_id": r, "result": r, "required": True, "explanation": r}  # noqa: E731
    assert checks.overall([mk("pass"), mk("fail"), mk("error")], [])[0] == "fail"
    assert checks.overall([mk("pass"), mk("error")], [])[0] == "verification_error"
    assert checks.overall([mk("pass"), mk("needs_human")], [])[0] == "needs_human"
    assert checks.overall([mk("pass")], [{"region": "main", "page_url": "u", "viewport": "d", "removed": [], "added": ["x"]}])[0] == "needs_human"
    assert checks.overall([mk("pass")], [])[0] == "pass"


def test_regressions_attribute_request_targets():
    base = {("u", "desktop"): {"banner": "Home\nOLD-1", "contentinfo": "Phone OLD-1\nHours 9-5"}}
    after_ok = {("u", "desktop"): {"banner": "Home\nNEW-2", "contentinfo": "Phone OLD-1\nHours 9-5"}}
    after_bad = {("u", "desktop"): {"banner": "Home\nNEW-2", "contentinfo": "Phone OLD-1\nHours 10-4"}}
    targets = [{"page_urls": ["u"], "region": "banner", "value": "OLD-1"}, {"page_urls": ["u"], "region": "contentinfo", "value": "OLD-1"}]
    assert checks.regressions(base, after_ok, targets) == []
    regs = checks.regressions(base, after_bad, targets)
    assert regs and regs[0]["region"] == "contentinfo" and regs[0]["added"] == ["Hours 10-4"]


# --- Model I/O --------------------------------------------------------------------------


def test_structured_tool_call_and_repair(app_env, scripted):
    replies = iter([{"checks": [{"check_id": "a", "result": "maybe"}]}, {"checks": [{"check_id": "a", "result": "pass", "explanation": "ok", "confidence": 0.9}]}])
    g = scripted({"j": lambda **_: next(replies)})
    out, model = llm.structured(llm.CallContext("t", "v1"), "muse-spark-1.3", "sys", "hi", JudgeOutput, tool_name="j", tool_description="d")
    assert out.checks[0].result == "pass" and len(g.calls) == 2


def test_structured_json_text_fallback(app_env, scripted):
    scripted({"j": lambda model, **_: llm.ModelReply(model=model, text='Here:\n```json\n{"checks": []}\n```')})
    out, _ = llm.structured(llm.CallContext("t", "v1"), "muse-spark-1.3", "sys", "hi", JudgeOutput, tool_name="j", tool_description="d")
    assert out.checks == []


def test_contributor_tier_refused(app_env, scripted):
    scripted({"_text": lambda model, **_: llm.ModelReply(model=model, text="x")})
    with pytest.raises(ValueError, match="Contributor"):
        llm.call(llm.CallContext("t", "v1"), "muse-spark-1.3-contributor", "s", "hi")


def test_budget_cap(app_env, scripted):
    from dataclasses import replace

    from feluda import settings

    settings.use_settings(replace(app_env, request_cost_cap_usd=0.0001))
    scripted({"_text": lambda model, **_: llm.ModelReply(model=model, text="x", input_tokens=100000, output_tokens=0)})
    ctx = llm.CallContext("t", "v1", request_id="req_1")
    llm.call(ctx, "muse-spark-1.3", "s", "hi")
    with pytest.raises(llm.BudgetExceeded):
        llm.call(ctx, "muse-spark-1.3", "s", "hi")


def test_sam_box_parsing():
    text = "<0f>0<|box;x1=10;y1=10;x2=150;y2=40;w=400;h=120|><|mask;x=0;y=0;data=abc|>"
    (b,) = sam.parse_boxes(text)
    assert (b.x1, b.y2, b.image_w) == (10, 40, 400)
    assert sam.iou(b.as_dict(), {"x1": 10, "y1": 10, "x2": 150, "y2": 40}) == 1.0


# --- QA regressions -------------------------------------------------------------------


SHORTEN = {
    "targets": [{"id": "b", "region": "main", "current_value": "Book now", "desired_value": "Book", "element_role": "button",
                 "structural_anchor": {"baseline_texts": {"http://x/|desktop": "Book now"}}}],
    "acceptance_checks": [
        {"id": "b.visible_text", "kind": "visible_text", "target_id": "b", "expected": "Book", "required": True},
        {"id": "b.old_value_absent", "kind": "old_value_absent", "target_id": "b", "expected": "Book now", "region": "main", "required": True},
    ],
}


def btn(text, main):
    return Observation(target_id="b", page_url="http://x/", viewport="desktop", final_url="http://x/", error=None, matched_phase="after",
                       match_count=1, ambiguous=False, info={"text": text, "visible": True}, interactable={"ok": True},
                       regions_text={"main": main, "page": main}, http={})


def test_shortened_label_unchanged_site_fails():
    r = by_id(checks.evaluate(SHORTEN, [btn("Book now", "Welcome\nBook now")]))
    assert r == {"b.visible_text": "fail", "b.old_value_absent": "fail"}


def test_shortened_label_done_passes():
    assert set(by_id(checks.evaluate(SHORTEN, [btn("Book", "Welcome\nBook")])).values()) == {"pass"}


def test_link_without_baseline_text_needs_exact_text():
    job = {**SHORTEN, "targets": [{**SHORTEN["targets"][0], "structural_anchor": {}}]}
    assert by_id(checks.evaluate(job, [btn("Book now", "Book now")]))["b.visible_text"] == "fail"


def test_paragraph_must_match_baseline_with_only_value_replaced():
    job = {
        "targets": [{"id": "p", "region": "main", "current_value": "201-4410", "desired_value": "201-9900", "element_role": "text",
                     "structural_anchor": {"baseline_texts": {"http://x/|desktop": "Call 201-4410 today"}}}],
        "acceptance_checks": [{"id": "p.visible_text", "kind": "visible_text", "target_id": "p", "expected": "201-9900", "required": True}],
    }
    ok = Observation("p", "http://x/", "desktop", "http://x/", None, "after", 1, False, {"text": "Call 201-9900 today"}, {}, {}, {})
    mangled = Observation("p", "http://x/", "desktop", "http://x/", None, "after", 1, False, {"text": "Call 201-9900"}, {}, {}, {})
    assert by_id(checks.evaluate(job, [ok]))["p.visible_text"] == "pass"
    assert by_id(checks.evaluate(job, [mangled]))["p.visible_text"] == "fail"


def test_same_value_outside_declared_target_is_a_regression():
    base = {("u", "desktop"): {"main": "Call OLD-1 to book"}}
    after = {("u", "desktop"): {"main": "Call NEW-2 to book"}}
    header_only = [{"page_urls": ["u"], "region": "banner", "value": "OLD-1"}]
    assert checks.regressions(base, after, header_only)
    assert not checks.regressions(base, after, [{"page_urls": ["u"], "region": "main", "value": "OLD-1"}])


def test_deletion_is_blocked():
    target = {"id": "t", "page_urls": ["http://x/"], "target_description": "fax", "region": "contentinfo", "element_role": "text",
              "current_value": "Fax 1", "desired_value": "", "viewports": ["desktop"]}
    d = jobspec.derive_job(job_type="text_update", target=target, recipe_id=None, profile=None)
    assert any("Removing content" in b for b in d["blockers"])


def test_web_guards(app_env):
    from fastapi.testclient import TestClient

    from feluda import web

    assert web.safe_next("//evil.example") == "/" and web.safe_next(r"/\evil") == "/" and web.safe_next("/jobs/1") == "/jobs/1"
    from feluda import users

    users.ensure_user("qa.lead", "care_lead", password="correct horse battery")
    with TestClient(web.app) as c:
        r = c.post("/requests", data={"site_url": "https://example.com/", "raw_text": "x"}, follow_redirects=False)
        assert r.status_code == 401  # anonymous is never a Care lead
        assert c.get("/", follow_redirects=False).headers["location"].startswith("/login")
        bad = c.post("/login", data={"username": "qa.lead", "password": "wrong", "next": "/"}, follow_redirects=False)
        assert "Wrong" in bad.headers["location"] or "err=" in bad.headers["location"]
        r = c.post("/login", data={"username": "qa.lead", "password": "correct horse battery", "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/" and "feluda_session" in r.cookies
        r = c.post("/requests", data={"site_url": "https://example.com/", "raw_text": "x"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403  # cross-site POST refused even with a session
        assert c.get("/").status_code == 200

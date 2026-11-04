"""Deterministic acceptance checks, freshness classification and regression scan.

Observations are per (target, page, viewport). Each check aggregates them
with precedence fail > error > needs_human > pass: a definite wrong change is
reported to the builder even if another viewport could not be captured.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import netguard
from .jobspec import normalize_href

PRECEDENCE = {"fail": 4, "error": 3, "needs_human": 2, "skipped": 1, "pass": 0}


@dataclass
class Observation:
    """What one capture found for one target (shape shared by live captures and stored Evidence)."""

    target_id: str
    page_url: str
    viewport: str
    final_url: str
    error: str | None
    matched_phase: str | None
    match_count: int
    ambiguous: bool
    info: dict[str, Any] | None
    interactable: dict[str, Any] | None
    regions_text: dict[str, str]
    http: dict[str, Any]
    evidence_id: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.target_id, self.page_url, self.viewport)

    @property
    def where(self) -> str:
        return f"{self.page_url} ({self.viewport})"


@dataclass
class CheckResult:
    check_id: str
    kind: str
    target_id: str
    required: bool
    result: str = "pass"
    explanation: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    model: str | None = None
    confidence: float | None = None

    def add(self, obs: Observation, result: str, why: str) -> None:
        self.observations.append({"page_url": obs.page_url, "viewport": obs.viewport, "result": result, "why": why, "evidence_id": obs.evidence_id})
        if obs.evidence_id:
            self.evidence_ids.append(obs.evidence_id)

    def finalize(self) -> "CheckResult":
        if self.observations:
            worst = max(self.observations, key=lambda o: PRECEDENCE[o["result"]])
            self.result = worst["result"]
            if self.result == "pass":
                self.explanation = f"Passed on {len(self.observations)} page/viewport capture(s)."
            else:
                bad = [o for o in self.observations if o["result"] == self.result]
                groups: dict[str, list[dict[str, Any]]] = {}
                for o in bad:
                    groups.setdefault(o["why"], []).append(o)
                parts = []
                for why, obs in groups.items():
                    if len(obs) == len(self.observations) and len(obs) > 1:
                        parts.append(f"{why[0].upper()}{why[1:]} — on all {len(obs)} page/viewport captures.")
                    else:
                        where = ", ".join(f"{urlsplit(o['page_url']).path or '/'} ({o['viewport']})" for o in obs)
                        parts.append(f"{why[0].upper()}{why[1:]} — on {where}.")
                self.explanation = " ".join(parts)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "required": self.required,
            "result": self.result,
            "explanation": self.explanation,
            "observations": self.observations,
            "evidence_ids": sorted(set(self.evidence_ids)),
            "model": self.model,
            "confidence": self.confidence,
        }


def contains_value(text: str, value: str) -> bool:
    """True if ``value`` occurs in ``text`` not glued to other letters/digits."""
    if not value:
        return False
    pattern = r"(?<![0-9A-Za-z])" + re.escape(value) + r"(?![0-9A-Za-z])"
    return re.search(pattern, re.sub(r"\s+", " ", text)) is not None


def norm_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def expected_element_text(target: dict[str, Any], obs: "Observation") -> str | None:
    """The element's full text after the change: its baseline text with only the old value replaced.

    Comparing against this (not a substring) catches a change that left extra
    text behind, e.g. "Book now" -> "Book" where nothing was edited.
    """
    anchor = target.get("structural_anchor") or {}
    baseline = (anchor.get("baseline_texts") or {}).get(f"{obs.page_url}|{obs.viewport}")
    if not baseline or target["current_value"] not in baseline:
        return None
    return baseline.replace(target["current_value"], target["desired_value"])


FLOW_KINDS = {"form_submission", "inbox_receipt"}


def fetch_image(url: str, cache: dict[str, Any]) -> bytes:
    if url not in cache:
        try:
            resp = netguard.guarded_get(url)
            resp.raise_for_status()
            cache[url] = resp.content
        except Exception as e:  # recorded per observation as an error
            cache[url] = e
    value = cache[url]
    if isinstance(value, Exception):
        raise value
    return value


def _image_asset(obs: "Observation", target: dict[str, Any], cache: dict[str, Any]) -> tuple[str, str]:
    """Approved visual match: the rendered image's dHash is near the approved asset's
    and not the original's. File names are not trusted (WordPress renames uploads)."""
    from . import imaging

    img = (obs.info or {}).get("img")
    if not img or not img.get("current_src"):
        return "needs_human", "the target is not an image element"
    if obs.matched_phase == "before":
        return "fail", f"still shows the original image file {img['current_src'].rsplit('/', 1)[-1]!r}"
    approved = target.get("desired_asset_fp")
    if not approved:
        return "needs_human", "no approved replacement image to compare against"
    try:
        shown = imaging.fingerprint(fetch_image(img["current_src"], cache))
    except Exception as e:
        return "error", f"could not fetch the rendered image {img['current_src']}: {e}"
    original = ((target.get("structural_anchor") or {}).get("baseline_fps") or {}).get(f"{obs.page_url}|{obs.viewport}")
    if original is not None and imaging.same_as_old(shown, original):
        return "fail", "still shows the original image (visually identical to the before-capture)"
    d, colour = imaging.compare(shown, approved)
    if imaging.matches(shown, approved):
        return "pass", f"shows the approved image (structure distance {d}/64, colour difference {colour})"
    return "fail", (
        f"shows a different image than the approved one (structure distance {d}/64, limit {imaging.MATCH_MAX_DISTANCE}; "
        f"colour difference {colour}, limit {imaging.MATCH_MAX_COLOUR})"
    )


def _image_rendered(obs: "Observation") -> tuple[str, str]:
    img = (obs.info or {}).get("img")
    if not img:
        return "needs_human", "the target is not an image element"
    if not img.get("complete") or not img.get("natural_w"):
        return "fail", "image is broken (did not load)"
    if not (obs.info or {}).get("visible") or img["rendered_w"] < 2 or img["rendered_h"] < 2:
        return "fail", "image is not visible"
    natural, rendered = img["natural_w"] / img["natural_h"], img["rendered_w"] / img["rendered_h"]
    if abs(rendered - natural) / natural > 0.05:
        return "fail", f"image is distorted (shown at {img['rendered_w']:.0f}x{img['rendered_h']:.0f}, file is {img['natural_w']}x{img['natural_h']})"
    if img["rendered_w"] > img.get("viewport_w", 10**6) + 1:
        return "fail", f"image overflows the {img['viewport_w']}px-wide viewport"
    return "pass", f"loads and renders at {img['rendered_w']:.0f}x{img['rendered_h']:.0f} without distortion"


def cache_hit(http: dict[str, Any]) -> bool:
    h = {k.lower(): str(v).lower() for k, v in (http.get("headers") or {}).items()}
    if "hit" in h.get("x-cache", "") or "hit" in h.get("cf-cache-status", "") or "hit" in h.get("x-proxy-cache", ""):
        return True
    try:
        return int(h.get("age", "0")) > 0
    except ValueError:
        return False


def needs_fresh_capture(obs: Observation, target: dict[str, Any]) -> bool:
    """The page still shows the old value: worth a bounded, cache-busted retry."""
    return obs.error is None and obs.matched_phase == "before" and target["current_value"] != target["desired_value"]


def _element_problem(obs: Observation) -> tuple[str, str] | None:
    if obs.error:
        return "error", f"capture failed: {obs.error}"
    if obs.ambiguous:
        return "needs_human", f"{obs.match_count} visible elements match inside the reviewed region; identity is ambiguous"
    if obs.info is None:
        return "needs_human", "the reviewed element could not be found in its region (missing, moved or restructured)"
    return None


def _link_matches(actual: str | None, expected: str, page_url: str) -> tuple[bool, str]:
    a = normalize_href(actual, page_url)
    e = normalize_href(expected, page_url)
    if a is None:
        return False, "element has no link destination"
    if a == e:
        return True, f"links to {a}"
    if a.startswith("http") and e and e.startswith("http"):
        try:
            final_a = normalize_href(str(netguard.guarded_get(a).url))
            final_e = normalize_href(str(netguard.guarded_get(e).url))
        except Exception as ex:  # destination unreachable: not proof either way
            return False, f"links to {a}, expected {e}; redirect check failed ({ex})"
        if final_a == final_e:
            return True, f"links to {a}, which redirects to the intended {final_e}"
        return False, f"links to {a} (resolves to {final_a}), expected {e} (resolves to {final_e})"
    return False, f"links to {a}, expected {e}"


def evaluate(job: dict[str, Any], observations: list[Observation]) -> list[CheckResult]:
    """Run the job's deterministic checks; visual checks are returned as placeholders for the judge."""
    targets = {t["id"]: t for t in job["targets"]}
    results: list[CheckResult] = []
    image_cache: dict[str, bytes | Exception] = {}
    for check in job["acceptance_checks"]:
        cr = CheckResult(check["id"], check["kind"], check["target_id"], check.get("required", True))
        target = targets[check["target_id"]]
        obs_for = [o for o in observations if o.target_id == target["id"]]
        if check["kind"] == "visual_layout" or check["kind"] in FLOW_KINDS:
            cr.result = "skipped"  # judged by the visual judge / the form flow check
            results.append(cr)
            continue
        for obs in obs_for:
            if obs.error is None and obs.matched_phase == "before" and cache_hit(obs.http):
                # A cached page showing the old value says nothing about the builder's change.
                cr.add(obs, "needs_human", f"page was served from a cache and still shows the old value {target['current_value']!r}; freshness unresolved")
                continue
            if check["kind"] == "menu_order":
                if obs.error:
                    cr.add(obs, "error", f"capture failed: {obs.error}")
                    continue
                labels = [x for x in (obs.regions_text.get("navigation_links") or "").splitlines() if x]
                expected = list(check["expected"])
                if labels == expected:
                    cr.add(obs, "pass", "menu order is " + " | ".join(labels))
                else:
                    cr.add(obs, "fail", f"menu reads {' | '.join(labels) or '(no items)'}; expected {' | '.join(expected)}")
                continue
            if check["kind"] == "old_value_absent":
                if obs.error:
                    cr.add(obs, "error", f"capture failed: {obs.error}")
                    continue
                region = check.get("region") or "page"
                text = obs.regions_text.get(region if region != "page" else "page")
                if text is None:
                    cr.add(obs, "needs_human", f"region {region} not found on the page")
                    continue
                # The old value may be part of the new one ("Book" -> "Book now"); only then
                # mask the new value before searching. The reverse ("Book now" -> "Book")
                # must not erase the old text.
                cleaned = text.replace(target["desired_value"], " ") if target["current_value"] in target["desired_value"] else text
                if contains_value(cleaned, check["expected"]):
                    cr.add(obs, "fail", f"old value {check['expected']!r} is still shown in the {region} region")
                else:
                    cr.add(obs, "pass", "old value absent")
                continue

            problem = _element_problem(obs)
            if problem:
                cr.add(obs, *problem)
                continue
            text = obs.info.get("text") or obs.info.get("aria_label") or ""
            if check["kind"] == "visible_text":
                full = expected_element_text(target, obs)
                if full is not None:
                    shows_expected = norm_text(text) == norm_text(full)
                elif target.get("element_role") in ("link", "button", "heading"):
                    shows_expected = norm_text(text) == norm_text(check["expected"])
                else:
                    shows_expected = contains_value(text, check["expected"])
                if obs.matched_phase in ("after", "structural") and shows_expected:
                    cr.add(obs, "pass", f"shows {check['expected']!r}")
                elif obs.matched_phase == "before":
                    if cache_hit(obs.http):
                        cr.add(obs, "needs_human", f"still shows the old value {target['current_value']!r} and the response came from a cache; freshness unresolved")
                    else:
                        cr.add(obs, "fail", f"still shows the old value {target['current_value']!r}; expected {check['expected']!r}")
                else:
                    cr.add(obs, "fail", f"shows {text!r}; expected {check['expected']!r}")
            elif check["kind"] == "link_destination":
                ok, why = _link_matches(obs.info.get("href"), check["expected"], obs.final_url or obs.page_url)
                cr.add(obs, "pass" if ok else "fail", why)
            elif check["kind"] == "image_asset":
                cr.add(obs, *_image_asset(obs, target, image_cache))
            elif check["kind"] == "image_rendered":
                cr.add(obs, *_image_rendered(obs))
            elif check["kind"] == "interactable":
                inter = obs.interactable or {}
                if not obs.info.get("visible"):
                    cr.add(obs, "fail", "element is not visible")
                elif inter.get("ok"):
                    cr.add(obs, "pass", "visible and clickable")
                else:
                    cr.add(obs, "fail", f"not clickable: {inter.get('reason', 'unknown')}")
            else:
                cr.add(obs, "needs_human", f"unsupported check kind {check['kind']!r}")
        if not obs_for:
            cr.result, cr.explanation = "error", "no captures for this target"
            results.append(cr)
            continue
        results.append(cr.finalize())
    return results


def regressions(
    baseline: dict[tuple[str, str], dict[str, str]],
    after: dict[tuple[str, str], dict[str, str]],
    attributed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Text changes outside the request's targets, per (page, viewport, region).

    ``baseline``/``after`` map (page_url, viewport) to region texts captured with
    the profile's dynamic masks applied. ``attributed`` lists the request's
    targets as {page_urls, region, value}. A changed line is attributed (not a
    regression) only when its baseline version contains the current value of a
    target declared on that page and region; the owning job's checks judge it.
    The same value elsewhere (e.g. an untargeted paragraph) is still a regression.
    """
    found = []
    for key, before_regions in baseline.items():
        after_regions = after.get(key)
        if after_regions is None:
            continue
        for region in ("banner", "navigation", "main", "contentinfo"):
            b = (before_regions.get(region) or "").splitlines()
            a = (after_regions.get(region) or "").splitlines()
            if region != "navigation":
                # Landmarks nest (the menu usually sits inside the header): judge nested
                # navigation lines once, in their own region.
                b = [x for x in b if x not in set((before_regions.get("navigation") or "").splitlines())]
                a = [x for x in a if x not in set((after_regions.get("navigation") or "").splitlines())]
            if b == a:
                continue
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=b, b=a, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                removed, added = b[i1:i2], a[j1:j2]
                mine = [t for t in attributed if key[0] in t["page_urls"] and t["region"] in (region, "page")]
                old_values = [t["value"] for t in mine if t.get("value")]
                # Lines a job adds or reorders on purpose (new menu item, requested order).
                new_values = [v for t in mine for v in t.get("added", [])]
                if removed and all(any(v in line for v in old_values + new_values) for line in removed):
                    continue
                if not removed and added and all(any(v in line for v in new_values) for line in added):
                    continue
                found.append({"page_url": key[0], "viewport": key[1], "region": region, "removed": removed[:5], "added": added[:5]})
    return found


def fingerprint(obs: Observation, region: str) -> str:
    info = obs.info or {}
    payload = {
        "phase": obs.matched_phase,
        "text": info.get("text"),
        "href": normalize_href(info.get("href"), obs.final_url or obs.page_url),
        "region_text": obs.regions_text.get(region, ""),
        "error": bool(obs.error),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def content_hash(regions_text: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(regions_text, sort_keys=True).encode()).hexdigest()


def overall(check_results: list[dict[str, Any]], regressions_found: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Pass rule: every required check passes and no unexplained material regression."""
    required = [c for c in check_results if c["required"]]
    reasons = [f"[{c['check_id']}] {c['explanation']}" for c in required if c["result"] not in ("pass", "skipped")]
    results = {c["result"] for c in required}
    if "fail" in results:
        return "fail", reasons
    if "error" in results:
        return "verification_error", reasons
    if "needs_human" in results or "skipped" in results:
        return "needs_human", reasons or ["A required visual check was not evaluated."]
    if regressions_found:
        return "needs_human", [
            f"Unexplained change in {r['region']} on {r['page_url']} ({r['viewport']}): removed {r['removed']} added {r['added']}" for r in regressions_found
        ]
    return "pass", []

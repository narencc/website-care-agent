"""Deterministic job derivation: locators, acceptance checks and recipe steps.

Both planner output and human corrections go through ``derive_job`` so the
rules live in one place: one target per job, locator candidates for the old
and new values, the change-type check table from the spec, and builder steps
rendered only from a validated recipe snapshot.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import profiles
from .schemas import SUPPORTED_JOB_TYPES

ROLE_FOR_ELEMENT = {"link": "link", "button": "link", "heading": "heading", "text": None, "image": "img", "form": None}


def normalize_href(href: str | None, base: str | None = None) -> str | None:
    if href is None:
        return None
    href = href.strip()
    lower = href.lower()
    if lower.startswith("tel:"):
        digits = re.sub(r"[^\d+]", "", href[4:])
        return "tel:" + digits
    if lower.startswith("mailto:"):
        return "mailto:" + href[7:].split("?")[0].strip().lower()
    absolute = urljoin(base, href) if base else href
    p = urlsplit(absolute)
    path = p.path or "/"
    if path != "/" and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"  # WordPress canonical trailing slash
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


IMAGE_TYPES = {"image_swap", "logo_change"}


def basename(src: str | None) -> str:
    return urlsplit(src or "").path.rsplit("/", 1)[-1]


def locator_candidates(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase-specific candidates. Images are found by their current file (the new
    file's URL is unknown until the builder uploads it, so the after-image is
    resolved through the baseline's structural position); forms by the profile's
    selector; menu additions only by their new label."""
    within = target["region"] if target["region"] != "page" else None
    role = target["element_role"]
    if role == "image":
        name = basename(target["current_value"]) or target["current_value"]
        return [{"phase": "before", "within_role": within, "css": f'img[src*="{name}"]'}]
    if role == "form":
        sel = target.get("form_selector") or "form"
        return [{"phase": "before", "within_role": within, "css": sel}, {"phase": "after", "within_role": within, "css": sel}]
    aria = ROLE_FOR_ELEMENT.get(role)
    pairs = [("before", target["current_value"]), ("after", target["desired_value"])]
    if target.get("menu_action") == "add":
        pairs = [("after", target["desired_value"])]
    out = []
    for phase, value in pairs:
        cand: dict[str, Any] = {"phase": phase, "within_role": within}
        if aria:
            cand.update(role=aria, name=value)
        else:
            cand.update(text=value)
        out.append(cand)
    return out


def _check(tid: str, kind: str, expected: Any, **extra) -> dict[str, Any]:
    return {"id": f"{tid}.{kind}", "kind": kind, "target_id": tid, "expected": expected, "required": True, **extra}


def acceptance_checks(job_type: str, target: dict[str, Any], require_old_value_absent: bool) -> list[dict[str, Any]]:
    """The spec's change-type table, as structured, stable-ID checks."""
    tid = target["id"]
    if job_type == "form_check":
        recipient = target["desired_value"] if "@" in (target.get("desired_value") or "") else "the designated test inbox"
        return [
            _check(tid, "form_submission", "one correlated test submission shows the success message"),
            _check(tid, "inbox_receipt", f"the matching message arrives at {recipient} within the timeout"),
        ]
    if job_type in IMAGE_TYPES:
        return [
            _check(tid, "image_asset", "the approved image (visual match, not just a file name) replaces the original"),
            _check(tid, "image_rendered", "image loads, keeps its aspect ratio and fits the viewport"),
            _check(tid, "visual_layout", f"{target['target_description']} shows the approved image, undistorted, with the surrounding layout intact at every supported viewport"),
        ]
    checks = [_check(tid, "visible_text", target["desired_value"])]
    expected_href = target.get("desired_href") or (target.get("current_href") if target["element_role"] in ("link", "button") else None)
    if expected_href:
        checks.append(_check(tid, "link_destination", expected_href))
    if target["element_role"] in ("link", "button"):
        expectation = "visible, unobstructed and clickable"
        if target["region"] == "navigation":
            expectation += " (on mobile, after opening the menu toggle)"
        checks.append(_check(tid, "interactable", expectation))
    if require_old_value_absent and target.get("menu_action") != "add" and target["current_value"] != target["desired_value"]:
        region = target["region"] if target["region"] != "page" else "page"
        checks.append(_check(tid, "old_value_absent", target["current_value"], region=region))
    if target.get("expected_menu_order"):
        checks.append(_check(tid, "menu_order", list(target["expected_menu_order"])))
    checks.append(
        _check(
            tid,
            "visual_layout",
            f"{target['target_description']} shows {target['desired_value']!r}, is readable and the surrounding layout is intact at every supported viewport",
        )
    )
    return checks


def recipe_values_for(target: dict[str, Any]) -> dict[str, str]:
    current = basename(target["current_value"]) if target["element_role"] == "image" else target["current_value"]
    values = {"current_text": current, "new_text": target["desired_value"]}
    href = target.get("desired_href") or target.get("current_href")
    if href:
        values["new_href"] = href
    order = target.get("expected_menu_order")
    values["menu_order"] = " | ".join(order) if order else "leave the other items in their current order"
    return values


def derive_job(
    *,
    job_type: str,
    target: dict[str, Any],
    recipe_id: str | None,
    profile: dict[str, Any] | None,
    require_old_value_absent: bool = True,
    page_outlines: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return fields for a Job (targets, checks, snapshot, steps, blockers).

    ``target`` uses stored keys: id, page_urls, target_description, region,
    element_role, current_value, desired_value, current_href, desired_href,
    viewports, and per type: desired_asset_key (images), menu_action and
    expected_menu_order (menus). ``page_outlines`` maps page URL to
    {region: text} for grounding checks.
    """
    from . import secrets_store

    blockers: list[str] = []
    target = dict(target)
    role = target["element_role"]
    is_image, is_form = job_type in IMAGE_TYPES, job_type == "form_check"
    is_menu_add = job_type == "menu_change" and target.get("menu_action") == "add"
    form_cfg = (profile or {}).get("form_tests") or {}
    if is_form:
        target["form_selector"] = form_cfg.get("form_selector")
    target["locator_candidates"] = locator_candidates(target)
    target.setdefault("structural_anchor", {"landmark_role": target["region"], "element_role": role, "baseline_match_count": None, "path": None})
    target["require_old_value_absent"] = require_old_value_absent

    if job_type not in SUPPORTED_JOB_TYPES:
        blockers.append(f"Job type {job_type!r} is not supported for automatic verification; route to a human.")
    if is_image != (role == "image"):
        blockers.append(f"Job type {job_type} needs an image target (got {role}).")
    if is_form != (role == "form"):
        blockers.append(f"Job type {job_type} needs a form target (got {role}).")
    if job_type == "menu_change" and target["region"] != "navigation":
        blockers.append("A menu change must target the navigation region.")
    if not (target.get("desired_value") or "").strip():
        blockers.append("Removing content (empty new value) is not supported for automatic verification; route to a human.")
    if not (target.get("current_value") or "").strip() and not (is_menu_add or is_form):
        blockers.append("Target has no current value to locate it by; route to a human.")
    if not target["page_urls"]:
        blockers.append("Target has no page URLs.")
    if not target.get("viewports"):
        blockers.append("Target has no supported viewports.")
    if (
        not (is_image or is_form)
        and target["current_value"] == target["desired_value"]
        and target.get("desired_href") in (None, target.get("current_href"))
        and not target.get("expected_menu_order")
    ):
        blockers.append("Target's desired value equals its current value; nothing to change.")

    if is_image:
        key = target.get("desired_asset_key")
        if not key:
            blockers.append("No approved replacement image: attach the customer's image or approve a Muse Image draft for this job.")
        else:
            from . import imaging, storage

            try:
                target["desired_asset_fp"] = imaging.fingerprint(storage.get(key))
            except Exception as e:
                blockers.append(f"Approved image {key} cannot be read: {e}")
    if is_form:
        if not form_cfg.get("enabled"):
            blockers.append("Contact-form tests are not approved for this site profile.")
        elif not secrets_store.test_account(form_cfg.get("test_account")):
            blockers.append(f"No test account {form_cfg.get('test_account')!r} in the secrets store; form tests must use an existing test account.")
        elif not form_cfg.get("inbox"):
            blockers.append("No test-inbox connector is configured; delivery cannot be confirmed, so the form test cannot pass.")

    if page_outlines is not None and not is_form:
        for url in target["page_urls"]:
            regions = page_outlines.get(url)
            if regions is None:
                blockers.append(f"Page {url} is not in the crawl manifest.")
                continue
            text = " ".join(regions.values()) if target["region"] == "page" else regions.get(target["region"], "")
            if is_menu_add:
                if target["desired_value"] in text.split(" | "):
                    blockers.append(f"Menu item {target['desired_value']!r} already exists on {url}.")
            elif (basename(target["current_value"]) if is_image else target["current_value"]) not in text:
                blockers.append(
                    f"Current value {target['current_value']!r} was not found in the {target['region']} region of {url}; target is ungrounded."
                )

    checks = acceptance_checks(job_type, target, require_old_value_absent)
    snapshot, steps, recipe_blockers = profiles.build_snapshot(
        profile,
        recipe_id,
        job_type,
        recipe_values_for(target),
        page_urls=target["page_urls"],
        viewports=target.get("viewports") or [],
    )
    blockers += recipe_blockers
    if profile and recipe_id and (recipe := profiles.find_recipe(profile, recipe_id)):
        if recipe.get("region") and recipe["region"] != target["region"]:
            blockers.append(f"Recipe {recipe_id} edits the {recipe['region']} region but the target is in {target['region']}.")
    return {
        "targets": [target],
        "acceptance_checks": checks,
        "recipe_snapshot": snapshot,
        "builder_steps": steps,
        "blockers": blockers,
        "recipe_values": recipe_values_for(target),
    }


def target_from_plan(pt, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    target = {
        "id": pt.key,
        "page_urls": list(pt.page_urls),
        "target_description": pt.target_description,
        "region": pt.region,
        "element_role": pt.element_role,
        "current_value": pt.current_text,
        "desired_value": pt.desired_text,
        "current_href": pt.current_href,
        "desired_href": pt.desired_href,
        "viewports": list(pt.viewports),
    }
    if pt.element_role == "image":
        target["current_value"] = re.sub(r"^\s*src=", "", target["current_value"])  # copied from the outline's "src=" label
    if pt.element_role == "image" and pt.desired_asset_attachment:
        images = [a for a in attachments or [] if a.get("media_type", "").startswith("image/")]
        if 1 <= pt.desired_asset_attachment <= len(images):
            target["desired_asset_key"] = images[pt.desired_asset_attachment - 1]["key"]
    if pt.menu_action == "add":
        target["menu_action"] = "add"
    if pt.expected_menu_order:
        target["expected_menu_order"] = list(pt.expected_menu_order)
    return target

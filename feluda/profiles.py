"""Site profiles and versioned editing recipes maintained by the Care team.

Builder steps are never free-form model output: the planner picks a recipe and
supplies values, and the application renders the recipe's validated steps with
explicit substitution. A job is blocked from ``ready`` when its recipe is
unvalidated, lacks a validation date or source hash, or leaves a placeholder
unresolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .netguard import origin_of

PROFILE_DIR = Path(__file__).parent / "fixtures" / "site_profiles"
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# Values the application fills itself; the planner never supplies them.
APP_VALUES = {"admin_url", "page_urls", "viewports"}


def recipe_source_hash(recipe: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"recipe_id": recipe["recipe_id"], "version": recipe["version"], "steps": recipe["steps"]},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, dict[str, Any]]:
    profiles = {}
    for path in sorted(PROFILE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles[data["id"]] = data
    return profiles


def profile_for_url(url: str) -> dict[str, Any] | None:
    origin = origin_of(url)
    for profile in load_profiles().values():
        if origin in profile.get("origins", []):
            return profile
    return None


def get_profile(profile_id: str | None) -> dict[str, Any] | None:
    return load_profiles().get(profile_id or "")


def find_recipe(profile: dict[str, Any] | None, recipe_id: str | None) -> dict[str, Any] | None:
    if not profile or not recipe_id:
        return None
    return next((r for r in profile.get("recipes", []) if r["recipe_id"] == recipe_id), None)


def required_values(recipe: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for step in recipe["steps"]:
        for name in PLACEHOLDER.findall(step):
            if name not in names:
                names.append(name)
    return names


def recipe_catalog_for_prompt(profile: dict[str, Any]) -> str:
    lines = []
    for r in profile.get("recipes", []):
        planner_values = [v for v in required_values(r) if v not in APP_VALUES]
        lines.append(
            f"- recipe_id={r['recipe_id']} job_types={r['job_types']} region={r.get('region')} "
            f"element={r.get('element')!r}: {r['title']}. Values to supply: {planner_values}"
        )
    return "\n".join(lines)


def build_snapshot(
    profile: dict[str, Any] | None,
    recipe_id: str | None,
    job_type: str,
    values: dict[str, str],
    *,
    page_urls: list[str],
    viewports: list[str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """Return (recipe_snapshot, builder_steps, blockers)."""
    blockers: list[str] = []
    if profile is None:
        return None, [], ["No approved site profile for this site; editing path is unknown, needs human review."]
    recipe = find_recipe(profile, recipe_id)
    if recipe is None:
        return None, [], [f"No validated recipe {recipe_id!r} in profile {profile['id']}; needs human review."]
    if job_type not in recipe["job_types"]:
        blockers.append(f"Recipe {recipe_id} does not cover job type {job_type}.")

    all_values = dict(values)
    all_values["admin_url"] = profile.get("admin_url", "")
    all_values["page_urls"] = ", ".join(page_urls)
    all_values["viewports"] = " and ".join(viewports)

    steps: list[dict[str, Any]] = []
    for number, template in enumerate(recipe["steps"], start=1):
        text = template
        for name in PLACEHOLDER.findall(template):
            value = all_values.get(name)
            if value is None or str(value).strip() == "":
                blockers.append(f"Step {number}: value for {{{name}}} is missing.")
                continue
            text = text.replace("{" + name + "}", str(value))
        if PLACEHOLDER.search(text):
            blockers.append(f"Step {number} has an unresolved placeholder.")
        steps.append({"number": number, "instruction": text})

    computed = recipe_source_hash(recipe)
    if recipe.get("validation_status") != "validated":
        blockers.append(f"Recipe {recipe_id} is {recipe.get('validation_status', 'unvalidated')}; it must be validated on this site first.")
    if not recipe.get("validation_date"):
        blockers.append(f"Recipe {recipe_id} has no validation date.")
    if not recipe.get("source_hash"):
        blockers.append(f"Recipe {recipe_id} has no source hash.")
    elif recipe["source_hash"] != computed:
        blockers.append(f"Recipe {recipe_id} steps changed since validation (source hash mismatch).")

    snapshot = {
        "profile_id": profile["id"],
        "recipe_id": recipe["recipe_id"],
        "version": recipe["version"],
        "title": recipe["title"],
        "validation_status": recipe.get("validation_status"),
        "validation_date": recipe.get("validation_date"),
        "validated_by": recipe.get("validated_by"),
        "source_hash": recipe.get("source_hash"),
        "template_steps": list(recipe["steps"]),
        "values": all_values,
        "steps": [s["instruction"] for s in steps],
    }
    return snapshot, steps, blockers


def steps_match_snapshot(builder_steps: list[dict[str, Any]], snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    return [s["instruction"] for s in builder_steps] == snapshot.get("steps")

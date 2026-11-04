"""Schemas for model proposals. Model output is validated here before any
application code turns it into records."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

JobType = Literal["text_update", "link_button_change", "image_swap", "logo_change", "menu_change", "form_check", "other"]
Scope = Literal["element", "page", "shared_component", "page_set"]
Region = Literal["banner", "navigation", "main", "contentinfo", "complementary", "page"]
ElementRole = Literal["link", "button", "text", "heading", "image", "form"]
Viewport = Literal["desktop", "mobile"]

SUPPORTED_JOB_TYPES = {"text_update", "link_button_change", "image_swap", "logo_change", "menu_change", "form_check"}
LANDMARK_FOR_REGION = {"banner": "banner", "navigation": "navigation", "main": "main", "contentinfo": "contentinfo", "complementary": "complementary"}


class PlanRequirement(BaseModel):
    key: str = Field(description="Stable id such as req_1, req_2")
    text: str = Field(description="One atomic customer requirement, in plain words")
    source: str = Field(description="Exact quote from the request text, or 'attachment:<n>' for a screenshot")


class PlanTarget(BaseModel):
    key: str = Field(description="Short snake_case id, e.g. header_phone")
    page_urls: list[str] = Field(min_length=1, description="Crawled page URLs (exactly as listed) where this target appears and must change")
    target_description: str = Field(description="Human description, e.g. 'phone number link in the shared site header'")
    region: Region = Field(description="Landmark region the element sits in, as shown in the outline")
    element_role: ElementRole
    current_text: str = Field(
        description="Current visible text copied exactly from the outline. Images: the current image src URL. Menu additions: empty string. Forms: a short name such as 'contact form'"
    )
    desired_text: str = Field(
        description="Visible text after the change (same as current_text if only the link changes). Images: short description of the approved new image. Forms: the email address that must receive form messages, if given"
    )
    current_href: str | None = Field(default=None, description="Current link destination from the outline, if a link/button")
    desired_href: str | None = Field(default=None, description="Required link destination after the change (tel:+1..., mailto:..., URL), if it changes")
    viewports: list[Viewport] = Field(default_factory=lambda: ["desktop", "mobile"])
    desired_asset_attachment: int | None = Field(
        default=None, description="Images only: 1-based number of the customer's attached image that is the approved replacement"
    )
    menu_action: Literal["rename", "add"] = Field(default="rename", description="Menus only: rename/relink an existing item, or add a new one")
    expected_menu_order: list[str] | None = Field(default=None, description="Menus only: full list of menu labels in the required order, if the customer specified one")


class PlanJob(BaseModel):
    key: str = Field(description="Short snake_case id, e.g. job_header_phone")
    title: str
    type: JobType
    scope: Scope
    requirement_keys: list[str] = Field(min_length=1)
    depends_on_keys: list[str] = Field(default_factory=list)
    recipe_id: str | None = Field(default=None, description="recipe_id from the site profile catalog, or null if none fits")
    targets: list[PlanTarget] = Field(min_length=1, max_length=1, description="Exactly one target per atomic job")
    require_old_value_absent: bool = Field(default=True, description="True if the old text must disappear from this target's region on its pages")
    notes: str = ""


class PlanClarification(BaseModel):
    requirement_key: str
    question: str


class PlanExclusion(BaseModel):
    requirement_key: str
    reason: str


class PlannerOutput(BaseModel):
    summary: str = Field(description="One-sentence summary of what the customer wants")
    requirements: list[PlanRequirement] = Field(min_length=1)
    jobs: list[PlanJob] = Field(default_factory=list)
    clarifications: list[PlanClarification] = Field(default_factory=list)
    exclusions: list[PlanExclusion] = Field(default_factory=list)


class JudgeCheck(BaseModel):
    check_id: str
    result: Literal["pass", "fail", "needs_human"]
    explanation: str = Field(description="Builder-readable reason referencing what is visible in the images")
    confidence: float = Field(ge=0, le=1)


class JudgeOutput(BaseModel):
    checks: list[JudgeCheck]
    notes: str = ""


class DoneMessage(BaseModel):
    subject: str
    body: str

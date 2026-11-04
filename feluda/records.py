"""Durable records: Request, Job, Evidence, Verdict and their supporting tables.

JSON columns hold model-proposed structures after schema validation. Always
assign a new value (never mutate in place) so SQLAlchemy records the change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class Account(Base):
    """A customer account. Users see only the accounts they are assigned to ("*" = all)."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    site_origins: Mapped[list[str]] = mapped_column(JSON, default=list)
    email_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(80), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))  # care_lead | builder
    accounts: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["*"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    site_url: Mapped[str] = mapped_column(String(2048))
    site_host: Mapped[str] = mapped_column(String(255), index=True)
    customer: Mapped[str] = mapped_column(String(255), default="")
    channel: Mapped[str] = mapped_column(String(20), default="ticket")
    raw_text: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # [{id, text, source, disposition: jobs|clarification|excluded, note, reviewed_by}]
    requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    scope_revision: Mapped[int] = mapped_column(Integer, default=1)
    crawl_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    site_profile_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="parsing")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    planner_notes: Mapped[str] = mapped_column(Text, default="")
    done_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_message_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CoverageApproval(Base):
    """A Care lead's sign-off on the requirement-to-job map at one scope revision.

    Validity is computed, not stored: it holds only while the request's scope
    revision, requirement dispositions and every mapped job revision still match.
    """

    __tablename__ = "coverage_approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"), index=True)
    scope_revision: Mapped[int] = mapped_column(Integer)
    job_revisions: Mapped[dict[str, int]] = mapped_column(JSON)
    coverage_map: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved_by: Mapped[str] = mapped_column(String(80))
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    type: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(40))
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    exclusions: Mapped[list[str]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    site_profile_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requirement_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    recipe_values: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    recipe_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    builder_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    acceptance_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    current_submission_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    blockers: Mapped[list[str]] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # genuine change failures
    escalations: Mapped[int] = mapped_column(Integer, default=0)
    baseline_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_to: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JobEvent(Base):
    """Audit log: corrections, transitions and invalidations with author and revision."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("job_id", "client_token", name="uq_submission_client_token"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_revision: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(String(40), unique=True)
    client_token: Mapped[str] = mapped_column(String(80))
    submitted_by: Mapped[str] = mapped_column(String(80))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    site_ready_assertion: Mapped[bool] = mapped_column(Boolean, default=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempt_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    executions: Mapped[int] = mapped_column(Integer, default=0)
    max_executions: Mapped[int] = mapped_column(Integer, default=3)
    timeout_s: Mapped[int] = mapped_column(Integer, default=300)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_revision: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    closure_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phase: Mapped[str] = mapped_column(String(20))  # baseline | after | final | final_recheck
    target_id: Mapped[str] = mapped_column(String(80))
    requested_url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str] = mapped_column(String(2048), default="")
    viewport: Mapped[str] = mapped_column(String(20))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    page_screenshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    element_crop: Mapped[str | None] = mapped_column(String(500), nullable=True)
    context_crop: Mapped[str | None] = mapped_column(String(500), nullable=True)
    element_mask: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sam_box: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    dom_snippet: Mapped[str] = mapped_column(Text, default="")
    element_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    element_href: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    element_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    locate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # method, matched phase, match_count, path
    regions_text: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    http: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    capture_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # files deleted by retention


class Verdict(Base):
    __tablename__ = "verdicts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_revision: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    closure_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    check_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    result: Mapped[str] = mapped_column(String(30))  # pass | fail | needs_human | verification_error
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    regressions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    committed: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_reason: Mapped[str] = mapped_column(Text, default="")
    invalidated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SiteLease(Base):
    """Site-level edit freeze for the final consistency gate, with a fencing token."""

    __tablename__ = "site_leases"

    site_host: Mapped[str] = mapped_column(String(255), primary_key=True)
    token: Mapped[int] = mapped_column(Integer, default=0)
    holder: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClosureRun(Base):
    __tablename__ = "closure_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"), index=True)
    scope_revision: Mapped[int] = mapped_column(Integer)
    approval_id: Mapped[str] = mapped_column(String(40))
    job_revisions: Mapped[dict[str, int]] = mapped_column(JSON)
    submission_ids: Mapped[dict[str, str]] = mapped_column(JSON)
    lease_token: Mapped[int] = mapped_column(Integer)
    started_by: Mapped[str] = mapped_column(String(80))
    freeze_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fingerprints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    check_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|closed|blocked|error
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    job_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    purpose: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FormOperation(Base):
    """One correlated test-form submission. Never blindly resubmitted: an unresolved
    operation is resolved by inspecting the test inbox, not by submitting again."""

    __tablename__ = "form_operations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    submission_id: Mapped[str] = mapped_column(String(40))
    attempt_id: Mapped[str] = mapped_column(String(40), unique=True)
    correlation_id: Mapped[str] = mapped_column(String(80), unique=True)
    page_url: Mapped[str] = mapped_column(String(2048))
    # prepared -> submitted -> received | not_received | uncertain | failed
    status: Mapped[str] = mapped_column(String(20), default="prepared")
    success_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImageDraft(Base):
    """A Muse Image replacement-image draft; a Care lead must approve it before builders use it."""

    __tablename__ = "image_drafts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    media_type: Mapped[str] = mapped_column(String(40), default="image/webp")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|draft|approved|rejected|failed
    created_by: Mapped[str] = mapped_column(String(80))
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

"""Contact-form flow check: one correlated submission and a test-inbox receipt.

Only on sites whose profile approves form tests, only with an existing test
account from the secrets store, and never blindly repeated:

- each verification attempt owns at most one FormOperation (unique attempt_id);
- the operation is durably marked ``submitting`` *before* the click, so a crash
  or retry afterwards can never submit again; it resolves the operation by
  inspecting the inbox instead;
- an unresolved operation from an earlier attempt is resolved the same way
  before a new submission is allowed;
- missing inbox access or uncertain delivery can never pass.
"""

from __future__ import annotations

import imaplib
import time
import uuid
from datetime import timedelta
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote, urljoin

from sqlalchemy import select

from . import netguard, secrets_store, storage
from .checks import CheckResult
from .db import new_id, session, utcnow
from .records import Evidence, FormOperation

UNRESOLVED = ("prepared", "submitting", "submitted", "uncertain")


class InboxUnavailable(RuntimeError):
    pass


class DemoInbox:
    """The demo site's local test inbox (an approved test origin)."""

    def __init__(self, base_url: str, path: str):
        self.url = urljoin(base_url, path)

    def find(self, correlation_id: str) -> list[dict[str, Any]]:
        try:
            resp = netguard.guarded_get(f"{self.url}?contains={quote(correlation_id)}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise InboxUnavailable(f"test inbox unreachable: {e}") from e


class ImapInbox:
    """A real designated test mailbox; the password comes from the secrets store."""

    def __init__(self, host: str, user: str, password: str, port: int = 993, folder: str = "INBOX"):
        self.host, self.user, self.password, self.port, self.folder = host, user, password, port, folder

    def find(self, correlation_id: str) -> list[dict[str, Any]]:
        import email

        try:
            with imaplib.IMAP4_SSL(self.host, self.port) as box:
                box.login(self.user, self.password)
                box.select(self.folder, readonly=True)
                _typ, data = box.search(None, "BODY", f'"{correlation_id}"')
                found = []
                for num in (data[0] or b"").split()[:5]:
                    _t, msg_data = box.fetch(num, "(RFC822.HEADER)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    found.append({"to": msg.get("To", ""), "subject": msg.get("Subject", ""), "received_at": msg.get("Date", "")})
                return found
        except Exception as e:
            raise InboxUnavailable(f"IMAP test inbox unavailable: {type(e).__name__}") from e


def connector_for(cfg: dict[str, Any], site_url: str):
    inbox = cfg.get("inbox") or {}
    if inbox.get("type") == "demo":
        return DemoInbox(site_url, inbox.get("path", "/__demo/inbox"))
    if inbox.get("type") == "imap":
        secret = secrets_store.inbox_secret(cfg.get("test_account")) or {}
        if not secret.get("password"):
            raise InboxUnavailable("no IMAP password for this test inbox in the secrets store")
        return ImapInbox(inbox["host"], inbox["user"], secret["password"], int(inbox.get("port", 993)), inbox.get("folder", "INBOX"))
    raise InboxUnavailable("no test-inbox connector configured")


def _recipient_ok(to_header: str | None, expected_to: str | None) -> bool:
    """Compare addresses, not header text: 'Front Desk <frontdesk@x>' matches frontdesk@x."""
    if not expected_to:
        return True
    addresses = {addr.lower() for _name, addr in getaddresses([to_header or ""]) if addr}
    return expected_to.strip().lower() in addresses


def _receipt(connector, op: FormOperation, expected_to: str | None) -> dict[str, Any] | None:
    for msg in connector.find(op.correlation_id):
        return {"to": msg.get("to"), "subject": msg.get("subject"), "received_at": msg.get("received_at"), "recipient_ok": _recipient_ok(msg.get("to"), expected_to)}
    return None


def _resolve_by_inbox(connector, op_id: str, timeout_s: int, expected_to: str | None) -> FormOperation:
    """Settle an operation without resubmitting: wait out its receipt window, reading the inbox."""
    while True:
        with session() as s:
            op = s.get(FormOperation, op_id)
            receipt = _receipt(connector, op, expected_to)
            if receipt:
                op.status, op.receipt, op.resolved_at = "received", receipt, utcnow()
            elif op.status == "prepared":
                op.status, op.resolved_at = "failed", utcnow()
                op.detail = "never submitted"
            else:
                started = op.submitted_at or op.created_at
                if utcnow() - started >= timedelta(seconds=timeout_s):
                    op.status = "not_received" if op.status == "submitted" else "uncertain"
                    op.resolved_at = utcnow()
                    op.detail = f"no matching message in the test inbox within {timeout_s}s"
            if op.resolved_at is not None:
                s.flush()  # persist the settled status before detaching
                s.expunge(op)
                return op
        time.sleep(2)


def run(
    capturer,
    *,
    job: dict[str, Any],
    request_id: str,
    site_url: str,
    profile: dict[str, Any],
    submission_id: str,
    attempt_id: str,
    task_id: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (check_results for form_submission and inbox_receipt, evidence ids)."""
    cfg = profile.get("form_tests") or {}
    target = job["targets"][0]
    sub_check = CheckResult(f"{target['id']}.form_submission", "form_submission", target["id"], True)
    inbox_check = CheckResult(f"{target['id']}.inbox_receipt", "inbox_receipt", target["id"], True)
    expected_to = target["desired_value"] if "@" in (target.get("desired_value") or "") else cfg.get("recipient")
    timeout_s = int(cfg.get("receipt_timeout_s", 60))

    def done(sub_result, sub_why, inbox_result, inbox_why, evidence_ids):
        sub_check.result, sub_check.explanation = sub_result, sub_why
        inbox_check.result, inbox_check.explanation = inbox_result, inbox_why
        for c in (sub_check, inbox_check):
            c.evidence_ids = evidence_ids
        return [sub_check.as_dict(), inbox_check.as_dict()], evidence_ids

    account = secrets_store.test_account(cfg.get("test_account"))
    if not cfg.get("enabled") or not account:
        return done("needs_human", "form tests are not approved for this site or no test account exists", "needs_human", "not attempted", [])
    try:
        connector = connector_for(cfg, site_url)
    except Exception as e:
        return done("error", f"not attempted: {e}", "error", str(e), [])

    # 1. Earlier attempts' unresolved operations are settled by inspecting the inbox, never resubmitted.
    with session() as s:
        earlier = [o.id for o in s.scalars(select(FormOperation).where(FormOperation.job_id == job["id"], FormOperation.attempt_id != attempt_id, FormOperation.status.in_(UNRESOLVED)))]
    for op_id in earlier:
        try:
            op = _resolve_by_inbox(connector, op_id, timeout_s, expected_to)
        except Exception as e:
            return done("error", f"an earlier test submission is unresolved and the inbox cannot be read: {e}", "error", str(e), [])
        if op.status == "uncertain":
            return done("needs_human", f"earlier test submission {op.correlation_id} may have been sent but never arrived; inspect the test inbox before another submission", "needs_human", "unresolved earlier submission", [])

    # 2. This attempt's operation: create it, or resume it without resubmitting.
    with session() as s:
        op = s.scalar(select(FormOperation).where(FormOperation.attempt_id == attempt_id))
        if op is None:
            op = FormOperation(
                id=new_id("formop"),
                job_id=job["id"],
                submission_id=submission_id,
                attempt_id=attempt_id,
                correlation_id=f"FEL-{uuid.uuid4().hex[:10].upper()}",
                status="prepared",  # explicit: column defaults only apply at INSERT
                page_url=target["page_urls"][0],
            )
            s.add(op)
        op_id, op_status, correlation_id, page_url = op.id, op.status, op.correlation_id, op.page_url

    evidence_ids: list[str] = []
    feedback = None
    if op_status == "prepared":
        values = {"correlation_id": correlation_id, **{f"account.{k}": v for k, v in account.items()}}
        fields = {}
        for selector, template in (cfg.get("fields") or {}).items():
            text = template
            for k, v in values.items():
                text = text.replace("{" + k + "}", str(v))
            fields[selector] = text

        def mark_submitting():
            with session() as s:
                o = s.get(FormOperation, op_id)
                o.status, o.submitted_at = "submitting", utcnow()

        out = capturer.run_form(page_url, fields=fields, submit_selector=cfg["submit_selector"], success_text=cfg["success_text"], on_submitting=mark_submitting)
        if out.get("page_png"):
            ev_id = new_id("ev")
            key = storage.put(f"{request_id}/{job['id']}/{ev_id}/page.png", out["page_png"])
            with session() as s:
                s.add(
                    Evidence(
                        id=ev_id, request_id=request_id, job_id=job["id"], job_revision=job["revision"], submission_id=submission_id,
                        attempt_id=attempt_id, task_id=task_id, phase="after", target_id=target["id"], requested_url=page_url,
                        final_url=out.get("final_url") or "", viewport="desktop", page_screenshot=key, element_text=out.get("feedback"),
                        locate={"matched_phase": "flow", "match_count": 1, "info": {"correlation_id": correlation_id}},
                        http={"status": out.get("status")}, capture_settings={"flow": "contact_form", "correlation_id": correlation_id}, error=out.get("error"),
                    )
                )
            evidence_ids.append(ev_id)
        never_clicked = bool(out.get("error")) and not out.get("submitted")
        with session() as s:
            o = s.get(FormOperation, op_id)
            o.evidence_id = evidence_ids[0] if evidence_ids else None
            if never_clicked:
                o.status = "prepared"  # nothing reached the site: safe to retry as infrastructure
                o.detail = out["error"]
            elif out.get("error"):
                o.status, o.detail = "uncertain", f"submission response unclear: {out['error']}"
            else:
                o.status, o.success_feedback = "submitted", out.get("feedback")
            op_status, feedback = o.status, o.success_feedback
        if never_clicked:
            raise RuntimeError(f"form check could not run: {out['error']}")
        success_seen = bool(out.get("success_seen"))
    else:
        with session() as s:
            o = s.get(FormOperation, op_id)
            feedback, success_seen = o.success_feedback, bool(o.success_feedback)
            if o.evidence_id:
                evidence_ids.append(o.evidence_id)

    # 3. Receipt: read the inbox until the timeout. Never resubmit.
    try:
        if op_status in ("submitting", "submitted", "uncertain"):
            op = _resolve_by_inbox(connector, op_id, timeout_s, expected_to)
        else:  # already settled by an earlier execution of this attempt: report it, never resubmit
            with session() as s:
                op = s.get(FormOperation, op_id)
                s.flush()  # persist the settled status before detaching
                s.expunge(op)
    except Exception as e:
        return done("pass" if success_seen else "needs_human", feedback or "submitted", "error", f"test inbox unreadable: {e}", evidence_ids)
    if op.status == "failed":
        return done("error", f"submission {correlation_id} never ran: {op.detail}", "error", "not checked", evidence_ids)
    if op_status == "uncertain" and op.status != "received":
        return done("needs_human", f"submission {correlation_id} response was unclear", "needs_human", "delivery unresolved; Feluda will not resubmit", evidence_ids)

    sub = ("pass", f"submitted once ({correlation_id}); page showed: {feedback!r}") if success_seen else ("fail", f"submitted once ({correlation_id}) but no success message was shown")
    if op.status == "received":
        if op.receipt.get("recipient_ok"):
            inbox = ("pass", f"message {correlation_id} arrived at {op.receipt.get('to')}")
        else:
            inbox = ("fail", f"message {correlation_id} arrived at {op.receipt.get('to')}, expected {expected_to}")
    else:
        inbox = ("fail", f"no message {correlation_id} arrived in the test inbox within {timeout_s}s" + (" although the site showed success" if success_seen else ""))
    return done(*sub, *inbox, evidence_ids)

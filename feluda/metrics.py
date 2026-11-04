"""Operational metrics from the spec: verdict latency (median, p95), outcome
rates and model cost per request and per verification attempt.

Latency runs from the builder's Mark done (submission) to the committed
verdict, so it includes queueing and retries. Final-gate verdicts are
excluded. Every figure is reported with its count; small samples say little.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from .db import session
from .records import Job, ModelCall, Request, Submission, Verdict


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 2)


def _summary(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "median": percentile(values, 50), "p95": percentile(values, 95), "max": round(max(values), 4) if values else None}


def compute(account_ids: list[str] | None = None) -> dict[str, Any]:
    """``account_ids`` None or containing "*" means every account."""
    every = account_ids is None or "*" in account_ids
    with session() as s:
        reqs = {r.id: r for r in s.scalars(select(Request)) if every or r.account_id in account_ids}
        jobs = {j.id: j for j in s.scalars(select(Job).where(Job.request_id.in_(list(reqs) or [""])))}
        subs = {x.id: x for x in s.scalars(select(Submission).where(Submission.job_id.in_(list(jobs) or [""])))}
        verdicts = [
            v for v in s.scalars(select(Verdict).where(Verdict.job_id.in_(list(jobs) or [""]), Verdict.committed.is_(True), Verdict.closure_run_id.is_(None)))
        ]
        calls = [c for c in s.scalars(select(ModelCall).where(ModelCall.request_id.in_(list(reqs) or [""])))]

        latency_all: list[float] = []
        latency_by_type: dict[str, list[float]] = defaultdict(list)
        latency_by_result: dict[str, list[float]] = defaultdict(list)
        results: dict[str, int] = defaultdict(int)
        by_type_result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for v in verdicts:
            results[v.result] += 1
            job = jobs[v.job_id]
            by_type_result[job.type][v.result] += 1
            sub = subs.get(v.submission_id)
            if sub:
                secs = (v.created_at - sub.submitted_at).total_seconds()
                latency_all.append(secs)
                latency_by_type[job.type].append(secs)
                latency_by_result[v.result].append(secs)

        per_request: dict[str, float] = defaultdict(float)
        per_attempt: dict[str, float] = defaultdict(float)
        by_purpose: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"calls": 0, "failed": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        for c in calls:
            per_request[c.request_id] += c.cost_usd
            if c.attempt_id:
                per_attempt[c.attempt_id] += c.cost_usd
            row = by_purpose[(c.purpose, c.model)]
            row["calls"] += 1
            row["failed"] += 0 if c.ok else 1
            row["cost_usd"] += c.cost_usd
            row["input_tokens"] += c.input_tokens
            row["output_tokens"] += c.output_tokens

    total = sum(results.values())
    rate = lambda k: round(results.get(k, 0) / total, 3) if total else None  # noqa: E731
    return {
        "verdicts": total,
        "results": dict(results),
        "rates": {"pass": rate("pass"), "fail": rate("fail"), "human_review": rate("needs_human"), "verification_error": rate("verification_error")},
        "by_type": {t: dict(r) for t, r in by_type_result.items()},
        "latency_s": {
            "all": _summary(latency_all),
            "by_type": {t: _summary(v) for t, v in latency_by_type.items()},
            "by_result": {t: _summary(v) for t, v in latency_by_result.items()},
            "target_median_s": 120,
        },
        "cost_usd": {
            "per_request": _summary(list(per_request.values())),
            "per_attempt": _summary(list(per_attempt.values())),
            "total": round(sum(per_request.values()), 4),
            "target_per_request": 0.25,
            "by_purpose": [
                {"purpose": p, "model": m, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}}
                for (p, m), row in sorted(by_purpose.items())
            ],
        },
        "requests": len(reqs),
        "closed_requests": sum(1 for r in reqs.values() if r.closed_at),
    }

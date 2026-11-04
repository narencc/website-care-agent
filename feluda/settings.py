"""Application settings (database, storage, model roles, verification limits).

Model endpoint credentials live in ``feluda.config``; this module holds
everything else. Values come from the environment (and .env), with defaults
that run the prototype locally against the bundled demo site.

    DATABASE_URL                    sqlite:///var/feluda.db  (postgresql+psycopg://... in production)
    FELUDA_DATA_DIR                 ./var       evidence and attachment storage root
    FELUDA_SECRET                   HMAC key for signed evidence links
    FELUDA_APPROVED_TEST_ORIGINS    comma-separated origins allowed even if private (e.g. http://localhost:8100)
    FELUDA_PLANNER_MODEL            muse-spark-1.3
    FELUDA_JUDGE_MODEL              muse-spark-1.3
    FELUDA_JUDGE_FALLBACK_MODEL     muse-spark-1.1
    FELUDA_ESCALATION_MODEL         muse-wonder-1.0
    FELUDA_SAM_MODEL                sam-3.1
    FELUDA_USE_SAM                  1
    FELUDA_INLINE_WORKER            1           run the task worker in a thread of the web process
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import _load_dotenv

# USD prices from Meta's model page, per 1M tokens (SAM: per image). Planning
# assumptions until confirmed against billing; see the spec's validation note.
PRICES = {
    "muse-spark-1.3": {"in": 1.25, "out": 4.25},
    "muse-spark-1.1": {"in": 1.25, "out": 4.25},
    "muse-wonder-1.0": {"in": 4.00, "out": 20.00},
    "sam-3.1": {"image": 0.0025},
}

# Contributor tiers train Meta's models; customer data must never reach them.
FORBIDDEN_MODEL_MARKERS = ("contributor",)


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int


VIEWPORTS = {
    "desktop": Viewport("desktop", 1366, 900),
    "mobile": Viewport("mobile", 390, 844),
}


@dataclass(frozen=True)
class AppSettings:
    database_url: str
    data_dir: Path
    secret: str
    approved_test_origins: tuple[str, ...]
    planner_model: str
    judge_model: str
    judge_fallback_model: str
    escalation_model: str
    sam_model: str
    use_sam: bool
    inline_worker: bool
    crawl_page_limit: int = 8
    capture_timeout_ms: int = 30_000
    freshness_retries: int = 2
    freshness_retry_delay_s: float = 3.0
    task_max_executions: int = 3
    task_lease_s: int = 300
    site_lease_ttl_s: int = 120
    closure_window_s: int = 300
    fingerprint_max_age_s: int = 30
    request_cost_cap_usd: float = 1.00
    max_escalations_per_job: int = 2
    attachment_max_bytes: int = 5 * 1024 * 1024
    signed_link_ttl_s: int = 900

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_app_settings(env: dict[str, str] | None = None) -> AppSettings:
    if env is None:
        _load_dotenv()
        env = dict(os.environ)
    data_dir = Path(env.get("FELUDA_DATA_DIR") or "var").resolve()
    origins = tuple(o.strip().rstrip("/") for o in (env.get("FELUDA_APPROVED_TEST_ORIGINS") or "").split(",") if o.strip())
    return AppSettings(
        database_url=env.get("DATABASE_URL") or f"sqlite:///{(data_dir / 'feluda.db').as_posix()}",
        data_dir=data_dir,
        secret=env.get("FELUDA_SECRET") or "dev-only-secret-change-me",
        approved_test_origins=origins,
        planner_model=env.get("FELUDA_PLANNER_MODEL") or "muse-spark-1.3",
        judge_model=env.get("FELUDA_JUDGE_MODEL") or "muse-spark-1.3",
        judge_fallback_model=env.get("FELUDA_JUDGE_FALLBACK_MODEL") or "muse-spark-1.1",
        escalation_model=env.get("FELUDA_ESCALATION_MODEL") or "muse-wonder-1.0",
        sam_model=env.get("FELUDA_SAM_MODEL") or "sam-3.1",
        use_sam=_flag(env.get("FELUDA_USE_SAM"), True),
        inline_worker=_flag(env.get("FELUDA_INLINE_WORKER"), True),
        freshness_retry_delay_s=float(env.get("FELUDA_FRESHNESS_RETRY_DELAY_S") or 3.0),
        request_cost_cap_usd=float(env.get("FELUDA_REQUEST_COST_CAP_USD") or 1.0),
    )


_override: AppSettings | None = None


@lru_cache(maxsize=1)
def _cached() -> AppSettings:
    return load_app_settings()


def app_settings() -> AppSettings:
    return _override or _cached()


def use_settings(s: AppSettings | None) -> None:
    """Swap settings (tests and scripts)."""
    global _override
    _override = s

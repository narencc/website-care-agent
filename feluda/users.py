"""Users, logins and per-customer access.

Passwords are stored as salted PBKDF2-SHA256 hashes. Sessions are HMAC-signed
cookies with an expiry. Each user is limited to a list of customer accounts
("*" = every account); records, tasks and evidence of other accounts are
invisible to them.

    python -m feluda.users add NAME ROLE [--accounts acct_a,acct_b]   # prompts for the password
    python -m feluda.users seed-dev        # demo users with generated passwords -> var/dev_users.txt
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import time
from typing import Iterable

from sqlalchemy import select

from .db import init_db, session
from .records import Account, User
from .settings import app_settings

ROLES = ("care_lead", "builder")
ITERATIONS = 260_000
SESSION_TTL_S = 12 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def ensure_user(username: str, role: str, accounts: Iterable[str] = ("*",), password: str | None = None) -> None:
    """Create or update a user. Without a password the account cannot log in (service use only)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    with session() as s:
        user = s.get(User, username)
        if user is None:
            user = User(username=username, role=role, password_hash="!")
            s.add(user)
        user.role, user.accounts, user.active = role, list(accounts), True
        if password is not None:
            user.password_hash = hash_password(password)


def authenticate(username: str, password: str) -> str | None:
    with session() as s:
        user = s.get(User, (username or "").strip())
        if user and user.active and verify_password(password or "", user.password_hash):
            return user.username
    hash_password("timing-equaliser")  # similar cost whether or not the user exists
    return None


def role_of(username: str | None) -> str | None:
    if not username:
        return None
    with session() as s:
        user = s.get(User, username)
        return user.role if user and user.active else None


def accounts_of(username: str | None) -> list[str]:
    if not username:
        return []
    with session() as s:
        user = s.get(User, username)
        return list(user.accounts) if user and user.active else []


def can_access(username: str | None, account_id: str | None) -> bool:
    allowed = accounts_of(username)
    return "*" in allowed or (account_id is not None and account_id in allowed)


def _sign(payload: str) -> str:
    return hmac.new(app_settings().secret.encode(), f"session|{payload}".encode(), hashlib.sha256).hexdigest()[:40]


def make_session(username: str) -> str:
    payload = f"{username}|{int(time.time()) + SESSION_TTL_S}"
    return f"{payload}|{_sign(payload)}"


def read_session(token: str | None) -> str | None:
    if not token or token.count("|") != 2:
        return None
    username, exp, sig = token.split("|")
    if not hmac.compare_digest(sig, _sign(f"{username}|{exp}")) or int(exp) < time.time():
        return None
    return username if role_of(username) else None


# --- Accounts ------------------------------------------------------------------------


def account_for_site(s, origin: str, *, create: bool = True) -> Account | None:
    for acct in s.scalars(select(Account)):
        if origin in (acct.site_origins or []):
            return acct
    if not create:
        return None
    host = origin.split("://", 1)[-1]
    acct = Account(id="acct_" + "".join(c if c.isalnum() else "_" for c in host)[:30], name=host, site_origins=[origin])
    s.add(acct)
    s.flush()
    return acct


def account_for_email(s, sender: str) -> Account | None:
    domain = sender.rsplit("@", 1)[-1].strip().lower().rstrip(">")
    for acct in s.scalars(select(Account)):
        if domain and domain in [d.lower() for d in acct.email_domains or []]:
            return acct
    return None


DEV_USERS = (("care.lead", "care_lead"), ("builder.one", "builder"), ("builder.two", "builder"))


def ensure_dev_users() -> None:
    """Service-only demo users (no password) for scripts and tests."""
    for name, role in DEV_USERS:
        with session() as s:
            exists = s.get(User, name) is not None
        if not exists:
            ensure_user(name, role)


def seed_dev() -> str:
    """Demo users with fresh random passwords, written to a local, gitignored file."""
    from . import secrets_store

    init_db()
    lines = ["# Local demo logins (generated; do not commit). Re-run seed-dev to rotate."]
    for name, role in DEV_USERS:
        password = secrets.token_urlsafe(12)
        ensure_user(name, role, password=password)
        lines.append(f"{name}\t{role}\t{password}")
    path = app_settings().data_dir / "dev_users.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    secrets_store.write_secrets(
        {"test_accounts": {"demo_dental_local": {"name": "Website Care Test", "email": "care-test@feluda.example"}}}
    )
    return str(path)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m feluda.users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("role", choices=ROLES)
    add.add_argument("--accounts", default="*", help="comma-separated account ids, or * for all")
    sub.add_parser("seed-dev")
    args = ap.parse_args()
    init_db()
    if args.cmd == "add":
        password = os.environ.get("FELUDA_NEW_PASSWORD") or getpass.getpass(f"Password for {args.username}: ")
        if len(password) < 10:
            raise SystemExit("Use at least 10 characters.")
        ensure_user(args.username, args.role, [a.strip() for a in args.accounts.split(",") if a.strip()], password=password)
        print(f"User {args.username} ({args.role}) saved.")
    else:
        print(f"Demo users created; passwords are in {seed_dev()}")


if __name__ == "__main__":
    main()

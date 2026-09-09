"""Manage AuthKit user invitations (the app-wide whitelist) via the WorkOS API.

Lets an operator add, refresh, revoke or list invitations for the AuthKit
APPLICATION's end users — the same people who sign in to this app. Invitations
are the whitelist: with Sign-up off in the dashboard, only an invited email can
create an account.

This operates on the APP-user layer, NOT the WorkOS workspace: the endpoint is
the AuthKit user-management API (api.workos.com/user_management/invitations),
and an invitation sent WITHOUT an organization_id is an app-wide invitation
("join the application", per WorkOS docs). A dashboard invite that asks you to
pick an organization and a member/admin role is inviting someone into your
WorkOS WORKSPACE instead — don't use that one. Use this script, or the
dashboard's app-wide invite (leave the organization unset).

What you need from WorkOS (dashboard, staging or production environment):
    Developer -> API keys -> create an API key (sk_...). It must come from the
    SAME environment whose users you are managing (staging key manages staging
    users, which is what a locally run app signs into). The key is a local
    admin secret: keep it in apps/api/.env as WORKOS_API_KEY=sk_..., never in
    the repo or in the deployed service's environment.

How invitations behave:
    add       sends a fresh invitation email.
    refresh   resends an existing PENDING invitation (restarts its accept
              window); if there is no pending invitation for the email it
              simply adds one.
    revoke    withdraws a pending invitation. Revoking someone who already
              accepted does nothing (they are a user now; remove them in
              Dashboard -> Users instead).
    list      shows pending/accepted/revoked invitations.

Accepting an invitation proves the inbox, so a new user who signs up with the
invited email is auto-verified (no separate email OTP) as long as they accept
promptly; resending restarts that window.

The app's AuthKit DEFAULT application must have the app origin set as its
default redirect URI, or an accepted invitation has nowhere to land and ends on
AuthKit's broken /default-redirect page (see README, "Inviting users").

Usage:
    # add a few people
    uv run python scripts/manage_workos_users.py add alice@example.com bob@example.com

    # add from a file: one email per line, or a CSV with an "email" column
    uv run python scripts/manage_workos_users.py add --file invites.csv
    uv run python scripts/manage_workos_users.py add --file emails.txt

    # a stale invite: resend if one exists, otherwise send a new one
    uv run python scripts/manage_workos_users.py refresh alice@example.com

    # withdraw an invite
    uv run python scripts/manage_workos_users.py revoke bob@example.com

    # see what's outstanding
    uv run python scripts/manage_workos_users.py list

    # preview without calling the API
    uv run python scripts/manage_workos_users.py add --file emails.txt --dry-run
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import requests

API_BASE = "https://api.workos.com/user_management/invitations"
# apps/api/.env, relative to this script (apps/api/scripts/manage_workos_users.py)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ApiError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("WORKOS_API_KEY", "").strip()
    if not key and _ENV_FILE.is_file():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("WORKOS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not key:
        raise ApiError(
            "WORKOS_API_KEY is not set. Put WORKOS_API_KEY=sk_... in apps/api/.env "
            "(from Dashboard -> Developer -> API keys, same environment as the "
            "users you are managing).")
    return key


def _api(method: str, path: str, *, json: dict | None = None) -> dict:
    resp = requests.request(
        method, f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"},
        json=json, timeout=20)
    if resp.status_code >= 400:
        raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else {}


def _emails(args: argparse.Namespace) -> list[str]:
    """Positional emails plus --file contents, de-duplicated, order preserved."""
    emails: list[str] = []
    for line in args.emails or []:
        emails.extend(e.strip() for e in line.split(",") if e.strip())
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            raise ApiError(f"--file: not found: {path}")
        if path.suffix.lower() == ".csv":
            with path.open(newline="") as fh:
                rows = list(csv.reader(fh))
            header = rows[0] if rows else []
            col = next((i for i, h in enumerate(header)
                        if h.strip().lower() == "email"), 0)
            for row in rows[1 if header else 0:]:
                if len(row) > col and row[col].strip():
                    emails.append(row[col].strip())
        else:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    emails.append(line.split(",")[0].strip())
    seen: set[str] = set()
    return [e for e in emails if not (e in seen or seen.add(e))]


def _pending_invite(email: str) -> dict | None:
    for inv in _api("GET", "")["data"]:
        if inv.get("email") == email and inv.get("state") == "pending":
            return inv
    return None


def _send(email: str, org: str | None) -> dict:
    body = {"email": email}
    if org:
        body["organization_id"] = org
    return _api("POST", "", json=body)


def _cmd_add(args: argparse.Namespace) -> None:
    for email in _emails(args):
        if args.dry_run:
            print(f"would invite  {email}")
        else:
            inv = _send(email, args.organization)
            print(f"invited      {email}  ({inv['id']})")


def _cmd_refresh(args: argparse.Namespace) -> None:
    for email in _emails(args):
        existing = None if args.dry_run else _pending_invite(email)
        if existing:
            if args.dry_run:
                print(f"would resend {email}")
            else:
                _api("POST", f"/{existing['id']}/resend")
                print(f"resent       {email}  ({existing['id']})")
        else:
            if args.dry_run:
                print(f"would invite {email}  (no pending invite to resend)")
            else:
                inv = _send(email, getattr(args, "organization", None))
                print(f"invited      {email}  ({inv['id']})  (none was pending)")


def _cmd_revoke(args: argparse.Namespace) -> None:
    for email in _emails(args):
        if args.dry_run:
            print(f"would revoke {email}")
            continue
        existing = _pending_invite(email)
        if existing:
            _api("POST", f"/{existing['id']}/revoke")
            print(f"revoked      {email}  ({existing['id']})")
        else:
            print(f"nothing to revoke for {email}  (no pending invite)")


def _cmd_list(args: argparse.Namespace) -> None:
    want = set(_emails(args))
    rows = _api("GET", "")["data"]
    for inv in rows:
        if want and inv.get("email") not in want:
            continue
        print(f"{inv.get('email', ''):40} {inv.get('state', ''):10} {inv.get('id', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manage_workos_users",
        description="Add, refresh, revoke or list AuthKit app-wide user "
                    "invitations (the email whitelist). See the module "
                    "docstring for what you need from WorkOS and full usage.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("emails", nargs="*", help="one or more email addresses")
        p.add_argument("--file", metavar="PATH",
                       help="read emails from a file: one per line, or a CSV "
                            "with an 'email' column")
        p.add_argument("--dry-run", action="store_true",
                       help="print what would happen without calling WorkOS")

    p_add = sub.add_parser("add", help="send a new invitation")
    shared(p_add)
    p_add.add_argument("--organization", metavar="ORG_ID",
                       help="invite into an organization instead of app-wide")

    p_refresh = sub.add_parser("refresh",
                               help="resend a pending invitation, or add one if "
                                    "none is pending")
    shared(p_refresh)
    p_refresh.add_argument("--organization", metavar="ORG_ID", help=argparse.SUPPRESS)

    p_revoke = sub.add_parser("revoke", help="withdraw a pending invitation")
    shared(p_revoke)

    p_list = sub.add_parser("list", help="list invitations (optionally by email)")
    shared(p_list)

    args = parser.parse_args(argv)
    try:
        {"add": _cmd_add, "refresh": _cmd_refresh,
         "revoke": _cmd_revoke, "list": _cmd_list}[args.cmd](args)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

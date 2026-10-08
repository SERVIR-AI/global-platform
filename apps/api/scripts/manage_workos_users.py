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
    add       sends a fresh invitation email. An email that already has an
              invitation or an account is reported and skipped; the rest of
              the batch still goes out.
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

    # add from a Google Sheet (sharing must be "Anyone with the link can view").
    # Uses the first column whose header contains "email" and holds valid
    # emails. Rows whose "invited" column is filled in (e.g. TRUE, yes) are
    # skipped by add/refresh. --startrow/--endrow are sheet row numbers
    # (header is row 1), inclusive.
    uv run python scripts/manage_workos_users.py add --sheet "https://docs.google.com/spreadsheets/d/<id>/edit#gid=0"
    uv run python scripts/manage_workos_users.py add --sheet "<url>" --startrow 10 --endrow 14 --dry-run

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
import io
import os
import re
import sys
from pathlib import Path

import requests

API_BASE = "https://api.workos.com/user_management/invitations"
# WorkOS error codes meaning the email is already on the whitelist.
_ALREADY_THERE = {"email_already_invited", "email_not_available", "user_already_exists"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Values in a sheet's "invited" column that mean "not invited yet".
_NOT_INVITED = {"", "false", "no", "n", "0"}
# apps/api/.env, relative to this script (apps/api/scripts/manage_workos_users.py)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        # WorkOS error identifier from the response body, e.g. "email_already_invited".
        self.code = code


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
        try:
            code = resp.json().get("code")
        except ValueError:
            code = None
        raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}",
                       status=resp.status_code, code=code)
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
    if args.sheet:
        emails.extend(_sheet_emails(args))
    seen: set[str] = set()
    return [e for e in emails if not (e in seen or seen.add(e))]


def _sheet_rows(url: str) -> list[list[str]]:
    """Download one tab of a link-shared Google Sheet as CSV rows."""
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m:
        raise ApiError(f"--sheet: not a Google Sheets URL: {url}")
    export = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    gid = re.search(r"[#&?]gid=(\d+)", url)
    if gid:
        export += f"&gid={gid.group(1)}"
    resp = requests.get(export, timeout=20)
    if resp.status_code >= 400 or "text/csv" not in resp.headers.get("Content-Type", ""):
        raise ApiError("--sheet: could not download the sheet as CSV. Set its "
                       "sharing to 'Anyone with the link can view'.")
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))


def _sheet_emails(args: argparse.Namespace) -> list[str]:
    """Emails from --sheet rows startrow..endrow (sheet row numbers, inclusive).

    The email column is the first header containing "email" whose cells hold
    at least one valid email. With no such column, no emails are returned.
    For add/refresh, rows marked in an "invited" column are skipped.
    """
    rows = _sheet_rows(args.sheet)
    if not rows:
        print("sheet is empty; no emails read", file=sys.stderr)
        return []
    header = [h.strip().lower() for h in rows[0]]

    def cell(row: list[str], i: int) -> str:
        return row[i].strip() if len(row) > i else ""

    col = next((i for i, h in enumerate(header) if "email" in h
                and any(_EMAIL_RE.match(cell(r, i)) for r in rows[1:])), None)
    if col is None:
        print("sheet has no column with 'email' in its header and valid emails "
              "in its cells; no emails read", file=sys.stderr)
        return []
    invited_col = next((i for i, h in enumerate(header) if "invited" in h), None)
    skip_invited = args.cmd in ("add", "refresh") and invited_col is not None

    first = max(args.startrow or 2, 2)
    last = min(args.endrow or len(rows), len(rows))
    emails: list[str] = []
    for n in range(first, last + 1):
        row = rows[n - 1]
        email = cell(row, col)
        if skip_invited and cell(row, invited_col).lower() not in _NOT_INVITED:
            print(f"skipped      {email or '(blank)'}  (row {n} marked invited)")
        elif not _EMAIL_RE.match(email):
            if email:
                print(f"skipped      {email}  (row {n} is not a valid email)")
        else:
            emails.append(email)
    return emails


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
            try:
                inv = _send(email, args.organization)
            except ApiError as e:
                # WorkOS rejects an email that already has an invitation or an
                # account; that is not a reason to abandon the rest of the batch.
                if e.code in _ALREADY_THERE:
                    print(f"skipped      {email}  (already invited or registered)")
                    continue
                raise RuntimeError(e)
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
        p.add_argument("--sheet", metavar="URL",
                       help="read emails from a Google Sheet shared as "
                            "'Anyone with the link can view'")
        p.add_argument("--startrow", type=int, metavar="N",
                       help="first sheet row to read (header is row 1)")
        p.add_argument("--endrow", type=int, metavar="N",
                       help="last sheet row to read, inclusive")
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
    if (args.startrow or args.endrow) and not args.sheet:
        parser.error("--startrow/--endrow need --sheet")
    if args.startrow and args.endrow and args.endrow < args.startrow:
        parser.error("--endrow is before --startrow")
    try:
        {"add": _cmd_add, "refresh": _cmd_refresh,
         "revoke": _cmd_revoke, "list": _cmd_list}[args.cmd](args)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

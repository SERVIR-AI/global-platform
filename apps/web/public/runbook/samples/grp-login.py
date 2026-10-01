#!/usr/bin/env python3
"""grp-login — get a Global Risk Platform token from a terminal.

The platform's API and MCP take the same bearer token that Claude gets when it logs
you in. This does that login from a shell, with nothing but the standard library:
the OAuth 2.0 device-authorization flow against the platform's AuthKit issuer,
minted for the platform's resource so the platform accepts it.

    python3 grp-login.py start      # prints a URL and a code; open it, approve
    python3 grp-login.py finish     # collects the token and saves it (never printed)
    python3 grp-login.py login      # both, for an interactive terminal
    python3 grp-login.py token      # prints the current access token (refreshes if expired)
    python3 grp-login.py check      # asks the platform whether it accepts the token

    curl -H "Authorization: Bearer $(python3 grp-login.py token)" \\
         -H "Content-Type: application/x-yaml" --data-binary @manifest.yml \\
         https://servirplatform.sig-gis.com/api/contribute

The token identifies YOU; contributions made with it are recorded under your name.
Treat ~/.grp/token.json like a password.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PLATFORM = os.environ.get("GRP_PLATFORM", "https://servirplatform.sig-gis.com")
RESOURCE = f"{PLATFORM}/mcp"                 # the audience the platform verifies
HOME = os.path.join(os.path.expanduser("~"), ".grp")
CLIENT_FILE = os.path.join(HOME, "client.json")
PENDING_FILE = os.path.join(HOME, "pending.json")
TOKEN_FILE = os.path.join(HOME, "token.json")
SCOPE = "openid profile email offline_access"
# Cloudflare in front of the issuer bans Python's default user-agent outright
# ("browser_signature_banned"); a tool should say what it is anyway.
UA = "grp-login/1.0 (Global Risk Platform CLI; +https://servirplatform.sig-gis.com/runbook/)"


# ----------------------------------------------------------------- plumbing

def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _post(url: str, form: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    if body is not None:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": UA}
    else:
        data = urllib.parse.urlencode(form or {}).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json", "User-Agent": UA}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": "http_error", "error_description": e.reason}


def _save(path: str, obj: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _load(path: str) -> dict | None:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def _die(msg: str, code: int = 1) -> None:
    print(f"grp-login: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------- the issuer

def discover() -> dict:
    """The platform says who issues its tokens; the issuer says where its endpoints are."""
    prm = _get(f"{PLATFORM}/.well-known/oauth-protected-resource/mcp")
    issuer = (prm.get("authorization_servers") or [None])[0]
    if not issuer:
        _die("the platform's discovery document names no authorization server")
    meta = _get(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server")
    for k in ("device_authorization_endpoint", "token_endpoint"):
        if not meta.get(k):
            _die(f"the issuer does not advertise {k} — device login is not enabled there")
    return {"issuer": issuer, "resource": prm.get("resource") or RESOURCE, **meta}


def client_id(meta: dict, override: str | None) -> str:
    """A public OAuth client for this tool: given, cached, or registered once."""
    if override:
        return override
    cached = _load(CLIENT_FILE) or {}
    if cached.get("issuer") == meta["issuer"] and cached.get("client_id"):
        return cached["client_id"]
    reg = meta.get("registration_endpoint")
    if not reg:
        _die("no cached client and the issuer offers no registration — pass --client-id")
    status, out = _post(reg, body={
        "client_name": "grp-login (Global Risk Platform CLI)",
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code",
                        "authorization_code", "refresh_token"],
        # Registration insists on a redirect and a response type; the device
        # flow never uses either. A loopback address is the conventional filler.
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    })
    if status >= 300 or not out.get("client_id"):
        _die(f"client registration failed ({status}): {out}")
    _save(CLIENT_FILE, {"issuer": meta["issuer"], "client_id": out["client_id"]})
    return out["client_id"]


# ------------------------------------------------------------- the flow

def start(args) -> None:
    meta = discover()
    cid = client_id(meta, args.client_id)
    status, out = _post(meta["device_authorization_endpoint"], form={
        "client_id": cid, "scope": SCOPE, "resource": meta["resource"]})
    if status >= 300 or not out.get("device_code"):
        _die(f"device authorization refused ({status}): {out}")
    _save(PENDING_FILE, {"issuer": meta["issuer"], "token_endpoint": meta["token_endpoint"],
                         "client_id": cid, "resource": meta["resource"],
                         "device_code": out["device_code"],
                         "interval": int(out.get("interval") or 5),
                         "expires_at": time.time() + int(out.get("expires_in") or 600)})
    url = out.get("verification_uri_complete") or out.get("verification_uri")
    print("Open this page and approve the login:\n")
    print(f"   {url}\n")
    mins = max(1, int(out.get("expires_in") or 600) // 60)
    print(f"   The code shown must be:  {out.get('user_code')}   (valid ~{mins} min)\n")
    print("Then run:  python3 grp-login.py finish")
    if args.open:
        webbrowser.open(url)


def finish(args) -> None:
    p = _load(PENDING_FILE)
    if not p:
        _die("nothing pending — run `start` first")
    if time.time() > p["expires_at"]:
        _die("that login code expired — run `start` again")
    interval = p["interval"]
    print("Waiting for the approval…", file=sys.stderr)
    while time.time() < p["expires_at"]:
        status, out = _post(p["token_endpoint"], form={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": p["device_code"], "client_id": p["client_id"],
            "resource": p["resource"]})
        if status < 300 and out.get("access_token"):
            _save(TOKEN_FILE, {"issuer": p["issuer"], "token_endpoint": p["token_endpoint"],
                               "client_id": p["client_id"], "resource": p["resource"],
                               "access_token": out["access_token"],
                               "refresh_token": out.get("refresh_token"),
                               "expires_at": time.time() + int(out.get("expires_in") or 3600)})
            try:
                os.remove(PENDING_FILE)
            except OSError:
                pass
            print(f"Logged in. Token saved to {TOKEN_FILE} (not printed).")
            print('Use it:  curl -H "Authorization: Bearer $(python3 grp-login.py token)" …')
            return
        err = out.get("error", "")
        if err == "authorization_pending":
            pass
        elif err == "slow_down":
            interval += 5
        elif err in ("access_denied", "expired_token"):
            _die(f"login {err.replace('_', ' ')}")
        else:
            _die(f"token endpoint said ({status}): {out}")
        time.sleep(interval)
    _die("timed out waiting for approval — run `start` again")


def token(args) -> None:
    t = _load(TOKEN_FILE)
    if not t:
        _die("not logged in — run `start`, approve, then `finish`")
    if time.time() > t["expires_at"] - 60:
        if not t.get("refresh_token"):
            _die("token expired and no refresh token — log in again")
        status, out = _post(t["token_endpoint"], form={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
            "client_id": t["client_id"], "resource": t["resource"]})
        if status >= 300 or not out.get("access_token"):
            _die(f"refresh failed ({status}): {out} — log in again")
        t.update({"access_token": out["access_token"],
                  "refresh_token": out.get("refresh_token") or t["refresh_token"],
                  "expires_at": time.time() + int(out.get("expires_in") or 3600)})
        _save(TOKEN_FILE, t)
    print(t["access_token"])


def check(args) -> None:
    """Does the platform accept this token? Asks about a contribution that cannot
    exist: an authenticated caller gets a polite decline, an unauthenticated one 401."""
    t = _load(TOKEN_FILE)
    if not t:
        _die("not logged in")
    req = urllib.request.Request(f"{PLATFORM}/api/contribute/0000000000000000",
                                 headers={"Authorization": f"Bearer {t['access_token']}",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
            print(f"accepted — the platform answered {r.status}: {body.get('note') or body}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _die("rejected (401) — the token is expired or not minted for this platform; "
                 "run `token` to refresh, or log in again")
        _die(f"unexpected {e.code}: {e.read()[:200]!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["start", "finish", "login", "token", "check"])
    ap.add_argument("--client-id", help="use a dashboard-issued client id instead of registering one")
    ap.add_argument("--open", action="store_true", help="open the approval page in a browser")
    args = ap.parse_args()
    if args.command == "login":
        start(args)
        finish(args)
    else:
        {"start": start, "finish": finish, "token": token, "check": check}[args.command](args)


if __name__ == "__main__":
    main()

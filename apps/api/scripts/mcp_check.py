"""Check a running server's HTTP surface, one line per assertion.

    uv run python scripts/mcp_check.py                       # http://127.0.0.1:8001
    uv run python scripts/mcp_check.py http://127.0.0.1:8001
    GRP_API_TOKEN=... uv run python scripts/mcp_check.py https://<service-url>

Companion to scripts/mcp_call.py, which drives the server over stdio. This one
exercises what only exists over HTTP: the token gate, the OAuth discovery documents
and the 401 that points a client at them. Those are headers and well-known paths, so
pytest cannot see most of it and a browser cannot POST to it.

A check tagged PENDING is a to-do; a check tagged FAIL is a regression. Exit status is 0 when nothing is FAIL.
"""

import json
import os
import sys

import requests

CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
TIMEOUT = 30

_results: list[bool] = []


def check(name: str, ok: bool, detail: str = "", pending: str = "") -> bool:
    """Record one assertion. `pending` says why it does not apply to this server as
    configured, which is not the same as the server being wrong."""
    if ok:
        status = "PASS"
    elif pending:
        status = "PEND"
    else:
        status = "FAIL"
    tail = f"   ({pending})" if (pending and not ok) else ""
    print(f"  {status}  {name:<52}{detail}{tail}")
    _results.append(ok or bool(pending))
    return ok


def post_mcp(base: str, headers: dict) -> requests.Response:
    return requests.post(f"{base}/mcp", headers={**HEADERS, **headers},
                         data=json.dumps(CALL), timeout=TIMEOUT)


def tool_count(response: requests.Response) -> int:
    """Tools in a tools/list reply, whether it came back as JSON or as one SSE frame."""
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        body = next((ln[5:] for ln in body.splitlines() if ln.startswith("data:")), "")
    try:
        return len(json.loads(body)["result"]["tools"])
    except (ValueError, KeyError, TypeError):
        return 0


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001").rstrip("/")
    token = os.environ.get("GRP_API_TOKEN", "").strip()
    print(f"\nservirplatform surface check   {base}\n")

    # --- the tools, and how they are gated -------------------------------------
    anon = post_mcp(base, {})
    gated = anon.status_code == 401
    check("anonymous POST /mcp is refused", gated, f"{anon.status_code}",
          pending="" if gated else "this server serves the tools unauthenticated")

    if gated:
        www = anon.headers.get("WWW-Authenticate", "")
        check("the 401 points at the discovery document",
              "resource_metadata=" in www, www[:60] or "no WWW-Authenticate header",
              pending="this server does not enforce OAuth")

    if token:
        ok = post_mcp(base, {"Authorization": f"Bearer {token}"})
        n = tool_count(ok)
        check("service token is accepted on /mcp", ok.ok and n > 0,
              f"{ok.status_code}, {n} tools")
        bad = post_mcp(base, {"Authorization": "Bearer not-the-token"})
        check("a wrong token is refused", bad.status_code == 401, f"{bad.status_code}")
    else:
        print("  ....  GRP_API_TOKEN unset, skipping the service-token checks")

    # --- OAuth discovery, which is what lets a client log a user in ------------
    # Only these two. The bare /.well-known/oauth-protected-resource is NOT served:
    # RFC 9728 puts the document at the well-known prefix plus the resource path, and
    # a spec-compliant client is pointed at it by the WWW-Authenticate header anyway.
    for path in ("/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-authorization-server"):
        r = requests.get(f"{base}{path}", timeout=TIMEOUT)
        detail = f"{r.status_code}"
        if r.ok:
            try:
                body = r.json()
                detail = (f"resource={body.get('resource')} "
                          f"as={body.get('authorization_servers') or body.get('issuer')}")
            except ValueError:
                detail = f"{r.status_code}, not JSON"
        check(f"GET {path}", r.ok, detail,
              pending="this server publishes no discovery documents")

    # --- what must stay reachable with no credential at all --------------------
    for path, why in (("/api/health", "liveness"),
                      ("/api/resolve/receipt/0000000000000000", "the receipt resolver")):
        r = requests.get(f"{base}{path}", timeout=TIMEOUT)
        # 404 is a PASS for the resolver: that id does not exist, but the route is open.
        check(f"public, no credential: {why}", r.status_code in (200, 404),
              f"{r.status_code}")

    passed = sum(_results)
    print(f"\n  {passed}/{len(_results)} checks passed or pending\n")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()

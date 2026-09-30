"""The contribution API — `contribute_submit` for a hub's OWN server.

A chat client is one way in; this is the other. A hub's application generates the
manifest and POSTs it here with the same AuthKit bearer token the MCP transport
accepts, from anywhere, with no VPN. It runs the same `staging.submit`, returns
the same JSON, and the contribution is attributed to the person whose token it is,
so reviewer rules and visibility apply unchanged. No service accounts: a machine
acting on its own is still a declared gap.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from ...config import get_settings
from ...contrib import identity, staging

router = APIRouter(prefix="/contribute")

# Same challenge the MCP transport sends, so a client that knows how to log in
# for /mcp knows how to log in here.
_CHALLENGE = 'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'


def _challenge() -> dict:
    base = (get_settings().grp_public_url or "").rstrip("/")
    return {"WWW-Authenticate": _CHALLENGE.format(base=base)}


async def _caller(request: Request) -> identity.Caller:
    """Who is contributing. A verified bearer token, or on a dev box the local
    operator / dev header. Never anonymous: an unattributed contribution has no
    owner, no reviewer boundary and nothing to withdraw."""
    settings = get_settings()
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        token = raw[7:].strip()
        from ...mcp import auth as mcp_auth
        prov = mcp_auth.provider()
        if prov is None:
            # OAuth off: a bearer means nothing here; fall through to the dev box.
            pass
        else:
            access = await prov.verify_token(token) if token else None
            if access is None:
                raise HTTPException(status_code=401, detail="invalid or expired token",
                                    headers=_challenge())
            claims = getattr(access, "claims", None) or {}
            sub = claims.get("sub") or getattr(access, "subject", None) \
                or getattr(access, "client_id", None)
            if not sub:
                raise HTTPException(status_code=401, detail="token carries no subject",
                                    headers=_challenge())
            cid, label = str(sub), str(claims.get("email") or sub)
            return identity.Caller(cid, label, identity._is_reviewer(cid, label, "token"),
                                   "token")
    if settings.grp_oauth_enabled or settings.grp_allow_anonymous:
        raise HTTPException(status_code=401, detail="a bearer token is required",
                            headers=_challenge())
    # Dev box (OAuth off, not open): the local operator, or the dev header.
    dev = request.headers.get(identity.DEV_HEADER, "").strip()
    cid = label = dev or identity.LOCAL_DEV
    source = "dev-header" if dev else "local-dev"
    return identity.Caller(cid, label, identity._is_reviewer(cid, label, source), source)


async def _manifest_text(request: Request, file: UploadFile | None) -> str:
    """The manifest as text: a multipart `file`, or the request body (YAML or JSON)."""
    if file is not None:
        raw = await file.read()
    else:
        raw = await request.body()
    cap = int(getattr(get_settings(), "grp_max_fetch_bytes", 0) or 0) or 5 * 1024 * 1024
    if len(raw) > cap:
        raise HTTPException(status_code=413, detail=f"manifest larger than {cap} bytes")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="manifest must be UTF-8 text (YAML or JSON)")


@router.post("")
async def submit(request: Request, kind: str = Query("", description="optional; else the "
                                                     "manifest's top-level `kind`"),
                 file: UploadFile | None = File(None)):
    """Submit a contribution. Body: the manifest as YAML or JSON with a top-level
    `kind`, or a multipart `file` holding it. Returns exactly what the MCP tool
    `contribute_submit` returns; a decline names every problem."""
    caller = await _caller(request)
    text = await _manifest_text(request, file)
    if not text.strip():
        raise HTTPException(status_code=400, detail="empty manifest")
    return staging.submit(kind, text, caller=caller)


@router.get("/{contribution_id}")
async def status(contribution_id: str, request: Request):
    """The state of one contribution you can see — for a server that polls."""
    caller = await _caller(request)
    return staging.status(contribution_id, caller=caller)

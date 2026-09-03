"""OAuth 2.1 against a managed identity provider (WorkOS AuthKit).

This server is a RESOURCE SERVER and nothing more. It never sees a password, never
stores a user, never issues a token. It publishes where to log in, and (once
enforcement is on) verifies the signature of the token it is handed against the
provider's public keys. That is why no WorkOS API key or client secret belongs in
this environment: there is nothing here to authenticate AS.

Configuration is read from the environment rather than from Settings, matching the
other serving-layer switches (GRP_API_TOKEN, GRP_MCP_ALLOWED_HOSTS) which are set per
deployment rather than per developer:

    GRP_OAUTH_ENABLED=1                              the master switch
    GRP_PUBLIC_URL=https://<host>                     the origin clients reach
    GRP_AUTHKIT_DOMAIN=https://<x>.authkit.app        the AuthKit instance
    GRP_OAUTH_REQUIRED_SCOPES=a,b                     optional, empty means none

Unset GRP_OAUTH_ENABLED and every function here returns nothing, so the server
behaves exactly as it did before any of this existed.
"""

from __future__ import annotations

import os

from starlette.routing import Route
from ..config import Settings, get_settings

# The path the MCP transport is mounted at. The discovery document has to name the
# endpoint exactly, path included, so this is not decoration: it becomes the
# `resource` identifier and the audience AuthKit stamps into the token.
MCP_PATH = "/mcp"


def enabled(settings: Settings) -> bool:
    return settings.grp_oauth_enabled.strip() not in ("", "0", "false")


def provider():
    """The AuthKit provider, or None when OAuth is switched off.

    Fails closed rather than half-configured: a server that says it does OAuth but
    cannot name its identity provider would publish discovery documents pointing
    nowhere, and every client would fail at the login step with nothing in the logs
    to explain it.
    """
    settings = get_settings()
    if not enabled(settings):
        return None
    domain = settings.grp_authkit_domain.strip()
    public_url = settings.grp_public_url.strip()
    missing = [name for name, value in
               (("GRP_AUTHKIT_DOMAIN", domain), ("GRP_PUBLIC_URL", public_url))
               if not value]
    if missing:
        raise RuntimeError(
            f"GRP_OAUTH_ENABLED is set but {' and '.join(missing)} is not. "
            "Set it, or unset GRP_OAUTH_ENABLED to serve without OAuth.")

    # Imported here so a server with OAuth off never pays for it.
    from fastmcp.server.auth.providers.workos import AuthKitProvider

    scopes = [s.strip() for s in
              os.environ.get("GRP_OAUTH_REQUIRED_SCOPES", "").split(",") if s.strip()]
    # base_url is the ORIGIN; the mount path is passed separately wherever it is
    # needed, so the provider derives resource = <origin><MCP_PATH> itself.
    return AuthKitProvider(authkit_domain=domain, base_url=public_url,
                           required_scopes=scopes or None)


def well_known_routes() -> list[Route]:
    """The discovery documents, to be served from the ORIGIN ROOT.

    RFC 9728 puts protected-resource metadata at
    /.well-known/oauth-protected-resource<resource path>, at the root of the origin.
    The MCP app is mounted under /mcp, so routes carried by that app would land at
    /mcp/.well-known/..., where no client looks. These therefore ride on the outer
    FastAPI app instead.

    Two documents come back:
      /.well-known/oauth-protected-resource/mcp   who we are and who issues our tokens
      /.well-known/oauth-authorization-server     AuthKit's own metadata, forwarded,
                                                  for clients that ask us rather than it
    """
    p = provider()
    return list(p.get_well_known_routes(mcp_path=MCP_PATH)) if p else []

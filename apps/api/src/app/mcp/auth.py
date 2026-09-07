"""OAuth 2.1 against a managed identity provider (WorkOS AuthKit).

This server is a RESOURCE SERVER and nothing more. It never sees a password, never
stores a user, never issues a token. It publishes where to log in, and verifies the
signature of the token it is handed against the provider's public keys. That is why
no WorkOS API key or client secret belongs in this environment: there is nothing
here to authenticate AS.

    GRP_OAUTH_ENABLED=1                          the master switch
    GRP_PUBLIC_URL=https://<host>                the origin clients reach
    GRP_AUTHKIT_DOMAIN=https://<x>.authkit.app   the AuthKit instance

With the switch off, provider() returns None and the server behaves exactly as it
did before any of this existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import Route

from ..config import get_settings

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import RemoteAuthProvider

# Where the MCP transport answers. The discovery document has to name the endpoint
# exactly, path included, so this is not decoration: it becomes the `resource`
# identifier and the audience AuthKit stamps into the token.
MCP_PATH = "/mcp"


def provider():
    """The AuthKit provider, or None when OAuth is switched off.

    Fails closed rather than half-configured: a server that says it does OAuth but
    cannot name its identity provider would publish discovery documents pointing
    nowhere, and every client would fail at the login step with nothing in the logs
    to explain it.
    """
    settings = get_settings()
    if not settings.grp_oauth_enabled:
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

    # Imported here, and the subclass defined here with it, so a server with OAuth off
    # never pays the import.
    from fastmcp.server.auth.providers.workos import AuthKitProvider

    class ResourceServer(AuthKitProvider):
        """AuthKit, told where this server actually answers and what to call AuthKit.

        Both corrections belong to the PROVIDER rather than to one call site, because
        two entry points build a transport from it and each would otherwise need its
        own copy.
        """

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            # Held as the plain string. AnyHttpUrl stringifies a pathless URL with a
            # trailing slash, and the client compares this against the authorization
            # server's published `issuer` with `!=` (RFC 8414); AuthKit publishes it
            # without one.
            self.authorization_servers = [self.authkit_domain]

        def _get_resource_url(self, path: str | None = None):
            # The transport is built with path="/" and mounted at MCP_PATH, so it
            # works out its own address as the bare origin and cannot see the prefix.
            # MCP_PATH is the public address either way, so the caller's idea of the
            # path is ignored. This one value becomes the token audience, the
            # `resource` in the discovery document, and the URL inside every 401.
            return super()._get_resource_url(MCP_PATH)

    return ResourceServer(authkit_domain=domain, base_url=public_url)


def well_known_routes(provider: RemoteAuthProvider | None) -> list[Route]:
    """The discovery documents, to be served from the ORIGIN ROOT.

    RFC 9728 puts protected-resource metadata at
    /.well-known/oauth-protected-resource<resource path>, at the root of the origin.
    The MCP app is mounted under /mcp, so routes carried by that app would land at
    /mcp/.well-known/..., where no client looks. These ride on the outer app instead.

    Two documents come back: who we are and who issues our tokens, and AuthKit's own
    metadata forwarded verbatim for clients that ask us rather than it.
    """
    if provider is None:
        return []
    return list(provider.get_well_known_routes(mcp_path=MCP_PATH))

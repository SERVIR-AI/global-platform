"""OAuth 2.1 resource server for the MCP transport (WorkOS AuthKit).

Publishes discovery and verifies the tokens clients bring against the
provider's public keys; it never issues tokens or stores users. Config lives in
config.py: GRP_OAUTH_ENABLED, GRP_AUTHKIT_DOMAIN, GRP_PUBLIC_URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import Route

from ..config import get_settings

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import RemoteAuthProvider

# Where the transport is mounted. The discovery document names it exactly, path
# included: it is the resource identifier and the audience AuthKit stamps into
# tokens.
MCP_PATH = "/mcp"


def provider():
    """The AuthKit provider, or None when OAuth is off.

    Raises when the switch is on but a required setting is missing: a provider
    without a domain or public URL would publish unusable discovery.
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

    # Imported here, and the subclass defined here with it, so a server with
    # OAuth off never pays the import.
    from fastmcp.server.auth.providers.workos import AuthKitProvider

    class ResourceServer(AuthKitProvider):
        """AuthKit provider pinned to where this server actually answers.

        The corrections live on the provider rather than at a call site because
        two entry points build a transport from it.
        """

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            # The authorization server URL must match AuthKit's published issuer
            # exactly: AnyHttpUrl stringifies a pathless URL with a trailing
            # slash, and the client compares the two with `!=`.
            self.authorization_servers = [self.authkit_domain]

        def _get_resource_url(self, path: str | None = None):
            # The transport is built at path="/" and mounted at MCP_PATH, so it
            # would name the bare origin; MCP_PATH is the public address. This
            # value becomes the token audience, the discovery document's
            # `resource`, and the URL in every 401.
            return super()._get_resource_url(MCP_PATH)

    return ResourceServer(authkit_domain=domain, base_url=public_url)


def well_known_routes(provider: RemoteAuthProvider | None) -> list[Route]:
    """Discovery documents served from the origin root.

    RFC 9728 puts protected-resource metadata at
    /.well-known/oauth-protected-resource<path>, at the origin root; routes on the
    /mcp mount would land at /mcp/.well-known/... where no client looks. Returns
    the server's own document and AuthKit's metadata for clients that ask us for
    it.
    """
    if provider is None:
        return []
    return list(provider.get_well_known_routes(mcp_path=MCP_PATH))

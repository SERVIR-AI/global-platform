from fastapi import APIRouter

from ... import __version__, authz
from ...config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness plus what is running: the app name, the default provider, the version,
    and the commit and image digest it was built and deployed from (empty on a checkout),
    so a deploy is confirmed over HTTPS rather than by reading the host. `authorization`
    says whether the store can answer (off | ok | unreachable | store-missing), probed
    without creating anything: with the store down every check fails closed, which from
    the outside looks like a platform that lost its contributed layers."""
    settings = get_settings()
    store = authz.health()
    return {
        "status": "ok",
        "app": settings.app_name,
        "default_provider": settings.default_provider,
        "version": __version__,
        "commit": settings.grp_build_commit or None,
        "image_digest": settings.grp_image_digest or None,
        # the public page says whether the store answers, not where it is
        "authorization": {"store": store["store"], "reason": store["reason"]},
    }

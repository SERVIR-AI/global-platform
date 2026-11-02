from fastapi import APIRouter

from ... import __version__
from ...config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness plus what is running: the app name, the default provider, the version,
    and the commit and image digest it was built and deployed from (empty on a checkout),
    so a deploy is confirmed over HTTPS rather than by reading the host."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "default_provider": settings.default_provider,
        "version": __version__,
        "commit": settings.grp_build_commit or None,
        "image_digest": settings.grp_image_digest or None,
    }

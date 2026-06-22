from fastapi import APIRouter

from ...config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "default_provider": settings.default_provider,
    }

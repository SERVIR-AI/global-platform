from fastapi import APIRouter

from .chat import router as chat_router
from .health import router as health_router
from .raster import router as raster_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(chat_router, tags=["chat"])
api_router.include_router(raster_router, tags=["raster"])

__all__ = ["api_router"]

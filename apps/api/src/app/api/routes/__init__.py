from fastapi import APIRouter

from ...food_security.routes import router as food_security_router
from .chat import router as chat_router
from .health import router as health_router
from .raster import router as raster_router
from .receipts import router as receipts_router
from .tiffs_upload import router as tiffs_upload_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(chat_router, tags=["chat"])
api_router.include_router(raster_router, tags=["raster"])
api_router.include_router(tiffs_upload_router, tags=["byod"])
api_router.include_router(food_security_router, tags=["food-security"])
api_router.include_router(receipts_router, tags=["resolve"])

__all__ = ["api_router"]

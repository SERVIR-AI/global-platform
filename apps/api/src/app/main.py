"""FastAPI application entrypoint.

Run locally:
    uv run uvicorn app.main:app --reload --app-dir apps/api/src
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import api_router
from .config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance.

    Sets up CORS middleware with origins from settings and mounts the API router
    at /api. Called once at module load to produce the `app` singleton.

    Returns:
        FastAPI: configured application instance
    """
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")

    @app.get("/")
    def root() -> dict:
        """Return service name and link to the interactive API docs.

        Returns:
            dict: {'service': str, 'docs': str}
        """
        return {"service": settings.app_name, "docs": "/docs"}

    return app


app = create_app()

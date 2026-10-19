"""FastAPI app. Run with:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app import api, lab, seed
from app.config import Settings, load_settings
from app.db import SessionLocal, ensure_schema, init_engine
from app.models import Student

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_engine(settings.database_url)
        ensure_schema()  # rebuilds an out-of-date demo database
        with SessionLocal() as session:
            if session.scalar(select(Student.id).limit(1)) is None:
                seed.seed(session)
                session.commit()
        yield

    app = FastAPI(
        title="Bundle API",
        description="A student group-buying agent. No real money moves: payments are simulated.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(api.router)
    app.include_router(lab.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()

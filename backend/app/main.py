from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import artifacts, health, images, jobs, settings_router, system, tts

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    settings.ensure_dirs()
    init_db()


app.include_router(health.router)
app.include_router(settings_router.router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(images.router)
app.include_router(tts.router)
app.include_router(artifacts.router)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging

load_dotenv()

from routes import auth, agents, admin, spend, analytics, branches, cities, reports, quality

app = FastAPI(title="Marzouk Academy — Ad Quality Tracker")

# CORS — restrict to same origin in production; Railway serves frontend + API on same domain
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)

app.include_router(auth.router,      prefix="/auth",      tags=["auth"])
app.include_router(agents.router,    prefix="/agents",    tags=["agents"])
app.include_router(admin.router,     prefix="/admin",     tags=["admin"])
app.include_router(spend.router,     prefix="/spend",     tags=["spend"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(branches.router,  prefix="/branches",  tags=["branches"])
app.include_router(cities.router,    prefix="/cities",    tags=["cities"])
app.include_router(reports.router,   prefix="/reports",   tags=["reports"])
app.include_router(quality.router,   prefix="/quality",   tags=["quality"])

@app.get("/health")
def health():
    return {"status": "ok"}

# Serve frontend static files with no-cache headers
from fastapi import Request
from fastapi.responses import Response
import mimetypes

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path == "/" or "." not in path.split("/")[-1]:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging

load_dotenv()

from routes import auth, agents, admin, spend, analytics, branches, cities, reports, quality, ad_leads

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
app.include_router(ad_leads.router,  prefix="/ad-leads",  tags=["ad-leads"])

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Background scheduler — pulls Kenitra lead-ads sheet every 5 minutes
# ---------------------------------------------------------------------------

SYNC_INTERVAL_MIN = int(os.getenv("AD_LEADS_SYNC_INTERVAL_MIN", "5"))
SYNC_ENABLED = os.getenv("AD_LEADS_SYNC_ENABLED", "true").lower() in ("1", "true", "yes")


@app.on_event("startup")
def start_sync_scheduler():
    if not SYNC_ENABLED:
        logging.info("[ad_leads] scheduler disabled via AD_LEADS_SYNC_ENABLED=false")
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from services.ad_leads_sync import sync_leads_from_sheet
        from datetime import datetime, timezone, timedelta

        def run_sync():
            try:
                r = sync_leads_from_sheet()
                logging.info(f"[ad_leads] sync: {r}")
            except Exception as e:
                logging.warning(f"[ad_leads] sync failed: {e}")

        scheduler = AsyncIOScheduler(timezone="UTC")
        # First run 30s after boot (so the app is ready), then every N minutes.
        first = datetime.now(timezone.utc) + timedelta(seconds=30)
        scheduler.add_job(
            run_sync, "interval",
            minutes=SYNC_INTERVAL_MIN,
            id="ad_leads_sync",
            next_run_time=first,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        logging.info(f"[ad_leads] scheduler started — first run at {first.isoformat()}, then every {SYNC_INTERVAL_MIN} min")
    except Exception as e:
        logging.warning(f"[ad_leads] could not start scheduler: {e}")

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

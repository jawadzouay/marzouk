from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging
import threading

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


app.state.last_sync_result = None
app.state.last_sync_at = None


def _bootstrap_scheduler():
    """Heavy imports + scheduler start. Runs in a detached daemon thread so
    uvicorn's startup event returns immediately and Railway's /health probe
    can succeed before we've finished wiring APScheduler and Supabase."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from services.ad_leads_sync import sync_leads_from_sheet
        from datetime import datetime, timezone, timedelta

        def run_sync():
            try:
                r = sync_leads_from_sheet()
                app.state.last_sync_result = r
                app.state.last_sync_at = datetime.now(timezone.utc).isoformat()
                logging.info(f"[ad_leads] sync: {r}")
            except Exception as e:
                app.state.last_sync_result = {"ok": False, "error": str(e)}
                app.state.last_sync_at = datetime.now(timezone.utc).isoformat()
                logging.warning(f"[ad_leads] sync failed: {e}")

        scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
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
        logging.info(f"[ad_leads] BackgroundScheduler started — first run at {first.isoformat()}, then every {SYNC_INTERVAL_MIN} min")
    except Exception as e:
        logging.warning(f"[ad_leads] could not start scheduler: {e}")


@app.on_event("startup")
def start_sync_scheduler():
    if not SYNC_ENABLED:
        logging.info("[ad_leads] scheduler disabled via AD_LEADS_SYNC_ENABLED=false")
        return
    # Kick bootstrap into a detached thread so this handler returns in <1ms.
    # Otherwise Railway's healthcheck races the scheduler's cold-import (which
    # pulls in apscheduler + supabase + googleapiclient) and we time out before
    # uvicorn can answer /health.
    threading.Thread(target=_bootstrap_scheduler, daemon=True, name="scheduler-bootstrap").start()
    logging.info("[ad_leads] scheduler bootstrap dispatched to background thread")


@app.on_event("shutdown")
def stop_sync_scheduler():
    sched = getattr(app.state, "scheduler", None)
    if sched:
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass


@app.get("/admin/scheduler-status")
def scheduler_status():
    """Diagnostic — returns whether the scheduler is running and when it
    last fired. Not auth-protected because it only exposes timing metadata."""
    sched = getattr(app.state, "scheduler", None)
    jobs = []
    if sched:
        for job in sched.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
    return {
        "scheduler_running": bool(sched and sched.running),
        "sync_enabled": SYNC_ENABLED,
        "interval_min": SYNC_INTERVAL_MIN,
        "jobs": jobs,
        "last_sync_at": getattr(app.state, "last_sync_at", None),
        "last_sync_result": getattr(app.state, "last_sync_result", None),
    }

# Serve frontend static files with no-cache headers
from fastapi import Request
from fastapi.responses import Response
import mimetypes

import pathlib
FRONTEND_DIR = str((pathlib.Path(__file__).resolve().parent.parent / "frontend"))

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

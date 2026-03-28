from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging

load_dotenv()

from routes import auth, agents, leads, rdv, swap, admin, blacklist, spend, analytics, branches, cities

app = FastAPI(title="Marzouk Academy CRM")

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

# ── Background scheduler ────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)

def run_auto_swap():
    """Auto-assign all swap-eligible leads every 6 hours."""
    try:
        from services.swap_service import get_eligible_leads_for_swap, assign_swap
        eligible = get_eligible_leads_for_swap()
        if eligible:
            logging.info(f"[AUTO-SWAP] {len(eligible)} leads eligible — running swap")
            for lead in eligible:
                try:
                    assign_swap(lead)
                except Exception as e:
                    logging.warning(f"[AUTO-SWAP] lead {lead['id']}: {e}")
    except Exception as e:
        logging.error(f"[AUTO-SWAP ERROR] {e}")

def run_noshow_repool():
    """Move no-show leads back to swap pool when their 10-day timer expires."""
    try:
        from services.supabase_service import get_client
        from datetime import datetime, timedelta
        sb = get_client()
        now = datetime.utcnow().isoformat()
        leads = sb.table("leads").select("id, original_agent") \
            .eq("status", "N.R") \
            .lte("swap_eligible_at", now) \
            .lt("swap_count", 3) \
            .execute().data
        if leads:
            logging.info(f"[REPOOL] {len(leads)} no-show leads re-entering swap pool")
    except Exception as e:
        logging.error(f"[REPOOL ERROR] {e}")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(run_auto_swap,    "interval", hours=6, id="auto_swap",    replace_existing=True)
    scheduler.add_job(run_noshow_repool,"interval", hours=1, id="noshow_repool",replace_existing=True)
    scheduler.start()
    logging.info("[SCHEDULER] Auto-swap (6h) and no-show repool (1h) jobs started")
except ImportError:
    logging.warning("[SCHEDULER] apscheduler not installed — auto-swap disabled. Add apscheduler to requirements.txt")

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(agents.router, prefix="/agents", tags=["agents"])
app.include_router(leads.router, prefix="/leads", tags=["leads"])
app.include_router(rdv.router, prefix="/rdv", tags=["rdv"])
app.include_router(swap.router, prefix="/swap", tags=["swap"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(blacklist.router, prefix="/blacklist", tags=["blacklist"])
app.include_router(spend.router,     prefix="/spend",     tags=["spend"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(branches.router, prefix="/branches", tags=["branches"])
app.include_router(cities.router,   prefix="/cities",   tags=["cities"])

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

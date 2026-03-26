from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os

load_dotenv()

from routes import auth, agents, leads, rdv, swap, admin, blacklist, spend, analytics, branches

app = FastAPI(title="Marzouk Academy CRM")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug-env")
def debug_env():
    pin = (os.getenv("ADMIN_PIN") or "").strip()
    return {
        "admin_pin_set": bool(pin),
        "admin_pin_length": len(pin),
        "admin_pin_value": pin,
        "all_env_keys": sorted(os.environ.keys())
    }

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

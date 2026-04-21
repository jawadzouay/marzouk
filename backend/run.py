import os
import sys
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # Log to stdout so Railway's deploy logs show exactly what we're binding
    # to — previous "Application failed to respond" was silent about this.
    print(f"[boot] starting uvicorn on 0.0.0.0:{port} (PORT env = {os.environ.get('PORT')!r})", flush=True)
    sys.stdout.flush()
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")

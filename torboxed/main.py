from __future__ import annotations

import json
import os
import re
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from torboxed.config import settings
from torboxed.db import Download, KVSetting, SessionLocal, init_db
from torboxed.downloader import worker


app = FastAPI(title="Torboxed")
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def _startup() -> None:
    os.makedirs(settings.download_dir, exist_ok=True)
    os.makedirs(os.path.join(settings.data_dir, "uploads"), exist_ok=True)
    init_db()
    worker.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await worker.stop()


def _set_setting(db, key: str, value: str) -> None:  # noqa: ANN001
    row = db.get(KVSetting, key)
    if row:
        row.value = value
    else:
        row = KVSetting(key=key, value=value)
    db.add(row)
    db.commit()


def _get_setting(db, key: str) -> str | None:  # noqa: ANN001
    row = db.get(KVSetting, key)
    return row.value if row else None


@app.get("/", response_class=HTMLResponse)
async def ui_root(request: Request) -> Any:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/downloads")
async def list_downloads() -> dict[str, Any]:
    with SessionLocal() as db:
        items = db.query(Download).order_by(Download.id.desc()).all()
        return {
            "items": [
                {
                    "id": d.id,
                    "created_at": d.created_at.isoformat(),
                    "updated_at": d.updated_at.isoformat(),
                    "filename": d.filename,
                    "source_type": d.source_type,
                    "category": d.category,
                    "status": d.status,
                    "progress": d.progress,
                    "current_speed_bps": d.current_speed_bps,
                    "error": d.error,
                    "local_path": d.local_path,
                }
                for d in items
            ]
        }


@app.post("/api/downloads/upload")
async def upload_download(
    source_type: str = Form(...),  # "torrent" | "nzb"
    file: UploadFile = File(...),
    category: str = Form(None),  # Optional: "radarr" | "sonarr" | "whisparr"
) -> dict[str, Any]:
    if source_type not in ("torrent", "nzb"):
        raise HTTPException(status_code=400, detail="source_type must be torrent or nzb")
    
    # Validate category if provided
    if category and category.lower() not in ("radarr", "sonarr", "whisparr"):
        raise HTTPException(status_code=400, detail="category must be radarr, sonarr, or whisparr")
    
    category = category.lower() if category else None

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # Sanitize filename so it can't escape the uploads dir
    raw_name = file.filename or "upload.bin"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(raw_name)) or "upload.bin"

    with SessionLocal() as db:
        d = Download(filename=file.filename or "upload.bin", source_type=source_type, category=category, status="queued", progress=0)
        db.add(d)
        db.commit()
        db.refresh(d)

        download_id = d.id
        upload_path = os.path.join(settings.data_dir, "uploads", f"{download_id}_{safe_name}")
        with open(upload_path, "wb") as f:
            f.write(content)
        _set_setting(db, f"upload_path:{download_id}", upload_path)

    return {"id": download_id}


@app.post("/api/downloads/{download_id}/cancel")
async def cancel_download(download_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        d = db.get(Download, download_id)
        if not d:
            raise HTTPException(status_code=404, detail="Not found")
        if d.status in ("completed", "failed"):
            return {"ok": True}
        d.status = "cancelled"
        db.add(d)
        db.commit()
    return {"ok": True}


@app.delete("/api/downloads/{download_id}")
async def delete_download(download_id: int) -> dict[str, Any]:
    """
    Deletes the download row and best-effort removes associated local/uploaded files.
    """
    with SessionLocal() as db:
        d = db.get(Download, download_id)
        if not d:
            raise HTTPException(status_code=404, detail="Not found")

        upload_key = f"upload_path:{download_id}"
        upload_path = _get_setting(db, upload_key)
        source_key = f"source_path:{download_id}"
        source_path = _get_setting(db, source_key)
        local_path = d.local_path

        # remove DB rows first
        for k in (upload_key, source_key):
            row = db.get(KVSetting, k)
            if row:
                db.delete(row)
        db.delete(d)
        db.commit()

    # best-effort: remove files
    for p in [upload_path, source_path, local_path]:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    return {"ok": True}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    keys = [
        "torbox_base_url",
        "torbox_api_key",
        "torbox_rate_limit_per_minute",
        "max_concurrent_local_downloads",
        "download_folder",
        "delete_on_complete_provider",
        "blackhole_enabled",
        "blackhole_path",
        "sonarr_url",
        "sonarr_api_key",
        "radarr_url",
        "radarr_api_key",
        "whisparr_url",
        "whisparr_api_key",
    ]
    with SessionLocal() as db:
        out: dict[str, Any] = {}
        for k in keys:
            out[k] = _get_setting(db, k)
        return out


@app.put("/api/settings")
async def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal() as db:
        for k, v in payload.items():
            if v is None:
                continue
            if k in ("torbox_rate_limit_per_minute", "max_concurrent_local_downloads"):
                try:
                    v = str(int(v))
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"{k} must be integer") from e
            else:
                v = str(v)
            _set_setting(db, k, v)

    # NOTE: changing concurrency/rate limit at runtime isn't applied to the already-running worker instance yet.
    # We'll apply it by rebuilding the worker configuration in a follow-up iteration if needed.
    return {"ok": True}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "worker_running": worker.state.running}


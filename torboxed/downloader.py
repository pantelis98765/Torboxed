from __future__ import annotations

import asyncio
import os
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
import time

import httpx
from aiolimiter import AsyncLimiter

from torboxed.config import settings
from torboxed.arr_clients import radarr_scan, sonarr_scan, whisparr_scan
from torboxed.db import Download, SessionLocal
from torboxed.torbox_client import TorboxClient, TorboxError

logger = logging.getLogger("torboxed.worker")


@dataclass
class WorkerState:
    running: bool = False


class DownloadWorker:
    def __init__(self) -> None:
        self.state = WorkerState(running=False)
        self._task: asyncio.Task | None = None

        # Rate limit: Torbox requests (submit/poll/etc)
        self._torbox_limiter = AsyncLimiter(settings.torbox_rate_limit_per_minute, 60)

        # Concurrency: local downloads
        self._local_dl_sem = asyncio.Semaphore(max(1, settings.max_concurrent_local_downloads))

        self._http = httpx.AsyncClient(timeout=300)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.state.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.state.running = False
        if self._task:
            await asyncio.wait([self._task], timeout=5)
        await self._http.aclose()

    async def _run_loop(self) -> None:
        while self.state.running:
            # Periodically scan blackhole directory for new files to import
            await self._scan_blackhole()
            await self._process_one()
            await asyncio.sleep(0.5)

    async def _process_one(self) -> None:
        # Find next queued item
        with SessionLocal() as db:
            item: Download | None = (
                db.query(Download).filter(Download.status.in_(["queued", "submitted"])).order_by(Download.id).first()
            )
            if not item:
                return

            # move "queued" -> "submitting"
            if item.status == "queued":
                item.status = "submitting"
                db.add(item)
                db.commit()
                db.refresh(item)

        # Work outside db session
        try:
            await self._ensure_submitted(item.id)
            await self._ensure_downloaded(item.id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Worker failed processing download_id=%s", item.id)
            with SessionLocal() as db:
                it = db.get(Download, item.id)
                if it and it.status not in ("completed", "cancelled"):
                    it.status = "failed"
                    it.error = f"{type(e).__name__}: {e}"
                    db.add(it)
                    db.commit()

    async def _ensure_submitted(self, download_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(Download, download_id)
            if not item or item.status in ("completed", "cancelled"):
                return
            if item.torbox_ref:
                # already submitted
                if item.status == "submitting":
                    item.status = "submitted"
                    db.add(item)
                    db.commit()
                return

            api_key = self._get_setting(db, "torbox_api_key") or settings.torbox_api_key
            base_url = self._get_setting(db, "torbox_base_url") or settings.torbox_base_url
            if not api_key:
                raise RuntimeError("Torbox API key not configured")

            upload_path = self._get_setting(db, f"upload_path:{item.id}")
            if not upload_path or not os.path.exists(upload_path):
                raise RuntimeError("Upload content missing on server")

            with open(upload_path, "rb") as f:
                content = f.read()

        client = TorboxClient(base_url=base_url, api_key=api_key, http=self._http)
        async with self._torbox_limiter:
            try:
                res = await client.submit_file(
                    filename=item.filename, content=content, source_type=item.source_type
                )
            except TorboxError as e:
                raise RuntimeError(str(e)) from e

        with SessionLocal() as db:
            it = db.get(Download, download_id)
            if not it or it.status in ("completed", "cancelled"):
                return
            it.torbox_ref = res.torrent_id
            it.status = "submitted"
            it.progress = max(it.progress, 5)
            db.add(it)
            db.commit()

    async def _ensure_downloaded(self, download_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(Download, download_id)
            if not item or item.status in ("completed", "cancelled"):
                return
            if item.torbox_download_url and item.local_path and os.path.exists(item.local_path):
                item.status = "completed"
                item.progress = 100
                db.add(item)
                db.commit()
                return

            api_key = self._get_setting(db, "torbox_api_key") or settings.torbox_api_key
            base_url = self._get_setting(db, "torbox_base_url") or settings.torbox_base_url
            if not api_key:
                raise RuntimeError("Torbox API key not configured")
            if not item.torbox_ref:
                return

        client = TorboxClient(base_url=base_url, api_key=api_key, http=self._http)

        # Poll until ready (lightweight, still rate-limited)
        download_url: str | None = None
        for _ in range(60):  # ~60 polls max (adjust later)
            # Stop early if user cancelled
            with SessionLocal() as db:
                it = db.get(Download, download_id)
                if not it or it.status == "cancelled":
                    return
            async with self._torbox_limiter:
                st = await client.get_status(reference_id=item.torbox_ref, kind=item.source_type)
            if st.is_ready and st.download_url:
                download_url = st.download_url
                break
            if st.progress is not None:
                with SessionLocal() as db:
                    it = db.get(Download, download_id)
                    if it and it.status in ("submitted", "downloading"):
                        it.progress = min(95, max(it.progress, int(st.progress)))
                        db.add(it)
                        db.commit()
            await asyncio.sleep(2)

        if not download_url:
            # keep it in submitted state; worker will revisit
            return

        with SessionLocal() as db:
            it = db.get(Download, download_id)
            if not it or it.status in ("completed", "cancelled"):
                return
            it.torbox_download_url = download_url
            it.status = "downloading"
            it.progress = max(it.progress, 10)
            db.add(it)
            db.commit()

        # Local download with concurrency control
        async with self._local_dl_sem:
            # Get download folder from settings (user configurable)
            with SessionLocal() as db:
                download_folder = self._get_setting(db, "download_folder") or settings.download_dir
            out_dir = Path(download_folder)
            
            # If category is set, create category subfolder (e.g., Downloads/radarr, Downloads/sonarr)
            if item.category:
                out_dir = out_dir / item.category.lower()
            
            out_dir.mkdir(parents=True, exist_ok=True)
            # Get actual filename from download (will be determined during download)
            actual_filename = await self._get_download_filename(download_url, item.filename)
            out_path = out_dir / f"{download_id}_{actual_filename}"
            # _download_stream may update out_path if it finds a better filename in headers
            final_path = await self._download_stream(download_url, out_path, download_id)

        with SessionLocal() as db:
            it = db.get(Download, download_id)
            if not it or it.status in ("completed", "cancelled"):
                return
            it.local_path = str(final_path)
            it.status = "completed"
            it.progress = 100
            db.add(it)
            db.commit()

        # Best-effort: notify Arr apps to import from the downloaded file path
        await self._notify_arrs(str(final_path))

        # Best-effort: cleanup upload/blackhole files and provider job
        await self._cleanup_after_complete(download_id)

    async def _cleanup_after_complete(self, download_id: int) -> None:
        from torboxed.db import KVSetting

        with SessionLocal() as db:
            d = db.get(Download, download_id)
            if not d:
                return
            delete_provider = (self._get_setting(db, "delete_on_complete_provider") or "").lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            api_key = self._get_setting(db, "torbox_api_key") or settings.torbox_api_key
            base_url = self._get_setting(db, "torbox_base_url") or settings.torbox_base_url

            upload_key = f"upload_path:{download_id}"
            upload_path = self._get_setting(db, upload_key)
            source_key = f"source_path:{download_id}"
            source_path = self._get_setting(db, source_key)

            # remove KV settings so we don't leak DB rows
            for k in (upload_key, source_key):
                row = db.get(KVSetting, k)
                if row:
                    db.delete(row)
            db.commit()

        # delete the original nzb/torrent files stored on disk
        for p in (upload_path, source_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        if delete_provider and api_key and d.torbox_ref:
            client = TorboxClient(base_url=base_url, api_key=api_key, http=self._http)
            try:
                async with self._torbox_limiter:
                    if d.source_type == "torrent":
                        await client.control_torrent({"action": "delete", "torrent_id": d.torbox_ref})
                    elif d.source_type == "nzb":
                        await client.control_usenet({"action": "delete", "usenet_id": d.torbox_ref})
            except Exception:
                # Best-effort only
                pass

    async def _notify_arrs(self, local_path: str) -> None:
        with SessionLocal() as db:
            sonarr_url = self._get_setting(db, "sonarr_url")
            sonarr_key = self._get_setting(db, "sonarr_api_key")
            radarr_url = self._get_setting(db, "radarr_url")
            radarr_key = self._get_setting(db, "radarr_api_key")
            whisparr_url = self._get_setting(db, "whisparr_url")
            whisparr_key = self._get_setting(db, "whisparr_api_key")

        # Each is optional; failures shouldn't break completion
        try:
            if sonarr_url and sonarr_key:
                await sonarr_scan(self._http, base_url=sonarr_url, api_key=sonarr_key, path=local_path)
        except Exception:
            pass
        try:
            if radarr_url and radarr_key:
                await radarr_scan(self._http, base_url=radarr_url, api_key=radarr_key, path=local_path)
        except Exception:
            pass
        try:
            if whisparr_url and whisparr_key:
                await whisparr_scan(self._http, base_url=whisparr_url, api_key=whisparr_key, path=local_path)
        except Exception:
            pass

    async def _get_download_filename(self, url: str, fallback_filename: str) -> str:
        """
        Extract the actual filename from the download URL/headers.
        Returns a safe filename with the correct extension.
        """
        import re
        from urllib.parse import unquote, urlparse
        
        # Try to get filename from Content-Disposition header via HEAD request
        try:
            r = await self._http.head(url, follow_redirects=True, timeout=10)
            r.raise_for_status()
            cd = r.headers.get("content-disposition", "")
            if cd:
                # Parse Content-Disposition: attachment; filename="file.mp4"
                match = re.search(r'filename[*]?=(?:"([^"]+)"|([^;]+))', cd, re.IGNORECASE)
                if match:
                    filename = match.group(1) or match.group(2)
                    filename = unquote(filename.strip())
                    if filename:
                        # Sanitize filename
                        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                        return filename
        except Exception:
            pass
        
        # Try to extract from URL
        try:
            parsed = urlparse(url)
            path = unquote(parsed.path)
            if path:
                filename = Path(path).name
                if filename and '.' in filename:
                    # Sanitize filename
                    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                    return filename
        except Exception:
            pass
        
        # Try to infer extension from Content-Type
        try:
            r = await self._http.head(url, follow_redirects=True, timeout=10)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "").lower()
            # Map common content types to extensions
            ext_map = {
                "video/mp4": ".mp4",
                "video/x-matroska": ".mkv",
                "video/x-msvideo": ".avi",
                "application/x-bittorrent": ".torrent",
                "application/x-nzb": ".nzb",
                "application/zip": ".zip",
                "application/x-zip-compressed": ".zip",
            }
            for ct, ext in ext_map.items():
                if ct in content_type:
                    # Remove original extension and add correct one
                    base = Path(fallback_filename).stem
                    return f"{base}{ext}"
        except Exception:
            pass
        
        # Fallback: use original filename but remove .nzb/.torrent extension
        base = Path(fallback_filename).stem
        # If it was .nzb or .torrent, we don't know the real extension, so use generic
        if fallback_filename.lower().endswith(('.nzb', '.torrent')):
            return f"{base}.bin"  # Generic binary extension
        return Path(fallback_filename).name

    async def _download_stream(self, url: str, out_path: Path, download_id: int) -> Path:
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        if tmp_path.exists():
            tmp_path.unlink()

        start_ts = time.monotonic()
        last_update = start_ts
        got = 0

        async with self._http.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or "0")
            
            # Extract actual filename from Content-Disposition header if available
            actual_filename = None
            import re
            from urllib.parse import unquote
            cd = r.headers.get("content-disposition", "")
            if cd:
                match = re.search(r'filename[*]?=(?:"([^"]+)"|([^;]+))', cd, re.IGNORECASE)
                if match:
                    actual_filename = unquote((match.group(1) or match.group(2)).strip())
                    # Sanitize filename
                    actual_filename = re.sub(r'[<>:"/\\|?*]', '_', actual_filename)
            
            # If we got a filename from headers and it differs from current path, update it
            if actual_filename and actual_filename != out_path.name:
                out_path = out_path.parent / actual_filename
                # Update tmp_path to match
                tmp_path = out_path.with_suffix(out_path.suffix + ".part")
                if tmp_path.exists():
                    tmp_path.unlink()
            
            with open(tmp_path, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                    # Allow cancellation while streaming
                    with SessionLocal() as db:
                        it = db.get(Download, download_id)
                        if not it or it.status == "cancelled":
                            raise RuntimeError("Cancelled")
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if total > 0:
                        pct = 10 + int((got / total) * 89)
                    else:
                        pct = None

                    now = time.monotonic()
                    elapsed = max(now - start_ts, 0.001)
                    speed_bps = int(got / elapsed)

                    if now - last_update > 0.5:  # throttle DB writes a bit
                        last_update = now
                        with SessionLocal() as db:
                            it = db.get(Download, download_id)
                            if it and it.status == "downloading":
                                if pct is not None:
                                    it.progress = min(99, max(it.progress, pct))
                                it.current_speed_bps = speed_bps
                                db.add(it)
                                db.commit()

        tmp_path.rename(out_path)

        # Clear speed after completion
        with SessionLocal() as db:
            it = db.get(Download, download_id)
            if it:
                it.current_speed_bps = None
                db.add(it)
                db.commit()
        
        # If downloaded file is a zip, extract it to a folder
        final_path = out_path
        if out_path.suffix.lower() == ".zip" or zipfile.is_zipfile(out_path):
            try:
                extract_dir = out_path.parent / out_path.stem
                extract_dir.mkdir(exist_ok=True)
                
                with zipfile.ZipFile(out_path, "r") as zip_ref:
                    zip_ref.extractall(extract_dir)
                
                # Remove the zip file after extraction
                out_path.unlink()
                
                # If there's only one file in the extracted folder, use that file path
                # Otherwise, use the folder path
                extracted_files = list(extract_dir.iterdir())
                if len(extracted_files) == 1 and extracted_files[0].is_file():
                    final_path = extracted_files[0]
                else:
                    final_path = extract_dir
                
                logger.info(f"Extracted zip {out_path.name} to {final_path}")
            except Exception as e:
                logger.warning(f"Failed to extract zip {out_path}: {e}, keeping zip file")
                final_path = out_path
        
        return final_path  # Return the final path (extracted folder/file or original file)

    async def _scan_blackhole(self) -> None:
        """
        Watches a configured "blackhole" directory for .torrent/.nzb files and
        automatically queues them every few seconds.
        """
        from torboxed.db import KVSetting  # local import to avoid cycles

        with SessionLocal() as db:
            enabled = (self._get_setting(db, "blackhole_enabled") or "").lower() in ("1", "true", "yes", "on")
            bh_path = self._get_setting(db, "blackhole_path")

        if not enabled or not bh_path:
            return

        base = Path(bh_path)
        if not base.exists() or not base.is_dir():
            return

        uploads_dir = Path(settings.data_dir) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        processed_dir = base / "_processed"
        processed_dir.mkdir(exist_ok=True)

        # Scan recursively to find files in subdirectories (for category detection)
        def scan_directory(dir_path: Path, base_path: Path):
            for entry in dir_path.iterdir():
                if entry.is_file():
                    if entry.name.startswith("."):
                        continue
                    ext = entry.suffix.lower()
                    if ext not in (".torrent", ".nzb"):
                        continue
                    
                    source_type = "torrent" if ext == ".torrent" else "nzb"
                    
                    # Detect category from path: if file is in base/radarr/, category is "radarr"
                    category = None
                    try:
                        rel_path = entry.parent.relative_to(base_path)
                        # Check if any parent directory matches an Arr app name
                        for part in rel_path.parts:
                            part_lower = part.lower()
                            if part_lower in ("radarr", "sonarr", "whisparr"):
                                category = part_lower
                                break
                    except Exception:
                        pass
                    
                    try:
                        content = entry.read_bytes()
                    except Exception:
                        continue

                    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in entry.name) or "file"

                    with SessionLocal() as db:
                        d = Download(filename=entry.name, source_type=source_type, category=category, status="queued", progress=0)
                        db.add(d)
                        db.commit()
                        db.refresh(d)

                        upload_path = uploads_dir / f"{d.id}_{safe_name}"
                        with open(upload_path, "wb") as f:
                            f.write(content)

                        kv = db.get(KVSetting, f"upload_path:{d.id}")
                        if kv:
                            kv.value = str(upload_path)
                        else:
                            kv = KVSetting(key=f"upload_path:{d.id}", value=str(upload_path))
                        db.add(kv)
                        db.commit()

                    try:
                        moved_to = processed_dir / entry.name
                        entry.rename(moved_to)
                        with SessionLocal() as db:
                            kv2 = db.get(KVSetting, f"source_path:{d.id}")
                            if kv2:
                                kv2.value = str(moved_to)
                            else:
                                kv2 = KVSetting(key=f"source_path:{d.id}", value=str(moved_to))
                            db.add(kv2)
                            db.commit()
                    except Exception:
                        # If we can't move it, at least avoid infinite re-processing
                        try:
                            entry.unlink()
                        except Exception:
                            pass
                elif entry.is_dir():
                    # Recursively scan subdirectories for category detection
                    scan_directory(entry, base_path)
        
        # Start scanning from base directory
        scan_directory(base, base)

    @staticmethod
    def _get_setting(db, key: str) -> str | None:  # noqa: ANN001
        from torboxed.db import KVSetting

        row = db.get(KVSetting, key)
        return row.value if row else None


worker = DownloadWorker()


from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


class TorboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class TorboxSubmitResult:
    torrent_id: str  # generic "job id" for torrents or usenet


@dataclass(frozen=True)
class TorboxStatusResult:
    is_ready: bool
    download_url: str | None
    progress: int | None = None


class TorboxClient:
    """
    Torbox API adapter.

    NOTE: Torbox's API surface may differ; this client is intentionally isolated so
    updating endpoint paths/response parsing is straightforward.
    """

    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=60)

    @property
    def _api_root(self) -> str:
        """
        Torbox main collection uses URLs like:
        https://api.torbox.app/v1/api/torrents/createtorrent

        We keep base_url as the host (https://api.torbox.app) and append /v1/api.
        """
        return f"{self.base_url}/v1/api"

    def _headers(self) -> dict[str, str]:
        # Torbox Postman collection expects an API key; we send X-API-Key and Bearer for compatibility.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
        }

    async def submit_file(self, *, filename: str, content: bytes, source_type: str) -> TorboxSubmitResult:
        """
        Submit a torrent or NZB file to Torbox.

        - Torrent: POST /api/torrents/createtorrent
        - Usenet/NZB: POST /api/usenet/createusenetdownload
        Ref: https://www.postman.com/torbox/torbox/collection/b6l9hbv/main-api
        """
        if source_type == "torrent":
            url = f"{self._api_root}/torrents/createtorrent"
        elif source_type == "nzb":
            url = f"{self._api_root}/usenet/createusenetdownload"
        else:
            raise TorboxError(f"Unsupported source_type: {source_type}")

        files = {"file": (filename, content)}

        r = await self._http.post(url, headers=self._headers(), files=files)
        if r.status_code >= 400:
            raise TorboxError(f"Torbox submit failed ({r.status_code}): {r.text}")

        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise TorboxError(f"Torbox submit returned invalid JSON: {r.text}") from e

        if isinstance(payload, dict):
            data = payload.get("data") or payload
        else:
            data = {"value": payload}

        # Torrents and Usenet may use slightly different keys; we normalise them.
        tid = None
        if source_type == "torrent":
            torrent_obj = data.get("torrent")
            if not isinstance(torrent_obj, dict):
                torrent_obj = None
            # For torrents, prefer hash (used by torrentinfo/requestdl), fallback to id
            tid = (
                data.get("hash")
                or (torrent_obj or {}).get("hash")
                or data.get("torrent_id")
                or data.get("id")
                or (torrent_obj or {}).get("id")
                or (torrent_obj or {}).get("torrent_id")
            )
        elif source_type == "nzb":
            usenet_obj = data.get("usenet") or data.get("download")
            if not isinstance(usenet_obj, dict):
                usenet_obj = None
            tid = (
                data.get("usenetdownload_id")
                or data.get("usenet_id")
                or data.get("id")
                or (usenet_obj or {}).get("id")
            )

        if not tid:
            raise TorboxError(f"Torbox create response missing id: {payload}")
        return TorboxSubmitResult(torrent_id=str(tid))

    async def get_torrent_info(self, *, torrent_id: str) -> dict:
        """
        Torbox Postman collection: GET /api/torrents/torrentinfo
        Ref: https://www.postman.com/torbox/torbox/folder/2ewnuh0/torrents
        
        Note: torrent_id should be the hash value returned from createtorrent.
        """
        url = f"{self._api_root}/torrents/torrentinfo"
        # Torbox API expects 'hash' parameter, not 'torrent_id'
        r = await self._http.get(url, headers=self._headers(), params={"hash": torrent_id, "token": self.api_key})
        if r.status_code >= 400:
            raise TorboxError(f"Torbox torrentinfo failed ({r.status_code}): {r.text}")
        payload = r.json()
        if isinstance(payload, dict):
            return payload.get("data") or payload
        return {"value": payload}

    async def list_torrents(self) -> dict:
        """
        GET /torrents/mylist
        """
        url = f"{self._api_root}/torrents/mylist"
        r = await self._http.get(url, headers=self._headers(), params={"token": self.api_key})
        r.raise_for_status()
        payload = r.json()
        # If payload is a dict, check for "data" key, otherwise return the payload itself
        if isinstance(payload, dict):
            return payload.get("data", payload)
        # If it's a list, wrap it in a dict so get_torrent_info_from_list can handle it
        if isinstance(payload, list):
            return {"items": payload}
        return {"value": payload}

    async def get_torrent_info_from_list(self, *, hash_value: str) -> dict:
        """
        Get info about a specific torrent by querying mylist and finding the matching hash.
        Returns status, progress, etc. This is safer than calling torrentinfo directly
        which may fail if the torrent isn't fully processed yet.
        """
        import logging
        logger = logging.getLogger("torboxed.torbox_client")
        
        mylist = await self.list_torrents()
        if not isinstance(mylist, dict):
            # mylist might be a list directly
            if isinstance(mylist, list):
                torrents = mylist
            else:
                logger.warning(f"get_torrent_info_from_list: mylist is not dict/list: {type(mylist)}")
                return {}
        else:
            # mylist might be a list of torrents, or a dict with a list
            torrents = mylist.get("items") or mylist.get("data") or mylist.get("list") or []
            if not isinstance(torrents, list):
                # If it's a single torrent dict, wrap it
                if isinstance(torrents, dict):
                    torrents = [torrents]
                else:
                    torrents = []
        
        # Find the torrent matching our hash
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            # Try multiple possible hash field names
            torrent_hash = (
                str(torrent.get("hash"))
                or str(torrent.get("infohash"))
                or str(torrent.get("torrent_id"))
                or str(torrent.get("id"))
            )
            if torrent_hash and torrent_hash.lower() == str(hash_value).lower():
                logger.debug(f"get_torrent_info_from_list: Found torrent {hash_value}, status={torrent.get('status')}, progress={torrent.get('progress')}")
                return torrent
        
        logger.debug(f"get_torrent_info_from_list: Torrent {hash_value} not found in mylist (checked {len(torrents)} torrents)")
        return {}

    async def check_torrents_cached(self, *, hashes: str) -> dict:
        """
        GET /torrents/checkcached
        `hashes` is a comma-separated string of infohashes.
        """
        url = f"{self._api_root}/torrents/checkcached"
        r = await self._http.get(url, headers=self._headers(), params={"hashes": hashes, "token": self.api_key})
        r.raise_for_status()
        payload = r.json()
        return payload.get("data") if isinstance(payload, dict) else {"value": payload}

    async def export_torrent_data(self) -> dict:
        """
        GET /torrents/exportdata
        """
        url = f"{self._api_root}/torrents/exportdata"
        r = await self._http.get(url, headers=self._headers(), params={"token": self.api_key})
        r.raise_for_status()
        payload = r.json()
        return payload.get("data") if isinstance(payload, dict) else {"value": payload}

    async def control_torrent(self, payload: dict) -> dict:
        """
        POST /torrents/controltorrent
        The exact payload shape (e.g. {"action": "pause", "torrent_id": ...}) comes from Torbox docs.
        """
        url = f"{self._api_root}/torrents/controltorrent"
        r = await self._http.post(url, headers=self._headers(), params={"token": self.api_key}, json=payload)
        r.raise_for_status()
        body = r.json()
        return body.get("data") if isinstance(body, dict) else {"value": body}

    async def request_download_link(self, *, torrent_id: str, hash_value: str | None = None) -> str | None:
        """
        Torbox Postman collection: GET /api/torrents/requestdl
        Ref: https://www.postman.com/torbox/torbox/folder/2ewnuh0/torrents
        
        Note: requestdl requires either file_id or zip_link=true.
        We use zip_link=true to get all files as a zip.
        """
        url = f"{self._api_root}/torrents/requestdl"
        base_params = {
            "redirect": "false",
            "zip_link": "true",  # Required: either file_id or zip_link=true
            "token": self.api_key,
        }
        
        # Try different parameter combinations with zip_link=true
        # Filter out None and "None" string values
        attempts = []
        if torrent_id and str(torrent_id).lower() != "none":
            attempts.append({**base_params, "torrent_id": torrent_id})
        if hash_value and hash_value != torrent_id and str(hash_value).lower() != "none":
            attempts.append({**base_params, "hash": hash_value})
        # If both provided, try with both
        if (torrent_id and str(torrent_id).lower() != "none" and 
            hash_value and hash_value != torrent_id and str(hash_value).lower() != "none"):
            attempts.append({**base_params, "torrent_id": torrent_id, "hash": hash_value})
        
        for params in attempts:
            r = await self._http.get(url, headers=self._headers(), params=params)
            # 404/422/500 might mean not ready yet or wrong params - try next combination
            if r.status_code in (404, 422, 500):
                continue
            if r.status_code >= 400:
                raise TorboxError(f"Torbox requestdl failed ({r.status_code}): {r.text}")
            
            # Success! Parse the response
            payload = r.json()
            if isinstance(payload, dict):
                data = payload.get("data") or payload
            else:
                data = payload

            # Torbox may return a direct string URL in `data`
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                return data.get("download_url") or data.get("link") or data.get("url")
            return None
        
        # All attempts failed with 404/422/500 - torrent not ready yet
        return None

    async def request_usenet_download_link(self, *, job_id: str) -> str | None:
        """
        Usenet/NZB analogue of requestdl.

        Torbox Postman collection: GET /api/usenet/requestdl
        Ref: https://www.postman.com/torbox/torbox/collection/b6l9hbv/main-api
        """
        url = f"{self._api_root}/usenet/requestdl"
        params = {
            "usenet_id": job_id,
            "redirect": "false",
            "zip_link": "false",
            "token": self.api_key,
        }
        r = await self._http.get(url, headers=self._headers(), params=params)

        # If the job is not ready yet, Torbox may answer 404 / 400 / 500 – we treat that
        # as "not yet ready" and return None. Other 4xx/5xx bubble up.
        if r.status_code in (404, 500):
            # 500 often means "DATABASE_ERROR" or job not ready yet - treat as not ready
            return None
        if r.status_code >= 400:
            raise TorboxError(f"Torbox usenet requestdl failed ({r.status_code}): {r.text}")

        payload = r.json()
        if isinstance(payload, dict):
            data = payload.get("data") or payload
        else:
            data = payload

        # Torbox may return a direct string URL in `data`
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return data.get("download_url") or data.get("link") or data.get("url")
        return None

    async def list_usenet(self) -> dict:
        """
        GET /usenet/mylist
        """
        url = f"{self._api_root}/usenet/mylist"
        r = await self._http.get(url, headers=self._headers(), params={"token": self.api_key})
        r.raise_for_status()
        payload = r.json()
        # If payload is a dict, check for "data" key, otherwise return the payload itself
        if isinstance(payload, dict):
            # If it has "data", return that; otherwise return the whole payload
            return payload.get("data", payload)
        # If it's a list, wrap it in a dict so get_usenet_info can handle it
        if isinstance(payload, list):
            return {"items": payload}
        return {"value": payload}

    async def get_usenet_info(self, *, usenet_id: str) -> dict:
        """
        Get info about a specific usenet job by querying mylist and finding the matching ID.
        Returns status, progress, etc.
        """
        import logging
        logger = logging.getLogger("torboxed.torbox_client")
        
        mylist = await self.list_usenet()
        if not isinstance(mylist, dict):
            # mylist might be a list directly
            if isinstance(mylist, list):
                jobs = mylist
            else:
                logger.warning(f"get_usenet_info: mylist is not dict/list: {type(mylist)}")
                return {}
        else:
            # mylist might be a list of jobs, or a dict with a list
            jobs = mylist.get("items") or mylist.get("data") or mylist.get("list") or []
            if not isinstance(jobs, list):
                # If it's a single job dict, wrap it
                if isinstance(jobs, dict):
                    jobs = [jobs]
                else:
                    jobs = []
        
        # Find the job matching our usenet_id
        for job in jobs:
            if not isinstance(job, dict):
                continue
            # Try multiple possible ID field names
            job_id = (
                str(job.get("usenetdownload_id"))
                or str(job.get("usenet_id"))
                or str(job.get("id"))
                or str(job.get("download_id"))
            )
            if job_id and job_id == str(usenet_id):
                logger.debug(f"get_usenet_info: Found job {usenet_id}, status={job.get('status')}, progress={job.get('progress')}")
                return job
        
        logger.warning(f"get_usenet_info: Job {usenet_id} not found in mylist (checked {len(jobs)} jobs)")
        return {}

    async def check_usenet_cached(self, *, hashes: str) -> dict:
        """
        GET /usenet/checkcached
        """
        url = f"{self._api_root}/usenet/checkcached"
        r = await self._http.get(url, headers=self._headers(), params={"hashes": hashes, "token": self.api_key})
        r.raise_for_status()
        payload = r.json()
        return payload.get("data") if isinstance(payload, dict) else {"value": payload}

    async def control_usenet(self, payload: dict) -> dict:
        """
        POST /usenet/controlusenetdownload
        """
        url = f"{self._api_root}/usenet/controlusenetdownload"
        r = await self._http.post(url, headers=self._headers(), params={"token": self.api_key}, json=payload)
        r.raise_for_status()
        body = r.json()
        return body.get("data") if isinstance(body, dict) else {"value": body}

    async def get_status(self, *, reference_id: str, kind: str = "torrent") -> TorboxStatusResult:
        """
        Compatibility wrapper used by the worker.

        Treats reference_id as Torbox torrent_id (kind="torrent") or usenet_id (kind="nzb").
        """
        if kind == "torrent":
            import logging
            logger = logging.getLogger("torboxed.torbox_client")
            
            # First, check if torrent is in the list (means it's been processed)
            list_info = await self.get_torrent_info_from_list(hash_value=reference_id)
            
            # If not in list, torrent might still be processing - return not ready
            if not list_info:
                logger.debug(f"get_status torrent: Torrent {reference_id} not found in mylist yet, still processing")
                return TorboxStatusResult(
                    is_ready=False,
                    download_url=None,
                    progress=None,
                )
            
            # Extract status and progress from list info
            status = str(list_info.get("status") or list_info.get("state") or "").lower()
            raw_progress = list_info.get("progress") or list_info.get("percentage") or list_info.get("percent_done")
            progress: int | None
            try:
                progress = int(float(raw_progress)) if raw_progress is not None else None
            except Exception:
                progress = None
            
            # Get torrent_id from list info (filter out None values)
            list_torrent_id = None
            logger.debug(f"get_status torrent: list_info keys: {list(list_info.keys()) if isinstance(list_info, dict) else 'not a dict'}")
            for key in ("torrent_id", "id", "torrentId", "torrentID"):
                val = list_info.get(key)
                if val is not None and str(val).lower() != "none":
                    list_torrent_id = str(val)
                    logger.debug(f"get_status torrent: Found torrent_id={list_torrent_id} from list_info[{key}]")
                    break
            
            # Try to get torrent_id from detailed info (torrentinfo) if available
            detailed_torrent_id = None
            try:
                detailed_info = await self.get_torrent_info(torrent_id=reference_id)
                if isinstance(detailed_info, dict):
                    logger.debug(f"get_status torrent: torrentinfo response keys: {list(detailed_info.keys())}")
                    # Use detailed info if available
                    raw_progress = detailed_info.get("progress") or detailed_info.get("percentage") or raw_progress
                    try:
                        progress = int(float(raw_progress)) if raw_progress is not None else None
                    except Exception:
                        pass
                    # Extract torrent_id from detailed info - try multiple possible field names
                    for key in ("torrent_id", "id", "torrentId", "torrentID"):
                        val = detailed_info.get(key)
                        if val is not None and str(val).lower() != "none":
                            detailed_torrent_id = str(val)
                            logger.debug(f"get_status torrent: Found torrent_id={detailed_torrent_id} from torrentinfo[{key}]")
                            break
            except TorboxError as e:
                # If torrentinfo fails (e.g., 500), that's okay - we'll use list info
                logger.debug(f"get_status torrent: torrentinfo failed for {reference_id} (may still be processing): {e}")
            
            # Prefer detailed_torrent_id, fallback to list_torrent_id
            torrent_id_to_use = detailed_torrent_id or list_torrent_id
            
            # Always try to request download link - if it succeeds, torrent is ready
            # This is more reliable than parsing status fields
            download_url = None
            try:
                # Try with torrent_id if we have it, otherwise just hash
                if torrent_id_to_use:
                    download_url = await self.request_download_link(
                        torrent_id=torrent_id_to_use,
                        hash_value=reference_id
                    )
                else:
                    # Only hash available
                    download_url = await self.request_download_link(
                        torrent_id=reference_id,  # Use hash as torrent_id
                        hash_value=None
                    )
                if download_url:
                    logger.debug(f"get_status torrent: Got download link for {reference_id}")
                    return TorboxStatusResult(
                        is_ready=True,
                        download_url=download_url,
                        progress=progress or 100,
                    )
            except TorboxError as e:
                # If requestdl fails with 404/422/500, torrent isn't ready yet or wrong params
                logger.debug(f"get_status torrent: requestdl not ready for {reference_id}: {e}")
                download_url = None
            
            # Check if torrent is completed based on available info
            list_completed = bool(
                list_info.get("completed")
                or list_info.get("is_complete")
                or (progress is not None and progress >= 100)
                or status in ("completed", "complete", "done", "finished", "downloaded")
            )
            
            return TorboxStatusResult(
                is_ready=False,  # Not ready until we have a download link
                download_url=None,
                progress=progress,
            )

        if kind == "nzb":
            import logging
            logger = logging.getLogger("torboxed.torbox_client")
            
            # First, check the usenet job status via mylist to see if it's complete
            info = await self.get_usenet_info(usenet_id=reference_id)
            if not isinstance(info, dict):
                info = {}
            
            # Extract status and progress from the job info
            status = str(info.get("status") or info.get("state") or "").lower()
            raw_progress = info.get("progress") or info.get("percentage") or info.get("percent_done")
            progress: int | None
            try:
                progress = int(float(raw_progress)) if raw_progress is not None else None
            except Exception:
                progress = None
            
            # Check if job is completed based on status fields
            status_completed = bool(
                info.get("completed")
                or info.get("is_complete")
                or (progress is not None and progress >= 100)
                or status in ("completed", "complete", "done", "finished", "downloaded", "ready")
            )
            
            # Try to request download link - if it succeeds, job is definitely ready
            # This is more reliable than parsing status fields
            download_url = None
            try:
                download_url = await self.request_usenet_download_link(job_id=reference_id)
                if download_url:
                    logger.debug(f"get_status nzb: Got download link for {reference_id}")
                    return TorboxStatusResult(
                        is_ready=True,
                        download_url=download_url,
                        progress=progress or 100,
                    )
            except TorboxError as e:
                # If requestdl fails with 404/500, job isn't ready yet
                logger.debug(f"get_status nzb: requestdl not ready for {reference_id}: {e}")
                download_url = None
            
            # If we have status indicating completion but no link yet, still mark as not ready
            # (we'll retry on next poll)
            return TorboxStatusResult(
                is_ready=False,
                download_url=None,
                progress=progress,
            )

        raise TorboxError(f"Unknown job kind: {kind}")

    async def aclose(self) -> None:
        await self._http.aclose()


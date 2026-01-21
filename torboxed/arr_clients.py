from __future__ import annotations

import httpx


class ArrError(RuntimeError):
    pass


async def _post_command(http: httpx.AsyncClient, *, base_url: str, api_key: str, payload: dict) -> None:
    url = base_url.rstrip("/") + "/api/v3/command"
    r = await http.post(url, params={"apikey": api_key}, json=payload)
    if r.status_code >= 400:
        raise ArrError(f"Arr command failed ({r.status_code}): {r.text}")


async def sonarr_scan(http: httpx.AsyncClient, *, base_url: str, api_key: str, path: str) -> None:
    await _post_command(
        http,
        base_url=base_url,
        api_key=api_key,
        payload={
            "name": "DownloadedEpisodesScan",
            "path": path,
            "importMode": "Move",
        },
    )


async def radarr_scan(http: httpx.AsyncClient, *, base_url: str, api_key: str, path: str) -> None:
    await _post_command(
        http,
        base_url=base_url,
        api_key=api_key,
        payload={
            "name": "DownloadedMoviesScan",
            "path": path,
            "importMode": "Move",
        },
    )


async def whisparr_scan(http: httpx.AsyncClient, *, base_url: str, api_key: str, path: str) -> None:
    # Whisparr is Sonarr-like; command naming is typically DownloadedEpisodesScan.
    await _post_command(
        http,
        base_url=base_url,
        api_key=api_key,
        payload={
            "name": "DownloadedEpisodesScan",
            "path": path,
            "importMode": "Move",
        },
    )


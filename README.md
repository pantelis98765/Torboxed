## Torboxed

Dockerized web app that submits NZB/torrent files to Torbox, then downloads the resulting file(s) locally so Sonarr/Radarr/Whisparr can import.

### Run

```bash
docker compose up --build
```

Open `http://localhost:8080`.

### Notes

- Torbox client endpoints in `torboxed/torbox_client.py` are currently **placeholders** (`/v1/submit` + `/v1/status/{id}`) until we confirm Torbox's exact API paths/fields.
- Rate limit: enforced at **10 Torbox API calls/min** (configurable in Settings; restart container to apply).
- Local download concurrency: configurable in Settings; restart container to apply.
- Downloads live in `/data/downloads` inside the container (persisted via Docker volume).


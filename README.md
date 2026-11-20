# Nano Reels Backend

A real backend for the Nano Reels video app: **Python (FastAPI +
Uvicorn), backend-only storage. No external service anywhere.**

- Metadata (videos, channels, comments, likes, subscriptions, watch
  history, saved videos, users) lives in **SQLite** (`./data/nano.db`,
  Python stdlib `sqlite3`) — no Postgres, no Neon, no Supabase.
- Video files live on the **server's local disk** (`./data/files`) —
  no MEGA, no S3/B2, no external storage.
- Playback goes through the backend: `GET /api/stream/<video_id>`,
  served straight from disk with HTTP Range support, so the existing
  ExoPlayer in the app keeps working with no changes.

**Videos are never deleted by users — there is no user-facing delete
endpoint at all.** The only delete is an admin-only endpoint guarded
by `ADMIN_KEY`, and it deletes the metadata row **and** the stored
file on the server disk.

## ⚠️ HONEST WARNING — videos do NOT survive a restart or redeploy

**Render's free tier has ephemeral disk.** A server restart or a
redeploy **wipes `./data` completely — every video file AND the
SQLite database with it.** The keep-alive ping (every 10 minutes)
only keeps the instance awake; it does **not** protect against
restarts, crashes, or deploys.

- **NEVER redeploy this service without the owner's permission
  first.** A redeploy is a full data loss event for this backend.
- If videos must survive redeploys, the storage has to move back
  out to an external service (MEGA / S3 / B2) — that is a deliberate
  architecture change, not a bug fix.
- There is no backup of `./data` anywhere. If the instance dies,
  the videos are gone.

## Storage limits

- **Per file: 1 GB** — uploads over 1 GB are refused with HTTP 413
  `file_too_large`.
- **Total: the server's disk.** `GET /api/storage` reports the size
  of `./data` (`used_bytes`), the disk total (`total_bytes`) and the
  disk free space (`free_bytes`) for the app's storage meter. There
  is no separate quota — when the disk fills, uploads fail.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | `{"status":"ok"}`. Lightweight keep-alive target — works even when no env vars are set, and makes no database call. Ping it every 10 minutes to keep the free Render instance awake. |
| GET | `/api/storage` | Disk usage: `{ used_bytes, total_bytes, free_bytes, used_gb, total_gb, free_gb, percent_used }`. `used_bytes` is the size of `./data`; `total_bytes`/`free_bytes` are the server disk. |
| GET | `/api/videos?page=&limit=&channel_id=&is_short=&category=&visibility=` | List videos, newest first. Default `visibility=public`, `page=1`, `limit=20` (max 100). `is_short` filters the real `is_short` boolean column. Returns `{ videos, page, limit, total }`. |
| GET | `/api/videos/:id` | One video row (includes `file_path`, `upload_id`, `file_size`). 404 if missing. |
| POST | `/api/videos` | Create a video metadata row (call AFTER the file is uploaded via `POST /api/upload`, or to re-save a retried upload). Required: `title`, `video_url`. Optional: `description`, `caption`, `category`, `tags`, `visibility` (public/private/unlisted), `thumbnail_url`, `channel_id`, `channel_name`, `channel_avatar_url`, `owner_uid`, `duration_sec`, `is_short`, `renditions`, `captions_url`, `file_path`, `upload_id`, `file_size`. If `is_short` is omitted it is derived from `duration_sec <= 60`. Idempotent per `upload_id`: a repeated call with the same `upload_id` returns the already-saved row instead of duplicating it. |
| POST | `/api/videos/:id/view` | Increment `views` by 1 (atomic). Returns `{ id, views }`. |
| POST | `/api/videos/:id/like` | Increment `likes` by 1. Body `{ "user_id": "..." }` optional — with a `user_id`, one like per user is recorded in the `likes` table and the counter only moves on the first like. |
| GET | `/api/channels/:id` | One channel row. 404 if missing. |
| GET | `/api/channels/:id/videos?page=&limit=` | That channel's videos, newest first. |
| GET | `/api/stream/:id` | Play a video: serves the stored file from `./data/files` with HTTP Range support (206 Partial Content) for ExoPlayer seeking. The app uses `https://<backend>/api/stream/<video_id>` as the video URL. 404 if the video or its file is missing. |
| GET | `/api/files/:kind/:filename` | Serve a stored file directly (`kind` is `videos` or `thumbnails`) — used for thumbnails. 404 if missing. |
| POST | `/api/upload` | Upload a video/thumbnail file to the server disk (multipart `file=`, `kind=video` (default) or `thumbnail`, optional `upload_id` idempotency key, plus video metadata for `kind=video`: `title` **required**, `description`, `category`, `channel_id`, `channel_name`, `owner_uid`, `duration_sec`, `is_short`, `thumbnail_url`). 1 GB per file. Returns 201 `{ key, file_url, size, content_type, upload_id, video_id }` (`video_id` is null for thumbnails). For videos `file_url` is `/api/stream/<video_id>`; for thumbnails `/api/files/thumbnails/<name>`. |
| POST | `/api/admin/delete_video` | **Admin only.** Header `x-admin-key: <ADMIN_KEY>`, body `{ "video_id": "..." }`. Deletes the metadata row **and** the stored file. Returns 503 if `ADMIN_KEY` is not set. |

## Upload flow (app side) — fully automatic, one call

1. `POST /api/upload` with the video file + `title` and the other
   metadata fields → the backend:
   - spools the file to a temp file (1 GB cap),
   - moves it into `./data/files/videos/` under a deterministic name
     (`videos-<upload_id>-<safe name>`),
   - **writes the SQLite video row (`file_path` included) and
     commits it BEFORE returning success**,
   - deletes the temp file.
2. The returned `video_id` is the video's id; the video appears in
   the feed. No second call is needed. (If the app already uploaded
   once and lost the response, re-send the same `upload_id`: the
   backend returns the already-saved row instead of uploading or
   saving twice — a retried upload never creates a duplicate file
   or a duplicate row.)

Playback: use `GET /api/stream/<video_id>` as the video URL in
ExoPlayer.

## Environment variables (Render → Environment)

| Variable | Required | Value |
|---|---|---|
| `ADMIN_KEY` | Yes (for admin delete) | Any long random string you choose; the admin panel sends it as the `x-admin-key` header. |
| `PORT` | No | Render sets this automatically. Defaults to `8000` locally. |
| `DATA_DIR` | No | Where `nano.db` + files live. Defaults to `./data` next to `main.py`. |

`/api/health` answers `{"status":"ok"}` even before any variables
are set. There are no other env vars — no database URL, no storage
credentials. The backend needs nothing external to run.

## Schema

`schema.sql` is the **one** schema definition: `CREATE TABLE IF NOT
EXISTS` for all tables (`videos`, `channels`, `comments`, `likes`,
`subscriptions`, `watch_history`, `saved_videos`, `app_users`;
`created_at` / `watched_at` are epoch milliseconds). `videos` has a
real `is_short` boolean column (stored as INTEGER 0/1), `tags` and
`renditions` as JSON text, the `file_path` of the stored file
(relative to the data dir), the `upload_id` idempotency key (unique
index), and `file_size`.

The backend runs `schema.sql` automatically on startup (idempotent
— safe to run on every start). To apply it manually instead:

```bash
sqlite3 data/nano.db < schema.sql
```

## Deploy (Render, free tier)

1. Push this folder to a GitHub repo (or connect the repo that holds it).
2. In Render: **New → Web Service → Build and deploy from a Git
   repository**, pick the repo. Or use the Blueprint: **New →
   Blueprint**, point it at the repo — `render.yaml` defines the free
   Python web service with health check `/api/health`.
3. Set `ADMIN_KEY` in Render → Environment (paste the value, never
   commit it).
4. Deploy. Check `https://<your-service>.onrender.com/api/health` — it
   should return `{"status":"ok"}`.
5. Keep-alive: ping `/api/health` every 10 minutes (cron / UptimeRobot /
   the app's own pinger) so the free instance does not sleep. Free Render
   spins down after 15 minutes without traffic; the first request after a
   sleep takes ~30–60s to wake.

**Remember the warning above: every redeploy wipes all videos and
all metadata. Ask the owner for permission before redeploying —
every single time.**

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ADMIN_KEY=<choose-a-secret> \
uvicorn main:app --host 0.0.0.0 --port 8000
# curl http://localhost:8000/api/health
```

No other variables are needed. Data lives in `./data/` next to
`main.py` (override with `DATA_DIR`).

## Notes

- View/like counters use atomic `UPDATE ... SET n = n + 1 ...
  RETURNING`, so concurrent increments cannot lose a count.
- `videos.is_short` is a real boolean column now; `POST /api/videos`
  accepts it, and the feed filter uses it directly.
- SQLite is accessed through one connection guarded by one lock
  (stdlib `sqlite3`, `check_same_thread=False`) — simple and safe
  at this backend's traffic level.
- The old versions of this backend (Node + Supabase, Python +
  Postgres + S3/B2, Python + Postgres + MEGA) were removed in favour
  of this backend-only version at the owner's direction
  (2026-11-20): everything lives inside the backend server itself.
  The API surface is unchanged except that upload/stream/storage
  now work on local disk + SQLite, and `mega_url` / `mega_key` are
  gone — uploads return `file_url` instead.

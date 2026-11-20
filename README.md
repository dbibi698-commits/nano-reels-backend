# Nano Reels Backend

A real backend for the Nano Reels video app: Node.js + Express + Supabase
(Postgres + Storage). The app uploads video files to Supabase Storage and
saves the metadata through this API. **Videos are never deleted by users —
there is no user-facing delete endpoint at all.** The only delete is an
admin-only endpoint guarded by `ADMIN_KEY`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | `{"status":"ok"}`. Lightweight keep-alive target — works even when Supabase env vars are missing, and makes no database call. Ping it every 10 minutes to keep the free Render instance awake. |
| GET | `/api/videos?page=&limit=&channel_id=&is_short=&category=&visibility=` | List videos, newest first. Default `visibility=public`, `page=1`, `limit=20` (max 100). `is_short=true` means `duration_sec <= 60` (the Supabase `videos` table has no `is_short` column). Returns `{ videos, page, limit, total }`. |
| GET | `/api/videos/:id` | One video row. 404 if missing. |
| POST | `/api/videos` | Create a video metadata row (call AFTER the file is uploaded to Supabase Storage). Required: `title`, `video_url`. Optional: `description`, `caption`, `category`, `tags`, `visibility` (public/private/unlisted), `thumbnail_url`, `channel_id`, `channel_name`, `channel_avatar_url`, `owner_uid`, `duration_sec`, `renditions`, `captions_url`. |
| POST | `/api/videos/:id/view` | Increment `views` by 1. Returns `{ id, views }`. |
| POST | `/api/videos/:id/like` | Increment `likes` by 1. Body `{ "user_id": "..." }` optional — with a `user_id`, one like per user is recorded in the `likes` table and the counter only moves on the first like. |
| GET | `/api/channels/:id` | One channel row. 404 if missing. |
| GET | `/api/channels/:id/videos?page=&limit=` | That channel's videos, newest first. |
| POST | `/api/admin/delete_video` | **Admin only.** Header `x-admin-key: <ADMIN_KEY>`, body `{ "video_id": "..." }`. Deletes the metadata row only, never the Storage file. Returns 503 if `ADMIN_KEY` is not set. |

## Environment variables (Render → Environment)

| Variable | Required | Value |
|---|---|---|
| `SUPABASE_URL` | Yes (for data endpoints) | The Supabase project URL, e.g. `https://<project-ref>.supabase.co` (the Nano Reels project in `ap-south-1`) |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes (for data endpoints) | The project's **service_role** key from Supabase → Settings → API. Server-side only — never put it in the app or commit it anywhere. |
| `ADMIN_KEY` | Yes (for admin delete) | Any long random string you choose; the admin panel sends it as the `x-admin-key` header. |
| `PORT` | No | Render sets this automatically. Defaults to `3000` locally. |

`/api/health` answers `{"status":"ok"}` even before the Supabase variables
are set; the data endpoints return HTTP 503 `backend_not_configured` until
they are.

## Deploy (Render, free tier)

1. Push this folder to a GitHub repo (or connect the repo that holds it).
2. In Render: **New → Web Service → Build and deploy from a Git repository**,
   pick the repo. Or use the Blueprint: **New → Blueprint**, point it at the
   repo — `render.yaml` defines the free web service with health check
   `/api/health`.
3. Set the three env vars above in Render → Environment (paste the values,
   never commit them).
4. Deploy. Check `https://<your-service>.onrender.com/api/health` — it
   should return `{"status":"ok"}`.
5. Keep-alive: ping `/api/health` every 10 minutes (cron / UptimeRobot /
   the app's own pinger) so the free instance does not sleep. Free Render
   spins down after 15 minutes without traffic; the first request after a
   sleep takes ~30–60s to wake.

## Run locally

```bash
npm install
SUPABASE_URL=https://<project-ref>.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=<service_role key> \
ADMIN_KEY=<choose-a-secret> \
npm start
# curl http://localhost:3000/api/health
```

## Notes

- The Supabase schema this backend expects is
  `~/workspace/aigalaxy-video/AIGALAXY/docs/supabase-schema.sql`
  (`videos`, `channels`, `comments`, `likes`, `subscriptions`,
  `watch_history`, `saved_videos`, `app_users`; `created_at` is epoch
  milliseconds). Run that SQL in the Supabase SQL editor first if the
  tables do not exist yet.
- Video files themselves live in the Supabase Storage `videos` bucket
  (public, 500 MB via HLS segments — see the app's `StorageUpload.kt`).
  This backend only stores and serves metadata.
- View/like counters use read-then-update; under heavy concurrent load a
  count can occasionally be off by one. If exact counts ever matter,
  replace them with a Postgres `rpc` increment function.

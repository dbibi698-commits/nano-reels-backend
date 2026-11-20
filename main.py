# Nano Reels backend — Python (FastAPI + Uvicorn), BACKEND-ONLY storage.
#
# Everything lives inside the backend server itself:
#   - Metadata in SQLite (./data/nano.db, stdlib sqlite3) — no Postgres,
#     no Neon, no Supabase, no external database of any kind.
#   - Video files on the server's local disk (./data/files) — no MEGA,
#     no S3/B2, no external storage of any kind.
#   - Playback through GET /api/stream/<video_id> with HTTP Range
#     support, so the existing ExoPlayer in the app keeps working.
#
# ⚠️  HONEST WARNING — READ BEFORE DEPLOYING:
# Render's free tier has EPHEMERAL disk. A server restart or redeploy
# WIPES ./data — every video file AND the SQLite database with it.
# The keep-alive ping only keeps the instance awake; it does NOT
# protect against restarts. NEVER redeploy this service without the
# owner's permission first. If videos must survive redeploys, the
# storage has to move back out to an external service (MEGA/S3).
#
# There is NO user-facing delete endpoint: videos are never deleted
# from the app. Only the admin endpoint below can delete a video,
# and only with the ADMIN_KEY env var — it deletes the metadata row
# AND the local file.
#
# Secrets come ONLY from environment variables. Nothing is hardcoded.
#   ADMIN_KEY               secret for the admin-only endpoints
#   PORT                    Render sets this; defaults to 8000 locally
#   DATA_DIR                where nano.db + files live (default: ./data
#                           next to this file)
#
# Health: GET /api/health works with no env vars set.
# Schema: schema.sql is applied automatically on startup (idempotent).

import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger("nano-reels-backend")
logging.basicConfig(level=logging.INFO)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB per file
SHORT_MAX_SEC = 60  # fallback only; videos.is_short is a real column now

# ------------------------------------------------------------------ paths
DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path(__file__).parent / "data"))
FILES_DIR = DATA_DIR / "files"
VIDEOS_DIR = FILES_DIR / "videos"
THUMBS_DIR = FILES_DIR / "thumbnails"
DB_PATH = DATA_DIR / "nano.db"


def ensure_dirs():
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------- db
# One SQLite connection, guarded by one lock. sqlite3 is stdlib, the
# database is a single file (./data/nano.db), and the traffic this
# backend sees is small — a serialised connection is the simple,
# correct trade. Every statement runs under the lock, so concurrent
# requests can never interleave a transaction.
_db_lock = threading.RLock()
_db = None


def get_db():
    """The single SQLite connection (created on first use)."""
    global _db
    if _db is not None:
        return _db
    with _db_lock:
        if _db is not None:
            return _db
        ensure_dirs()
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA foreign_keys=ON")
        log.info("SQLite database at %s", DB_PATH)
        return _db


def run_schema():
    """Apply schema.sql once, idempotently. Best-effort: never crash boot."""
    try:
        schema_path = Path(__file__).with_name("schema.sql")
        sql = schema_path.read_text()
        with _db_lock:
            db = get_db()
            db.executescript(sql)
            db.commit()
        log.info("schema.sql applied OK.")
    except Exception as e:  # noqa: BLE001 — startup must not die on DB issues
        log.warning("schema.sql could not be applied (will retry on next start): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    run_schema()
    yield


app = FastAPI(title="Nano Reels backend", lifespan=lifespan)


# -------------------------------------------------------------- helpers
def now_ms() -> int:
    return int(time.time() * 1000)


def parse_paging(request: Request):
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = min(100, max(1, limit))
    return page, limit, (page - 1) * limit


def video_where(request: Request, params: list):
    """Build the WHERE clause for video listings from query params."""
    clauses = ["visibility = ?"]
    params.append(request.query_params.get("visibility", "public"))
    if request.query_params.get("channel_id"):
        clauses.append("channel_id = ?")
        params.append(request.query_params["channel_id"])
    if request.query_params.get("owner_uid"):
        clauses.append("owner_uid = ?")
        params.append(request.query_params["owner_uid"])
    if request.query_params.get("is_short") == "true":
        clauses.append("is_short = 1")
    elif request.query_params.get("is_short") == "false":
        clauses.append("is_short = 0")
    if request.query_params.get("category"):
        clauses.append("category = ?")
        params.append(request.query_params["category"])
    return " AND ".join(clauses)


def clean_row(row) -> dict:
    """sqlite3.Row -> plain dict the app can read as JSON."""
    d = dict(row)
    d.pop("_total", None)
    # SQLite stores booleans as 0/1; the app expects a real boolean.
    if "is_short" in d:
        d["is_short"] = bool(d["is_short"])
    if "is_like" in d:
        d["is_like"] = bool(d["is_like"])
    # tags / renditions are stored as JSON text.
    for f in ("tags", "renditions"):
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except (ValueError, TypeError):
                d[f] = [] if f == "tags" else {}
    return d


def _db_error(e):
    return JSONResponse(status_code=500, content={"error": "db_error", "message": str(e)})


# ---------------------------------------------------------------- health
# Lightweight keep-alive endpoint. Does NOT touch the database, so the
# keep-alive pinger (every 10 min) keeps the free Render instance
# awake without costing database work.
@app.get("/api/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------- storage
# GET /api/storage — server disk usage for the app's storage meter.
# { used_bytes (size of ./data), total_bytes (disk total),
#   free_bytes (disk free), used_gb, total_gb, free_gb, percent_used }
@app.get("/api/storage")
def storage_status():
    try:
        ensure_dirs()
        total, _used_disk, free = shutil.disk_usage(str(DATA_DIR))
        used = 0
        for root, _dirs, files in os.walk(DATA_DIR):
            for name in files:
                try:
                    used += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": "storage_error", "message": str(e)})
    gib = 1024 ** 3
    return {
        "used_bytes": used,
        "total_bytes": total,
        "free_bytes": free,
        "used_gb": round(used / gib, 2),
        "total_gb": round(total / gib, 2),
        "free_gb": round(free / gib, 2),
        "percent_used": round(100.0 * used / total, 1) if total else 0.0,
    }


# ---------------------------------------------------------------- videos
# GET /api/videos?page=&limit=&channel_id=&is_short=&category=&visibility=
# Newest first. Response: { videos, page, limit, total }
@app.get("/api/videos")
def list_videos(request: Request):
    page, limit, offset = parse_paging(request)
    params: list = []
    where = video_where(request, params)
    sql = (
        "SELECT *, COUNT(*) OVER() AS _total FROM videos "
        f"WHERE {where} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?"
    )
    try:
        with _db_lock:
            cur = get_db().execute(sql, params + [limit, offset])
            raw = cur.fetchall()
        total = raw[0]["_total"] if raw else 0
        rows = [clean_row(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    return {"videos": rows, "page": page, "limit": limit, "total": total}


# GET /api/videos/:id — one video row.
@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    try:
        with _db_lock:
            cur = get_db().execute("SELECT * FROM videos WHERE id = ?", (video_id,))
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video not found"})
    return clean_row(row)


# POST /api/videos — create a video metadata row. The app calls this
# AFTER uploading the file (via POST /api/upload), passing the
# file_url it got back — or, for a retried upload, with the same
# upload_id it used before: the insert is idempotent per upload_id
# and returns the already-saved row instead of duplicating it.
# Required: title, video_url.
VIDEO_FIELDS = [
    "title", "description", "caption", "category", "tags", "visibility",
    "video_url", "thumbnail_url", "channel_id", "channel_name",
    "channel_avatar_url", "owner_uid", "duration_sec", "renditions",
    "captions_url", "is_short", "file_path", "upload_id", "file_size",
]


@app.post("/api/videos", status_code=201)
async def create_video(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "validation", "message": "JSON object expected"})
    title = body.get("title")
    if not title or not isinstance(title, str) or not title.strip():
        return JSONResponse(status_code=400, content={"error": "validation", "message": "title is required"})
    video_url = body.get("video_url")
    if not video_url or not isinstance(video_url, str) or not video_url.strip():
        return JSONResponse(status_code=400, content={"error": "validation", "message": "video_url is required"})
    row = {}
    for f in VIDEO_FIELDS:
        if body.get(f) is not None:
            row[f] = body[f]
    if row.get("visibility") and row["visibility"] not in ("public", "private", "unlisted"):
        return JSONResponse(
            status_code=400,
            content={"error": "validation", "message": "visibility must be public, private or unlisted"},
        )
    if row.get("tags") is not None and not isinstance(row["tags"], list):
        return JSONResponse(
            status_code=400,
            content={"error": "validation", "message": "tags must be an array of strings"},
        )
    if row.get("file_size") is not None:
        try:
            row["file_size"] = int(row["file_size"])
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400, content={"error": "validation", "message": "file_size must be an integer"}
            )
    if row.get("is_short") is None:
        # Derive from duration when the app does not send is_short.
        try:
            row["is_short"] = int(row.get("duration_sec") or 0) <= SHORT_MAX_SEC
        except (TypeError, ValueError):
            row["is_short"] = False
    row["is_short"] = 1 if row["is_short"] else 0
    if row.get("tags") is not None:
        row["tags"] = json.dumps(row["tags"])
    if row.get("renditions") is not None:
        row["renditions"] = json.dumps(row["renditions"])
    if not row.get("upload_id"):
        row["upload_id"] = uuid.uuid4().hex
    row["id"] = uuid.uuid4().hex
    row["created_at"] = now_ms()
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    sql = (
        f"INSERT INTO videos ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT (upload_id) DO NOTHING RETURNING *"
    )
    try:
        with _db_lock:
            db = get_db()
            cur = db.execute(sql, list(row.values()))
            created = cur.fetchone()
            if created is None:
                # Same upload_id was already saved (retried upload):
                # return the existing row instead of duplicating it.
                cur = db.execute("SELECT * FROM videos WHERE upload_id = ?", (row["upload_id"],))
                created = cur.fetchone()
            db.commit()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if created is None:
        return JSONResponse(status_code=500, content={"error": "db_error", "message": "could not save video"})
    return clean_row(created)


# POST /api/videos/:id/view — increment views by 1 (atomic).
@app.post("/api/videos/{video_id}/view")
def view_video(video_id: str):
    try:
        with _db_lock:
            db = get_db()
            cur = db.execute(
                "UPDATE videos SET views = views + 1 WHERE id = ? RETURNING id, views",
                (video_id,),
            )
            row = cur.fetchone()
            db.commit()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video not found"})
    return clean_row(row)


# POST /api/videos/:id/like — increment likes by 1.
# Body: { "user_id": "..." } (optional). With a user_id, one like per
# user is recorded in the `likes` table and the counter only moves on
# the first like from that user. Without user_id it is a plain +1.
@app.post("/api/videos/{video_id}/like")
async def like_video(video_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty/invalid body means no user_id
        body = {}
    user_id = (body or {}).get("user_id") or None
    try:
        with _db_lock:
            db = get_db()
            cur = db.execute("SELECT id, likes FROM videos WHERE id = ?", (video_id,))
            video = cur.fetchone()
            if not video:
                return JSONResponse(
                    status_code=404, content={"error": "not_found", "message": "video not found"}
                )
            if user_id:
                cur = db.execute(
                    "INSERT INTO likes (id, video_id, user_id, is_like, created_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT (video_id, user_id) DO NOTHING RETURNING id",
                    (uuid.uuid4().hex, video_id, user_id, now_ms()),
                )
                if cur.fetchone() is None:
                    # Already liked: counter does not move.
                    db.commit()
                    return {"id": video["id"], "likes": video["likes"], "already_liked": True}
            cur = db.execute(
                "UPDATE videos SET likes = likes + 1 WHERE id = ? RETURNING id, likes",
                (video_id,),
            )
            row = cur.fetchone()
            db.commit()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    return clean_row(row)


# -------------------------------------------------------------- channels
# GET /api/channels/:id — one channel row.
@app.get("/api/channels/{channel_id}")
def get_channel(channel_id: str):
    try:
        with _db_lock:
            cur = get_db().execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "channel not found"})
    return clean_row(row)


# GET /api/channels/:id/videos — that channel's videos, newest first.
@app.get("/api/channels/{channel_id}/videos")
def list_channel_videos(channel_id: str, request: Request):
    page, limit, offset = parse_paging(request)
    params: list = [channel_id]
    where = "channel_id = ?"
    if request.query_params.get("visibility"):
        where += " AND visibility = ?"
        params.append(request.query_params["visibility"])
    if request.query_params.get("is_short") == "true":
        where += " AND is_short = 1"
    elif request.query_params.get("is_short") == "false":
        where += " AND is_short = 0"
    if request.query_params.get("category"):
        where += " AND category = ?"
        params.append(request.query_params["category"])
    sql = (
        "SELECT *, COUNT(*) OVER() AS _total FROM videos "
        f"WHERE {where} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?"
    )
    try:
        with _db_lock:
            cur = get_db().execute(sql, params + [limit, offset])
            raw = cur.fetchall()
        total = raw[0]["_total"] if raw else 0
        rows = [clean_row(r) for r in raw]
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    return {"videos": rows, "page": page, "limit": limit, "total": total}


# ---------------------------------------------------------------- stream
# GET /api/stream/:id — play a video straight from the server disk.
# The file lives at ./data/files/videos/..., so there is no download
# or decrypt step: FileResponse serves the plain bytes with HTTP
# Range support (206 Partial Content), which is what ExoPlayer needs
# for seeking. The app uses /api/stream/<video_id> as the video URL.
@app.get("/api/stream/{video_id}")
def stream_video(video_id: str):
    try:
        with _db_lock:
            cur = get_db().execute(
                "SELECT id, title, file_path FROM videos WHERE id = ?", (video_id,)
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video not found"})
    rel_path = (row["file_path"] or "").strip()
    if not rel_path:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "video has no file stored"},
        )
    # file_path is always relative to DATA_DIR and never escapes it.
    dest = (DATA_DIR / rel_path).resolve()
    if not str(dest).startswith(str(DATA_DIR.resolve())):
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video file not found"})
    if not dest.is_file() or dest.stat().st_size == 0:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video file not found"})
    media_type = mimetypes.guess_type(dest.name)[0] or "video/mp4"
    return FileResponse(dest, media_type=media_type, filename=dest.name)


# GET /api/files/{kind}/{filename} — serve a stored file directly
# (thumbnails, and any file the app references by its stored path).
@app.get("/api/files/{kind}/{filename}")
def serve_file(kind: str, filename: str):
    if kind not in ("videos", "thumbnails"):
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "unknown file kind"})
    safe = _safe_filename(filename)
    if safe != filename:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "file not found"})
    dest = (FILES_DIR / kind / safe).resolve()
    if not str(dest).startswith(str(FILES_DIR.resolve())) or not dest.is_file():
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "file not found"})
    media_type = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
    return FileResponse(dest, media_type=media_type, filename=dest.name)


# ---------------------------------------------------------------- upload
class _TooLarge(Exception):
    pass


class _LimitedReader:
    """Wraps a file object, counting bytes and refusing past the limit.
    Used while spooling the incoming upload to a temp file, so a file
    over the 1 GB limit is rejected before anything is stored."""

    def __init__(self, fp, limit: int):
        self._fp = fp
        self.limit = limit
        self.total = 0

    def read(self, size=-1):
        data = self._fp.read(size)
        if data:
            self.total += len(data)
            if self.total > self.limit:
                raise _TooLarge()
        return data

    def __getattr__(self, name):
        return getattr(self._fp, name)


def _safe_filename(name: str) -> str:
    name = (name or "video").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "video"
    return name[:120]


def _parse_bool(value):
    if value is None:
        return None
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _insert_video_row(row: dict):
    """Insert a video row, idempotent per upload_id. Returns the saved
    row (the existing row when the upload_id was already saved)."""
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    sql = (
        f"INSERT INTO videos ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT (upload_id) DO NOTHING RETURNING *"
    )
    with _db_lock:
        db = get_db()
        cur = db.execute(sql, list(row.values()))
        created = cur.fetchone()
        if created is None:
            cur = db.execute("SELECT * FROM videos WHERE upload_id = ?", (row["upload_id"],))
            created = cur.fetchone()
        db.commit()
    return dict(created) if created else None


# POST /api/upload — upload a video (or thumbnail) file to the server.
# multipart/form-data: file=<the file>, kind=<"video" (default) |
# "thumbnail">, upload_id=<client idempotency key, optional>,
# plus optional video metadata for kind=video: title (REQUIRED for
# videos), description, category, channel_id, channel_name,
# owner_uid, duration_sec, is_short, thumbnail_url.
#
# The file is spooled to a temp file (1 GB limit), then moved into
# ./data/files/<kind>/ under a deterministic name
# (<kind>-<upload_id>-<safe name>), so a retried upload never
# creates a duplicate file. For videos, the SQLite row — file_path
# included — is written BEFORE the call returns success.
# Returns 201 { key, file_url, size, content_type, upload_id,
# video_id } — video_id is null for thumbnails. For videos,
# file_url is /api/stream/<video_id> (the playback URL); for
# thumbnails it is /api/files/thumbnails/<name>.
# Limit: 1 GB per file.
@app.post("/api/upload", status_code=201)
def upload_file(
    file: UploadFile = File(...),
    kind: str = Form("video"),
    upload_id: str = Form(None),
    title: str = Form(None),
    description: str = Form(None),
    category: str = Form(None),
    channel_id: str = Form(None),
    channel_name: str = Form(None),
    owner_uid: str = Form(None),
    duration_sec: str = Form(None),
    is_short: str = Form(None),
    thumbnail_url: str = Form(None),
):
    if kind not in ("video", "thumbnail"):
        return JSONResponse(
            status_code=400, content={"error": "validation", "message": "kind must be video or thumbnail"}
        )
    if kind == "video" and (not title or not title.strip()):
        return JSONResponse(
            status_code=400,
            content={
                "error": "validation",
                "message": "title is required for video uploads (the video row is saved with the upload)",
            },
        )
    if upload_id:
        upload_id = re.sub(r"[^A-Za-z0-9_-]", "", upload_id)[:64] or uuid.uuid4().hex
    else:
        upload_id = uuid.uuid4().hex
    ensure_dirs()

    # Replay: this upload_id was already saved — return the stored
    # info instead of uploading again (no duplicate file, no
    # duplicate row).
    if kind == "video":
        try:
            with _db_lock:
                cur = get_db().execute(
                    "SELECT * FROM videos WHERE upload_id = ?", (upload_id,)
                )
                existing = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            return _db_error(e)
        if existing:
            existing = clean_row(existing)
            return JSONResponse(
                status_code=200,
                content={
                    "key": existing.get("file_path", ""),
                    "file_url": existing.get("video_url", ""),
                    "size": existing.get("file_size", 0),
                    "content_type": "video/mp4",
                    "upload_id": upload_id,
                    "video_id": existing.get("id"),
                    "already_uploaded": True,
                },
            )

    content_type = file.content_type or "application/octet-stream"
    prefix = "thumbnails" if kind == "thumbnail" else "videos"
    stored_name = f"{prefix}-{upload_id}-{_safe_filename(file.filename)}"
    dest_dir = THUMBS_DIR if kind == "thumbnail" else VIDEOS_DIR
    dest = dest_dir / stored_name
    rel_path = f"files/{prefix}/{stored_name}"

    # Spool the incoming file to a temp file, enforcing the 1 GB cap.
    tmpdir = tempfile.mkdtemp(prefix="nr-upload-")
    tmp_path = os.path.join(tmpdir, "upload.bin")
    try:
        reader = _LimitedReader(file.file, MAX_UPLOAD_BYTES)
        with open(tmp_path, "wb") as out:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        file_size = reader.total
    except _TooLarge:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(
            status_code=413,
            content={"error": "file_too_large", "message": "file exceeds the 1 GB upload limit"},
        )
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": "upload_error", "message": str(e)})
    finally:
        try:
            file.file.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        # Idempotent file store: if a previous attempt already moved
        # this exact name into place (but failed to save the row),
        # reuse it instead of storing a second copy.
        if dest.exists():
            log.info("upload %s: file already stored, reusing %s", upload_id, stored_name)
        else:
            shutil.move(tmp_path, dest)
            file_size = dest.stat().st_size

        video_id = None
        file_url = f"/api/files/{prefix}/{stored_name}"
        if kind == "video":
            video_id = uuid.uuid4().hex
            file_url = f"/api/stream/{video_id}"
            row = {
                "id": video_id,
                "title": title.strip(),
                "description": description or "",
                "category": category or "",
                "video_url": file_url,
                "file_path": rel_path,
                "thumbnail_url": thumbnail_url or "",
                "channel_id": channel_id or "",
                "channel_name": channel_name or "",
                "owner_uid": owner_uid or "",
                "upload_id": upload_id,
                "file_size": file_size,
                "created_at": now_ms(),
            }
            if duration_sec:
                try:
                    row["duration_sec"] = int(duration_sec)
                except (TypeError, ValueError):
                    pass
            parsed_short = _parse_bool(is_short)
            if parsed_short is None:
                try:
                    row["is_short"] = 1 if int(row.get("duration_sec") or 0) <= SHORT_MAX_SEC else 0
                except (TypeError, ValueError):
                    row["is_short"] = 0
            else:
                row["is_short"] = 1 if parsed_short else 0
            try:
                saved = _insert_video_row(row)
            except Exception as e:  # noqa: BLE001
                # The row could not be saved: remove the file we just
                # stored, so a failed upload leaves nothing behind.
                if dest.exists():
                    dest.unlink()
                return _db_error(e)
            if saved is None:
                if dest.exists():
                    dest.unlink()
                return JSONResponse(
                    status_code=500,
                    content={"error": "db_error", "message": "could not save video"},
                )
            video_id = str(saved["id"])
            file_url = saved["video_url"]

        return {
            "key": rel_path,
            "file_url": file_url,
            "size": file_size,
            "content_type": content_type,
            "upload_id": upload_id,
            "video_id": video_id,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ admin
# No user-facing delete endpoint exists anywhere in this service.
# The only delete is admin-only: POST /api/admin/delete_video with
# header x-admin-key matching the ADMIN_KEY env var, body {video_id}.
# It deletes the metadata row AND the stored file on the server disk.
@app.post("/api/admin/delete_video")
async def admin_delete_video(request: Request):
    if not ADMIN_KEY:
        return JSONResponse(
            status_code=503,
            content={"error": "admin_disabled", "message": "ADMIN_KEY is not set on the server."},
        )
    if request.headers.get("x-admin-key") != ADMIN_KEY:
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "x-admin-key header is missing or wrong"},
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    video_id = (body or {}).get("video_id")
    if not video_id:
        return JSONResponse(status_code=400, content={"error": "validation", "message": "video_id is required"})
    try:
        with _db_lock:
            db = get_db()
            cur = db.execute("SELECT file_path FROM videos WHERE id = ?", (video_id,))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "not_found", "message": "video not found"})
            cur = db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            deleted = cur.rowcount
            db.commit()
    except Exception as e:  # noqa: BLE001
        return _db_error(e)
    if deleted == 0:
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "video not found"})
    # Remove the stored file too (best-effort: the row is already gone).
    rel_path = (row["file_path"] or "").strip()
    if rel_path:
        dest = (DATA_DIR / rel_path).resolve()
        if str(dest).startswith(str(DATA_DIR.resolve())) and dest.is_file():
            try:
                dest.unlink()
            except OSError:
                pass
    return {"deleted": video_id}


# ------------------------------------------------------------------ misc
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(status_code=404, content={"error": "not_found", "message": "unknown endpoint"})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

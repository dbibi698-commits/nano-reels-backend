-- Nano Reels backend — SQLite schema (stdlib sqlite3).
-- The backend runs this file automatically on startup (idempotent:
-- every statement uses IF NOT EXISTS). It is the ONE schema
-- definition; tables are never created anywhere else.
--
-- Tables: videos, channels, comments, likes, subscriptions,
-- watch_history, saved_videos, app_users.
-- created_at / watched_at are epoch milliseconds (bigint), matching
-- the app models. `videos` has a real `is_short` boolean column
-- (stored as INTEGER 0/1 — SQLite has no native boolean type).
-- tags / renditions are stored as JSON text.
-- videos.file_path is the path of the stored file relative to the
-- data dir, e.g. "files/videos/videos-<upload_id>-clip.mp4".
-- Playback uses /api/stream/<id>, which serves that file with
-- HTTP Range support.
--
-- NOTE: there is no auth here. This database is only ever reached
-- through the backend, and the backend enforces visibility /
-- ownership in its queries. Never expose the .db file.

-- ---------------------------------------------------------------- videos
create table if not exists videos (
  id text primary key,
  title text not null default '',
  description text not null default '',
  caption text not null default '',
  category text not null default '',
  tags text not null default '[]',
  visibility text not null default 'public'
    check (visibility in ('public', 'private', 'unlisted')),
  video_url text not null default '',
  thumbnail_url text not null default '',
  file_path text not null default '',
  upload_id text,
  file_size integer not null default 0,
  channel_id text not null default '',
  channel_name text not null default '',
  channel_avatar_url text not null default '',
  owner_uid text not null default '',
  views integer not null default 0,
  likes integer not null default 0,
  dislikes integer not null default 0,
  comments_count integer not null default 0,
  duration_sec integer not null default 0,
  is_short integer not null default 0,
  created_at integer not null default 0,
  renditions text not null default '{}',
  captions_url text not null default ''
);
-- One video row per upload_id (NULLs never conflict in a unique
-- index, so rows saved without an upload_id are unaffected). This
-- is what makes POST /api/videos and POST /api/upload idempotent
-- on retries.
create unique index if not exists videos_upload_id_unique on videos (upload_id);
create index if not exists videos_created_at_idx on videos (created_at desc);
create index if not exists videos_channel_id_idx on videos (channel_id);
create index if not exists videos_owner_uid_idx on videos (owner_uid);
create index if not exists videos_visibility_idx on videos (visibility);
create index if not exists videos_is_short_idx on videos (is_short);

-- -------------------------------------------------------------- channels
create table if not exists channels (
  id text primary key,
  owner_uid text not null default '',
  name text not null default '',
  handle text not null default '',
  bio text not null default '',
  logo_url text not null default '',
  subscribers integer not null default 0,
  created_at integer not null default 0
);
create index if not exists channels_owner_uid_idx on channels (owner_uid);
create unique index if not exists channels_owner_uid_unique on channels (owner_uid);

-- -------------------------------------------------------------- comments
create table if not exists comments (
  id text primary key,
  video_id text not null default '',
  user_id text not null default '',
  user_name text not null default '',
  user_avatar_url text not null default '',
  text text not null default '',
  likes integer not null default 0,
  created_at integer not null default 0
);
create index if not exists comments_video_id_idx on comments (video_id, created_at desc);

-- ------------------------------------------------------------------ likes
-- One row per (user, video). is_like 1 = like, 0 = dislike.
create table if not exists likes (
  id text primary key,
  video_id text not null default '',
  user_id text not null default '',
  is_like integer not null default 1,
  created_at integer not null default 0,
  unique (video_id, user_id)
);
create index if not exists likes_video_id_idx on likes (video_id);

-- ---------------------------------------------------------- subscriptions
create table if not exists subscriptions (
  id text primary key,
  user_id text not null default '',
  channel_id text not null default '',
  created_at integer not null default 0,
  unique (user_id, channel_id)
);
create index if not exists subscriptions_user_id_idx on subscriptions (user_id);
create index if not exists subscriptions_channel_id_idx on subscriptions (channel_id);

-- ---------------------------------------------------------- watch_history
create table if not exists watch_history (
  id text primary key,
  user_id text not null default '',
  video_id text not null default '',
  watched_at integer not null default 0,
  unique (user_id, video_id)
);
create index if not exists watch_history_user_id_idx on watch_history (user_id, watched_at desc);

-- ----------------------------------------------------------- saved_videos
create table if not exists saved_videos (
  id text primary key,
  user_id text not null default '',
  video_id text not null default '',
  created_at integer not null default 0,
  unique (user_id, video_id)
);
create index if not exists saved_videos_user_id_idx on saved_videos (user_id);

-- -------------------------------------------------------------- app_users
create table if not exists app_users (
  uid text primary key,
  email text not null default '',
  display_name text not null default '',
  status text not null default 'ok' check (status in ('ok', 'suspended', 'banned')),
  created_at integer not null default 0
);

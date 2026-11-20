// Nano Reels backend — Node.js + Express + Supabase.
// All videos are stored in Supabase (Postgres `videos` table + Storage
// bucket). This service is the API in front of it.
// There is NO user-facing delete endpoint: videos are never deleted
// from the app. Only the admin endpoint below can delete, and only
// with the ADMIN_KEY env var.
//
// Secrets come ONLY from environment variables. Nothing is hardcoded.
//   SUPABASE_URL              e.g. https://<project-ref>.supabase.co
//   SUPABASE_SERVICE_ROLE_KEY service_role key (server-side only, never
//                             shipped in the app or committed anywhere)
//   ADMIN_KEY                 secret for the admin-only endpoints
//   PORT                      Render sets this; defaults to 3000
//
// Health: GET /api/health works even if Supabase env vars are missing.

const express = require('express');
const cors = require('cors');
const { createClient } = require('@supabase/supabase-js');

const app = express();
app.use(cors());
app.use(express.json({ limit: '1mb' }));

const PORT = process.env.PORT || 3000;
const ADMIN_KEY = process.env.ADMIN_KEY || '';

// Supabase client is created lazily: the server must boot and answer
// /api/health even when SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are
// not set (e.g. a fresh Render deploy before env vars are added).
let supabase = null;
function getSupabase(res) {
  if (supabase) return supabase;
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) {
    if (res) {
      res.status(503).json({
        error: 'backend_not_configured',
        message: 'SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not set on the server.',
      });
    }
    return null;
  }
  supabase = createClient(url, key, { auth: { persistSession: false } });
  return supabase;
}

// ---------------------------------------------------------------- health
// Lightweight keep-alive endpoint. Does NOT touch Supabase, so the
// keep-alive pinger (every 10 min) keeps the free Render instance
// awake without costing database queries.
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok' });
});

// ---------------------------------------------------------------- videos
// Shorts convention: the Supabase `videos` table has no is_short
// column. A video counts as a Short when duration_sec <= 60, so the
// is_short filter is implemented on duration_sec.
const SHORT_MAX_SEC = 60;

function parsePaging(req) {
  const page = Math.max(1, parseInt(req.query.page, 10) || 1);
  const limit = Math.min(100, Math.max(1, parseInt(req.query.limit, 10) || 20));
  return { page, limit, from: (page - 1) * limit, to: page * limit - 1 };
}

function applyVideoFilters(query, req) {
  // Feed shows public videos by default; pass visibility= to override.
  const visibility = req.query.visibility || 'public';
  query = query.eq('visibility', visibility);
  if (req.query.channel_id) query = query.eq('channel_id', req.query.channel_id);
  if (req.query.owner_uid) query = query.eq('owner_uid', req.query.owner_uid);
  if (req.query.is_short === 'true') query = query.lte('duration_sec', SHORT_MAX_SEC);
  if (req.query.is_short === 'false') query = query.gt('duration_sec', SHORT_MAX_SEC);
  if (req.query.category) query = query.eq('category', req.query.category);
  return query;
}

// GET /api/videos?page=&limit=&channel_id=&is_short=&category=&visibility=
// Newest first. Response: { videos, page, limit, total }
app.get('/api/videos', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const { page, limit, from, to } = parsePaging(req);
  let query = db.from('videos').select('*', { count: 'exact' });
  query = applyVideoFilters(query, req);
  const { data, error, count } = await query
    .order('created_at', { ascending: false })
    .range(from, to);
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.json({ videos: data || [], page, limit, total: count || 0 });
});

// GET /api/videos/:id — one video row.
app.get('/api/videos/:id', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const { data, error } = await db
    .from('videos')
    .select('*')
    .eq('id', req.params.id)
    .single();
  if (error || !data) return res.status(404).json({ error: 'not_found', message: 'video not found' });
  res.json(data);
});

// POST /api/videos — create a video metadata row. The app calls this
// AFTER uploading the file to Supabase Storage, passing the public
// video_url / thumbnail_url it got back.
// Required: title, video_url. Optional: description, caption, category,
// tags, visibility, thumbnail_url, channel_id, channel_name,
// channel_avatar_url, owner_uid, duration_sec, renditions, captions_url.
// `is_short` is accepted in the body but is NOT a column in the
// Supabase table — Shorts are derived from duration_sec <= 60.
const VIDEO_FIELDS = [
  'title', 'description', 'caption', 'category', 'tags', 'visibility',
  'video_url', 'thumbnail_url', 'channel_id', 'channel_name',
  'channel_avatar_url', 'owner_uid', 'duration_sec', 'renditions',
  'captions_url',
];
app.post('/api/videos', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const body = req.body || {};
  if (!body.title || typeof body.title !== 'string' || !body.title.trim()) {
    return res.status(400).json({ error: 'validation', message: 'title is required' });
  }
  if (!body.video_url || typeof body.video_url !== 'string' || !body.video_url.trim()) {
    return res.status(400).json({ error: 'validation', message: 'video_url is required' });
  }
  const row = {};
  for (const f of VIDEO_FIELDS) {
    if (body[f] !== undefined) row[f] = body[f];
  }
  if (row.visibility && !['public', 'private', 'unlisted'].includes(row.visibility)) {
    return res.status(400).json({ error: 'validation', message: 'visibility must be public, private or unlisted' });
  }
  if (row.tags && !Array.isArray(row.tags)) {
    return res.status(400).json({ error: 'validation', message: 'tags must be an array of strings' });
  }
  const { data, error } = await db.from('videos').insert(row).select().single();
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.status(201).json(data);
});

// POST /api/videos/:id/view — increment views by 1.
app.post('/api/videos/:id/view', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const { data: video, error: readErr } = await db
    .from('videos')
    .select('id, views')
    .eq('id', req.params.id)
    .single();
  if (readErr || !video) return res.status(404).json({ error: 'not_found', message: 'video not found' });
  const { data, error } = await db
    .from('videos')
    .update({ views: (video.views || 0) + 1 })
    .eq('id', req.params.id)
    .select('id, views')
    .single();
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.json(data);
});

// POST /api/videos/:id/like — increment likes by 1.
// Body: { "user_id": "..." } (optional). With a user_id, one like per
// user is recorded in the `likes` table and the counter only moves on
// the first like from that user. Without user_id it is a plain +1.
app.post('/api/videos/:id/like', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const userId = (req.body && req.body.user_id) || null;
  const { data: video, error: readErr } = await db
    .from('videos')
    .select('id, likes')
    .eq('id', req.params.id)
    .single();
  if (readErr || !video) return res.status(404).json({ error: 'not_found', message: 'video not found' });
  if (userId) {
    const { error: likeErr } = await db
      .from('likes')
      .insert({ video_id: req.params.id, user_id: userId, is_like: true });
    // 23505 = unique violation: this user already liked this video.
    if (likeErr && likeErr.code !== '23505') {
      return res.status(500).json({ error: 'db_error', message: likeErr.message });
    }
    if (likeErr && likeErr.code === '23505') {
      return res.json({ id: video.id, likes: video.likes, already_liked: true });
    }
  }
  const { data, error } = await db
    .from('videos')
    .update({ likes: (video.likes || 0) + 1 })
    .eq('id', req.params.id)
    .select('id, likes')
    .single();
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.json(data);
});

// -------------------------------------------------------------- channels
// GET /api/channels/:id — one channel row.
app.get('/api/channels/:id', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const { data, error } = await db
    .from('channels')
    .select('*')
    .eq('id', req.params.id)
    .single();
  if (error || !data) return res.status(404).json({ error: 'not_found', message: 'channel not found' });
  res.json(data);
});

// GET /api/channels/:id/videos — that channel's videos, newest first.
app.get('/api/channels/:id/videos', async (req, res) => {
  const db = getSupabase(res);
  if (!db) return;
  const { page, limit, from, to } = parsePaging(req);
  let query = db.from('videos').select('*', { count: 'exact' }).eq('channel_id', req.params.id);
  query = applyVideoFilters(query, req);
  const { data, error, count } = await query
    .order('created_at', { ascending: false })
    .range(from, to);
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.json({ videos: data || [], page, limit, total: count || 0 });
});

// ------------------------------------------------------------------ admin
// No user-facing delete endpoint exists anywhere in this service.
// The only delete is admin-only: POST /api/admin/delete_video with
// header x-admin-key matching the ADMIN_KEY env var, body {video_id}.
// It deletes the metadata row only — the Storage file is left alone
// (delete it from the bucket separately if truly needed).
app.post('/api/admin/delete_video', async (req, res) => {
  if (!ADMIN_KEY) {
    return res.status(503).json({ error: 'admin_disabled', message: 'ADMIN_KEY is not set on the server.' });
  }
  if (req.headers['x-admin-key'] !== ADMIN_KEY) {
    return res.status(401).json({ error: 'unauthorized', message: 'x-admin-key header is missing or wrong' });
  }
  const videoId = req.body && req.body.video_id;
  if (!videoId) return res.status(400).json({ error: 'validation', message: 'video_id is required' });
  const db = getSupabase(res);
  if (!db) return;
  const { error } = await db.from('videos').delete().eq('id', videoId);
  if (error) return res.status(500).json({ error: 'db_error', message: error.message });
  res.json({ deleted: videoId });
});

// ------------------------------------------------------------------ misc
app.use((req, res) => res.status(404).json({ error: 'not_found', message: 'unknown endpoint' }));
app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: 'server_error', message: 'unexpected server error' });
});

app.listen(PORT, () => {
  console.log(`nano-reels-backend listening on port ${PORT}`);
  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    console.log('WARNING: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — /api/health works, data endpoints return 503 until they are set.');
  }
});

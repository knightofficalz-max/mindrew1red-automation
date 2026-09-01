require('dotenv').config();
const express  = require('express');
const session  = require('express-session');
const cors     = require('cors');
const path     = require('path');
const Database = require('better-sqlite3');
const { google } = require('googleapis');
const fs       = require('fs');

const app  = express();
const PORT = process.env.PORT || 3000;

// ── SQLite DB ────────────────────────────────────────────────────────────────
const dataDir = '/app/data';
if (!fs.existsSync(dataDir)) fs.mkdirSync(dataDir, { recursive: true });

const db = new Database(path.join(dataDir, 'mindrewired.db'));

// ── Init tables ──────────────────────────────────────────────────────────────
db.exec(`
  CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT UNIQUE,
    file_name   TEXT,
    video_id    TEXT,
    video_url   TEXT,
    title       TEXT,
    description TEXT,
    slot        TEXT,
    is_short    INTEGER,
    duration    REAL,
    uploaded_at TEXT
  );

  CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
  );
`);

// Default settings
const defaults = [
  ['instagram',    ''],
  ['telegram',     ''],
  ['whatsapp',     ''],
  ['youtube',      ''],
  ['custom_links', '[]'],
  ['desc_prompt',  'You are a YouTube SEO expert for Mind Rewired — a motivational psychology channel. Generate powerful, emotionally resonant titles and descriptions.'],
];
const insertDefault = db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)');
for (const [k, v] of defaults) insertDefault.run(k, v);

console.log('✅ DB ready');

// ── Middleware ───────────────────────────────────────────────────────────────
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(session({
  secret:            process.env.SESSION_SECRET || 'mindrewired_secret_key',
  resave:            false,
  saveUninitialized: false,
  cookie:            { secure: false, maxAge: 24 * 60 * 60 * 1000 }
}));
app.use(express.static(path.join(__dirname, 'public')));

// ── Auth middleware ──────────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  if (req.session.authenticated) return next();
  res.status(401).json({ error: 'Unauthorized' });
}

// ── YouTube clients ──────────────────────────────────────────────────────────
function getYouTubeClient() {
  const oauth2 = new google.auth.OAuth2(
    process.env.YT_CLIENT_ID,
    process.env.YT_CLIENT_SECRET,
  );
  oauth2.setCredentials({ refresh_token: process.env.YT_REFRESH_TOKEN });
  return google.youtube({ version: 'v3', auth: oauth2 });
}

function getYTAnalyticsClient() {
  const oauth2 = new google.auth.OAuth2(
    process.env.YT_CLIENT_ID,
    process.env.YT_CLIENT_SECRET,
  );
  oauth2.setCredentials({ refresh_token: process.env.YT_REFRESH_TOKEN });
  return google.youtubeAnalytics({ version: 'v2', auth: oauth2 });
}

// ════════════════════════════════════════════════════════════════════════════
// API ROUTES
// ════════════════════════════════════════════════════════════════════════════

// ── Auth ─────────────────────────────────────────────────────────────────────
app.post('/api/login', (req, res) => {
  const { password } = req.body;
  if (password === process.env.DASHBOARD_PASSWORD) {
    req.session.authenticated = true;
    res.json({ success: true });
  } else {
    res.status(401).json({ error: 'Wrong password' });
  }
});

app.post('/api/logout', (req, res) => {
  req.session.destroy();
  res.json({ success: true });
});

app.get('/api/auth-check', (req, res) => {
  res.json({ authenticated: !!req.session.authenticated });
});

// ── Uploads ──────────────────────────────────────────────────────────────────
app.get('/api/uploads', requireAuth, (req, res) => {
  try {
    const rows = db.prepare('SELECT * FROM uploads ORDER BY uploaded_at DESC').all();
    res.json(rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GitHub Actions posts here
app.post('/api/uploads', (req, res) => {
  const secret = req.headers['x-api-secret'];
  if (secret !== process.env.API_SECRET) return res.status(401).json({ error: 'Unauthorized' });

  const { file_id, file_name, video_id, video_url, title, description, slot, is_short, duration, uploaded_at } = req.body;
  try {
    db.prepare(`
      INSERT OR REPLACE INTO uploads
        (file_id, file_name, video_id, video_url, title, description, slot, is_short, duration, uploaded_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(file_id, file_name, video_id, video_url, title, description, slot, is_short ? 1 : 0, duration, uploaded_at);
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Video management (hide/public/delete/edit) ───────────────────────────────
app.post('/api/video/privacy', requireAuth, async (req, res) => {
  try {
    const { video_id, status } = req.body; // status: 'public' | 'private' | 'unlisted'
    const yt = getYouTubeClient();
    await yt.videos.update({
      part: ['status'],
      requestBody: {
        id: video_id,
        status: { privacyStatus: status }
      }
    });
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/video/delete', requireAuth, async (req, res) => {
  try {
    const { video_id } = req.body;
    const yt = getYouTubeClient();
    await yt.videos.delete({ id: video_id });
    // Remove from local DB too
    db.prepare('DELETE FROM uploads WHERE video_id = ?').run(video_id);
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/video/update', requireAuth, async (req, res) => {
  try {
    const { video_id, title, description, tags } = req.body;
    const yt = getYouTubeClient();
    await yt.videos.update({
      part: ['snippet'],
      requestBody: {
        id: video_id,
        snippet: { title, description, tags, categoryId: '26' }
      }
    });
    // Update local DB
    db.prepare('UPDATE uploads SET title=?, description=? WHERE video_id=?').run(title, description, video_id);
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Settings ──────────────────────────────────────────────────────────────────
app.get('/api/settings', (req, res) => {
  try {
    const rows     = db.prepare('SELECT key, value FROM settings').all();
    const settings = {};
    rows.forEach(r => settings[r.key] = r.value);
    res.json(settings);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/settings', requireAuth, (req, res) => {
  try {
    const stmt = db.prepare('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)');
    for (const [key, value] of Object.entries(req.body)) {
      stmt.run(key, String(value));
    }
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── YouTube Analytics ─────────────────────────────────────────────────────────
app.get('/api/analytics/overview', requireAuth, async (req, res) => {
  try {
    const yt  = getYouTubeClient();
    const channelRes = await yt.channels.list({
      part: ['statistics'],
      mine: true,
    });
    const stats = channelRes.data.items?.[0]?.statistics || {};
    res.json({
      subscribers: parseInt(stats.subscriberCount  || 0),
      totalViews:  parseInt(stats.viewCount         || 0),
      videoCount:  parseInt(stats.videoCount        || 0),
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/analytics/videos', requireAuth, async (req, res) => {
  try {
    const yt = getYouTubeClient();
    const dbRows  = db.prepare('SELECT video_id, title, is_short, uploaded_at FROM uploads ORDER BY uploaded_at DESC LIMIT 20').all();
    const videoIds = dbRows.map(r => r.video_id).filter(Boolean);
    if (!videoIds.length) return res.json([]);

    const ytRes = await yt.videos.list({
      part: ['statistics'],
      id:   videoIds.join(','),
    });
    const ytMap = {};
    ytRes.data.items?.forEach(v => { ytMap[v.id] = v.statistics; });

    const videos = dbRows.map(row => ({
      video_id:    row.video_id,
      title:       row.title,
      is_short:    row.is_short,
      uploaded_at: row.uploaded_at,
      views:       parseInt(ytMap[row.video_id]?.viewCount    || 0),
      likes:       parseInt(ytMap[row.video_id]?.likeCount    || 0),
      comments:    parseInt(ytMap[row.video_id]?.commentCount || 0),
    }));
    res.json(videos);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/analytics/chart', requireAuth, async (req, res) => {
  try {
    const analytics = getYTAnalyticsClient();
    const endDate   = new Date().toISOString().slice(0, 10);
    const startDate = new Date(Date.now() - 28 * 86400000).toISOString().slice(0, 10);
    const result = await analytics.reports.query({
      ids:        'channel==MINE',
      startDate,
      endDate,
      metrics:    'views,likes,subscribersGained',
      dimensions: 'day',
      sort:       'day',
    });
    res.json(result.data.rows || []);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Description builder ───────────────────────────────────────────────────────
app.post('/api/build-description', requireAuth, (req, res) => {
  try {
    const { video_id } = req.body;
    const settings     = {};
    db.prepare('SELECT key, value FROM settings').all().forEach(r => settings[r.key] = r.value);

    const upload = db.prepare('SELECT * FROM uploads WHERE video_id = ?').get(video_id);
    if (!upload) return res.status(404).json({ error: 'Video not found' });

    let desc = upload.description || '';

    const links = [];
    if (settings.instagram) links.push(`📸 Instagram → ${settings.instagram}`);
    if (settings.telegram)  links.push(`✈ Telegram  → ${settings.telegram}`);
    if (settings.whatsapp)  links.push(`💬 WhatsApp  → ${settings.whatsapp}`);
    if (settings.youtube)   links.push(`▶ YouTube   → ${settings.youtube}`);

    try {
      const customs = JSON.parse(settings.custom_links || '[]');
      customs.forEach(l => { if (l.url) links.push(`🔗 ${l.label || 'Link'} → ${l.url}`); });
    } catch {}

    if (links.length) {
      desc += '\n\n────────────────────\n' + links.join('\n') + '\n────────────────────';
    }

    res.json({ description: desc, title: upload.title });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Fallback ──────────────────────────────────────────────────────────────────
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => console.log(`🚀 Server running on port ${PORT}`));

require('dotenv').config();
const express    = require('express');
const session    = require('express-session');
const cors       = require('cors');
const path       = require('path');
const { createClient } = require('@libsql/client/web');
const { google } = require('googleapis');

const app  = express();
const PORT = process.env.PORT || 3000;

// ── Turso DB ──────────────────────────────────────────────────────────────────
const db = createClient({
  url:       process.env.TURSO_URL,
  authToken: process.env.TURSO_TOKEN,
});

// ── Init DB tables ─────────────────────────────────────────────────────────────
async function initDB() {
  await db.execute(`
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
    )
  `);

  await db.execute(`
    CREATE TABLE IF NOT EXISTS settings (
      key   TEXT PRIMARY KEY,
      value TEXT
    )
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

  for (const [key, value] of defaults) {
    await db.execute({
      sql:  'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
      args: [key, value],
    });
  }

  console.log('✅ DB ready');
}

// ── Middleware ─────────────────────────────────────────────────────────────────
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

// ── Auth middleware ────────────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  if (req.session.authenticated) return next();
  res.status(401).json({ error: 'Unauthorized' });
}

// ── YouTube OAuth ──────────────────────────────────────────────────────────────
function getYouTubeClient() {
  const oauth2Client = new google.auth.OAuth2(
    process.env.YT_CLIENT_ID,
    process.env.YT_CLIENT_SECRET,
  );
  oauth2Client.setCredentials({ refresh_token: process.env.YT_REFRESH_TOKEN });
  return google.youtube({ version: 'v3', auth: oauth2Client });
}

function getYouTubeAnalyticsClient() {
  const oauth2Client = new google.auth.OAuth2(
    process.env.YT_CLIENT_ID,
    process.env.YT_CLIENT_SECRET,
  );
  oauth2Client.setCredentials({ refresh_token: process.env.YT_REFRESH_TOKEN });
  return google.youtubeAnalytics({ version: 'v2', auth: oauth2Client });
}

// ══════════════════════════════════════════════════════════════════════════════
// API ROUTES
// ══════════════════════════════════════════════════════════════════════════════

// ── LOGIN ──────────────────────────────────────────────────────────────────────
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

// ── UPLOADS (from Turso) ───────────────────────────────────────────────────────
app.get('/api/uploads', requireAuth, async (req, res) => {
  try {
    const result = await db.execute('SELECT * FROM uploads ORDER BY uploaded_at DESC');
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GitHub Actions calls this to log each upload
app.post('/api/uploads', async (req, res) => {
  const secret = req.headers['x-api-secret'];
  if (secret !== process.env.API_SECRET) return res.status(401).json({ error: 'Unauthorized' });

  const { file_id, file_name, video_id, video_url, title, description, slot, is_short, duration, uploaded_at } = req.body;
  try {
    await db.execute({
      sql:  `INSERT OR REPLACE INTO uploads
             (file_id, file_name, video_id, video_url, title, description, slot, is_short, duration, uploaded_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      args: [file_id, file_name, video_id, video_url, title, description, slot, is_short ? 1 : 0, duration, uploaded_at],
    });
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── SETTINGS ───────────────────────────────────────────────────────────────────
app.get('/api/settings', requireAuth, async (req, res) => {
  try {
    const result = await db.execute('SELECT key, value FROM settings');
    const settings = {};
    result.rows.forEach(r => settings[r.key] = r.value);
    res.json(settings);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/settings', requireAuth, async (req, res) => {
  try {
    const entries = Object.entries(req.body);
    for (const [key, value] of entries) {
      await db.execute({
        sql:  'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        args: [key, String(value)],
      });
    }
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── YOUTUBE ANALYTICS ──────────────────────────────────────────────────────────
app.get('/api/analytics/overview', requireAuth, async (req, res) => {
  try {
    const yt = getYouTubeClient();

    // Channel stats
    const channelRes = await yt.channels.list({
      part: ['statistics', 'snippet'],
      mine: true,
    });
    const stats = channelRes.data.items[0]?.statistics || {};

    res.json({
      subscribers:  parseInt(stats.subscriberCount  || 0),
      totalViews:   parseInt(stats.viewCount         || 0),
      videoCount:   parseInt(stats.videoCount        || 0),
      hiddenSubs:   stats.hiddenSubscriberCount,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/analytics/videos', requireAuth, async (req, res) => {
  try {
    const yt = getYouTubeClient();

    // Get uploads from DB
    const dbResult = await db.execute('SELECT video_id, title, is_short, uploaded_at FROM uploads ORDER BY uploaded_at DESC LIMIT 20');
    const videoIds = dbResult.rows.map(r => r.video_id).filter(Boolean);

    if (!videoIds.length) return res.json([]);

    // YouTube video stats
    const ytRes = await yt.videos.list({
      part: ['statistics', 'snippet'],
      id:   videoIds.join(','),
    });

    const ytMap = {};
    ytRes.data.items?.forEach(v => {
      ytMap[v.id] = v.statistics;
    });

    const videos = dbResult.rows.map(row => ({
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
    const analytics = getYouTubeAnalyticsClient();
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

// ── DESCRIPTION BUILDER ────────────────────────────────────────────────────────
app.post('/api/build-description', requireAuth, async (req, res) => {
  try {
    const { video_id } = req.body;
    const settingsRes  = await db.execute('SELECT key, value FROM settings');
    const settings     = {};
    settingsRes.rows.forEach(r => settings[r.key] = r.value);

    const uploadRes = await db.execute({
      sql:  'SELECT * FROM uploads WHERE video_id = ?',
      args: [video_id],
    });

    const upload = uploadRes.rows[0];
    if (!upload) return res.status(404).json({ error: 'Video not found' });

    let desc = upload.description || '';

    // Add social links
    const links = [];
    if (settings.instagram) links.push(`📸 Instagram → ${settings.instagram}`);
    if (settings.telegram)  links.push(`✈️ Telegram  → ${settings.telegram}`);
    if (settings.whatsapp)  links.push(`💬 WhatsApp  → ${settings.whatsapp}`);
    if (settings.youtube)   links.push(`▶️ YouTube   → ${settings.youtube}`);

    // Custom links
    try {
      const custom = JSON.parse(settings.custom_links || '[]');
      custom.forEach(l => { if (l.url) links.push(`🔗 ${l.label || 'Link'} → ${l.url}`); });
    } catch {}

    if (links.length) {
      desc += '\n\n────────────────────\n' + links.join('\n') + '\n────────────────────';
    }

    res.json({ description: desc, title: upload.title });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Fallback to index.html ─────────────────────────────────────────────────────
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ── START ──────────────────────────────────────────────────────────────────────
initDB().then(() => {
  app.listen(PORT, () => console.log(`🚀 Server running on port ${PORT}`));
});

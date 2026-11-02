CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE videos (video_id TEXT PRIMARY KEY, title TEXT NOT NULL, channel_name TEXT, channel_id TEXT,
  description TEXT, duration_s INTEGER, published_at TEXT, tags_json TEXT, category TEXT, chapters_json TEXT,
  thumbnail_url TEXT, view_count INTEGER, like_count INTEGER, transcript TEXT, first_seen TEXT NOT NULL);
CREATE TABLE plays (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos, queue_item_id INTEGER,
  started_at TEXT NOT NULL, ended_at TEXT, seconds_listened REAL,
  end_reason TEXT CHECK (end_reason IN ('finished','skipped','stopped','error')));
CREATE TABLE ratings (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos,
  liked INTEGER CHECK (liked IN (0,1)), satisfaction INTEGER CHECK (satisfaction BETWEEN 1 AND 10),
  notes TEXT, created_at TEXT NOT NULL,
  CHECK (liked IS NOT NULL OR satisfaction IS NOT NULL OR notes IS NOT NULL));
CREATE TABLE agent_requests (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('user','morning')), play TEXT NOT NULL CHECK (play IN ('now','end')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  summary TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE queue_items (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos, position REAL NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('agent','user','morning','fallback')), reason TEXT,
  agent_request_id INTEGER REFERENCES agent_requests,
  status TEXT NOT NULL CHECK (status IN ('queued','playing','played','removed')), added_at TEXT NOT NULL);
CREATE TABLE taste_profiles (id INTEGER PRIMARY KEY, summary TEXT NOT NULL, likes_json TEXT NOT NULL,
  dislikes_json TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('agent','user','legacy')), created_at TEXT NOT NULL);
CREATE TABLE taste_corrections (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
CREATE TABLE morning_settings (id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL, wake_time TEXT NOT NULL,
  days_json TEXT NOT NULL, topics_json TEXT NOT NULL, count INTEGER NOT NULL);
INSERT INTO morning_settings VALUES (1, 1, '07:45', '[0,1,2,3,4,5,6]',
  '["technology","AI","hardware","engineering","news/politics"]', 3);
CREATE TABLE account_imports (id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('subscription','liked','playlist','history','recommended')),
  video_id TEXT, channel_id TEXT, title TEXT, imported_at TEXT NOT NULL);
CREATE TABLE api_quota (day TEXT PRIMARY KEY, units INTEGER NOT NULL);
CREATE TABLE status_events (id INTEGER PRIMARY KEY, component TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('info','warn','error')), message TEXT NOT NULL, created_at TEXT NOT NULL);

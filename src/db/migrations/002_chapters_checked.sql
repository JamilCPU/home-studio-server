-- 1 once ensure_chapters looked a video up (chapters_json '[]' alone can't tell
-- "checked, none" from "never checked", since upsert_video writes '[]' for no chapters).
ALTER TABLE videos ADD COLUMN chapters_checked INTEGER NOT NULL DEFAULT 0;

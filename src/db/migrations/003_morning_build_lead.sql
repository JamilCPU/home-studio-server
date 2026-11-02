-- Minutes before wake time that the morning build starts (Ruling R15), 1-60.
ALTER TABLE morning_settings ADD COLUMN build_lead_min INTEGER NOT NULL DEFAULT 5
  CHECK (build_lead_min BETWEEN 1 AND 60);

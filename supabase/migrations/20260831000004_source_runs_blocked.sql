-- Two columns the orchestrator's policy needs to tell failures apart.
--
-- blocked: a source that was rate-limited into silence reported the same
-- zero as a source that genuinely found nothing. Common Crawl refuses
-- connections outright rather than answering 429, so this distinction is the
-- difference between "heal it" and "leave it alone".
--
-- skipped: a source that never ran (no API key, not available) is not a
-- regression and must not trigger a healer.

ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS blocked integer DEFAULT 0;
ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS skipped text;

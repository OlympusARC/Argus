-- Region, derived from the free-text location by classify/geo.py.
--
-- Stored rather than computed on read for the same reason role_family is:
-- the gazetteer is Python, so the alternative to a column is fetching every
-- row and deciding in the application.
--
-- Nullable. A row written before this column existed has NULL, which means
-- "never computed" -- a different thing from 'unknown', which is a decision
-- the gazetteer reached about a location it could not place.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS region TEXT;

-- Ordered by posted_at because that is the dashboard's default sort, so a
-- region filter and the ordering ride one index.
CREATE INDEX IF NOT EXISTS idx_jobs_region ON jobs (region, status, posted_at DESC);

UPDATE meta SET value = '11' WHERE key = 'schema_version';

-- The index last_sync() wants.
--
-- last_sync(conn, kind) reads "the newest sync_runs row of this kind", and the
-- draft loop prunes older rows of one kind on every poll. Both are
-- (kind, id DESC) lookups, and a five-second poll doing a full table scan for
-- each of them is the kind of thing nobody notices until the table is large.
--
-- (kind, id) not (id, kind): the leading column has to be the one being
-- filtered, or the index cannot serve "latest run of kind X" at all.
CREATE INDEX idx_sync_runs_kind_id ON sync_runs (kind, id);

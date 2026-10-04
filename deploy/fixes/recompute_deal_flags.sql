-- B42 (one-off): a deal's product badges follow its tracker lines.
--
-- The register now keeps deals.is_lending / is_syndication / is_asset_mon in
-- step with the lines on every line write (app/api/deal_flags.py). This brings
-- the EXISTING book to the same rule. Dry run by default; apply with
--   psql ... -v apply=1 -f recompute_deal_flags.sql
\if :{?apply}
\else
\set apply 0
\endif

\echo '---- Deals whose flags disagree with their live lines (before)'
SELECT d.deal_no, e.code, d.is_lending, d.is_syndication, d.is_asset_mon,
       EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS has_lending,
       EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS has_syndication,
       EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS has_asset_mon
  FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL
   AND (   d.is_lending     <> EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_syndication <> EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_asset_mon   <> EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL))
 ORDER BY e.code;

\if :apply
BEGIN;
UPDATE deals d
   SET is_lending     = EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       is_syndication = EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       is_asset_mon   = EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       updated_at = now(), updated_by = 'fix:recompute_deal_flags'
 WHERE d.deleted_at IS NULL
   AND (   d.is_lending     <> EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_syndication <> EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_asset_mon   <> EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL));
COMMIT;
\echo '---- applied; mismatches remaining:'
SELECT count(*) FROM deals d WHERE d.deleted_at IS NULL
   AND (   d.is_lending     <> EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_syndication <> EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_asset_mon   <> EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL));
\else
\echo '(dry run — nothing changed; re-run with -v apply=1 to write)'
\endif

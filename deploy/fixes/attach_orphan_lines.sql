-- B05 (one-off): tracker lines with no deal behind them.
--
-- A lending / syndication / asset-monetisation line that carries no deal_id is
-- invisible to the Deals grid and to the product badges. Where the company has
-- EXACTLY ONE live deal, the line is attached to it (and the deal's badges
-- recomputed). Where it has none or several, the line is listed for the desk:
-- open the company in Deals and use Add product, or pick the deal by hand.
-- Dry run by default; apply with -v apply=1.
\if :{?apply}
\else
\set apply 0
\endif

CREATE TEMP VIEW orphan_lines AS
  SELECT 'lending' AS kind, t.id, t.tracker_no AS no, t.entity_id, e.code, t.stage AS state
    FROM lending_tracker t JOIN entities e ON e.id = t.entity_id
   WHERE t.deleted_at IS NULL AND t.deal_id IS NULL
  UNION ALL
  SELECT 'syndication', t.id, t.tracker_no, t.entity_id, e.code, t.status
    FROM syndication_tracker t JOIN entities e ON e.id = t.entity_id
   WHERE t.deleted_at IS NULL AND t.deal_id IS NULL
  UNION ALL
  SELECT 'asset_monetisation', t.id, NULL, t.entity_id, e.code, t.status
    FROM asset_monetisation t JOIN entities e ON e.id = t.entity_id
   WHERE t.deleted_at IS NULL AND t.deal_id IS NULL;

CREATE TEMP VIEW orphan_plan AS
  SELECT o.*, (SELECT count(*) FROM deals d WHERE d.entity_id = o.entity_id AND d.deleted_at IS NULL) AS live_deals,
         (SELECT d.id FROM deals d WHERE d.entity_id = o.entity_id AND d.deleted_at IS NULL
           ORDER BY d.created_at LIMIT 1) AS deal_id
    FROM orphan_lines o;

\echo '---- Orphan lines that WILL be attached (company has exactly one live deal)'
SELECT kind, no, code, state FROM orphan_plan WHERE live_deals = 1 ORDER BY code, kind;
\echo '---- Orphan lines the DESK must place (no live deal, or several)'
SELECT kind, no, code, state, live_deals FROM orphan_plan WHERE live_deals <> 1 ORDER BY code, kind;

\if :apply
BEGIN;
UPDATE lending_tracker t SET deal_id = p.deal_id, updated_at = now(), updated_by = 'fix:attach_orphan_lines'
  FROM orphan_plan p WHERE p.kind = 'lending' AND p.live_deals = 1 AND t.id = p.id;
UPDATE syndication_tracker t SET deal_id = p.deal_id, updated_at = now(), updated_by = 'fix:attach_orphan_lines'
  FROM orphan_plan p WHERE p.kind = 'syndication' AND p.live_deals = 1 AND t.id = p.id;
UPDATE asset_monetisation t SET deal_id = p.deal_id, updated_at = now(), updated_by = 'fix:attach_orphan_lines'
  FROM orphan_plan p WHERE p.kind = 'asset_monetisation' AND p.live_deals = 1 AND t.id = p.id;
-- the badges of every deal that just gained a line
UPDATE deals d
   SET is_lending     = EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       is_syndication = EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       is_asset_mon   = EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL),
       updated_at = now(), updated_by = 'fix:attach_orphan_lines'
 WHERE d.deleted_at IS NULL AND d.id IN (SELECT deal_id FROM orphan_plan WHERE live_deals = 1);
COMMIT;
\echo '---- applied; orphan lines remaining:'
SELECT count(*) FROM orphan_lines;
\else
\echo '(dry run — nothing changed; re-run with -v apply=1 to write)'
\endif

-- PRISM register tally: Leads, Deals, Lending, Syndication, Asset Monetisation
-- against the Client Master, and every way their entries can disagree.
--
-- Read-only. Run with deploy/reconcile.sh on the box (it finds the compose
-- project and pipes this into the register database), or by hand:
--   docker compose -p <project> -f prism/deploy/compose/docker-compose.yml \
--     exec -T postgres psql -U prism -d register -v ON_ERROR_STOP=1 < prism/deploy/reconcile.sql
--
-- "live" = not soft-deleted (deleted_at IS NULL). Rows an import parked with
-- reconciliation_status Required/Waived are hidden from the desk, so they are
-- counted apart. A section that prints "(0 rows)" is clean.

\pset border 1
\pset footer on
\set ON_ERROR_STOP on

\echo
\echo '==================== 1. HEADLINE COUNTS (live rows) ===================='
SELECT 'Client master (entities)'       AS register, count(*) AS live,
       count(*) FILTER (WHERE entity_type <> 'Company') AS non_company,
       NULL::bigint AS hidden_by_import
  FROM entities WHERE deleted_at IS NULL
UNION ALL
SELECT 'Leads', count(*), NULL, NULL FROM leads WHERE deleted_at IS NULL
UNION ALL
SELECT 'Deals', count(*), NULL,
       count(*) FILTER (WHERE reconciliation_status IN ('Required','Waived'))
  FROM deals WHERE deleted_at IS NULL
UNION ALL
SELECT 'Lending tracker', count(*), NULL,
       count(*) FILTER (WHERE reconciliation_status IN ('Required','Waived'))
  FROM lending_tracker WHERE deleted_at IS NULL
UNION ALL
SELECT 'Syndication tracker', count(*), NULL,
       count(*) FILTER (WHERE reconciliation_status IN ('Required','Waived'))
  FROM syndication_tracker WHERE deleted_at IS NULL
UNION ALL
SELECT 'Asset monetisation', count(*), NULL,
       count(*) FILTER (WHERE reconciliation_status IN ('Required','Waived'))
  FROM asset_monetisation WHERE deleted_at IS NULL;

\echo
\echo '---- Leads by status'
SELECT status, count(*) FROM leads WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC;
\echo '---- Deals by stage'
SELECT coalesce(stage,'(blank)') AS stage, count(*) FROM deals WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC;
\echo '---- Lending by stage'
SELECT coalesce(stage,'(blank)') AS stage, count(*), round(sum(amount_cr)::numeric,2) AS amount_cr
  FROM lending_tracker WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC;
\echo '---- Syndication by status'
SELECT coalesce(status,'(blank)') AS status, count(*), round(sum(amount_cr)::numeric,2) AS amount_cr
  FROM syndication_tracker WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC;
\echo '---- Asset monetisation by status'
SELECT coalesce(status,'(blank)') AS status, count(*) FROM asset_monetisation WHERE deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC;

\echo
\echo '==================== 2. CLIENT COUNT TALLY ===================='
\echo 'How many clients each register actually touches, against the client master.'
WITH e AS (SELECT id FROM entities WHERE deleted_at IS NULL)
SELECT 'Clients in the master'                            AS measure, count(*) AS clients FROM e
UNION ALL
SELECT 'Clients with at least one deal',
       count(DISTINCT d.entity_id) FROM deals d JOIN e ON e.id = d.entity_id WHERE d.deleted_at IS NULL
UNION ALL
SELECT 'Clients with a lending line',
       count(DISTINCT t.entity_id) FROM lending_tracker t JOIN e ON e.id = t.entity_id WHERE t.deleted_at IS NULL
UNION ALL
SELECT 'Clients with a syndication line',
       count(DISTINCT t.entity_id) FROM syndication_tracker t JOIN e ON e.id = t.entity_id WHERE t.deleted_at IS NULL
UNION ALL
SELECT 'Clients with an asset monetisation line',
       count(DISTINCT t.entity_id) FROM asset_monetisation t JOIN e ON e.id = t.entity_id WHERE t.deleted_at IS NULL
UNION ALL
SELECT 'Clients with a linked lead',
       count(DISTINCT l.entity_id) FROM leads l JOIN e ON e.id = l.entity_id WHERE l.deleted_at IS NULL
UNION ALL
SELECT 'Clients with a deal OR any tracker line OR a lead',
       count(DISTINCT x.entity_id) FROM (
         SELECT entity_id FROM deals WHERE deleted_at IS NULL
         UNION SELECT entity_id FROM lending_tracker WHERE deleted_at IS NULL
         UNION SELECT entity_id FROM syndication_tracker WHERE deleted_at IS NULL
         UNION SELECT entity_id FROM asset_monetisation WHERE deleted_at IS NULL
         UNION SELECT entity_id FROM leads WHERE deleted_at IS NULL AND entity_id IS NOT NULL) x
       JOIN e ON e.id = x.entity_id
UNION ALL
SELECT 'Clients with NOTHING against them (master only)',
       count(*) FROM e WHERE NOT EXISTS (SELECT 1 FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL)
                    AND NOT EXISTS (SELECT 1 FROM lending_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
                    AND NOT EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
                    AND NOT EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
                    AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.entity_id = e.id AND l.deleted_at IS NULL);

\echo
\echo '---- 2a. Clients with nothing against them (who they are)'
SELECT e.code, e.legal_name, e.register_status, e.lifecycle, e.created_by, e.created_at::date
  FROM entities e
 WHERE e.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM lending_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.entity_id = e.id AND t.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.entity_id = e.id AND l.deleted_at IS NULL)
 ORDER BY e.legal_name;

\echo
\echo '==================== 3. CLIENT MASTER: DUPLICATES ===================='
\echo '---- 3a. Same name more than once (case/space-insensitive)'
SELECT lower(regexp_replace(legal_name, '\s+', ' ', 'g')) AS name_key, count(*) AS rows_,
       string_agg(code || ' (' || coalesce(register_status,'-') || ')', ', ' ORDER BY created_at) AS codes
  FROM entities WHERE deleted_at IS NULL
 GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC, 1;
\echo '---- 3b. Same CIN more than once'
SELECT cin, count(*) AS rows_, string_agg(code || ' · ' || legal_name, ' | ' ORDER BY created_at) AS entities
  FROM entities WHERE deleted_at IS NULL AND cin IS NOT NULL AND cin <> ''
 GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC;
\echo '---- 3c. Same code more than once among live rows (should be impossible)'
SELECT code, count(*) FROM entities WHERE deleted_at IS NULL GROUP BY 1 HAVING count(*) > 1;

\echo
\echo '==================== 4. LEADS vs CLIENT MASTER ===================='
\echo '---- 4a. Converted leads with no deal behind them'
SELECT l.lead_no, l.company, l.status, l.rm, l.entity_id IS NOT NULL AS has_entity, l.converted_deal_id
  FROM leads l LEFT JOIN deals d ON d.id = l.converted_deal_id AND d.deleted_at IS NULL
 WHERE l.deleted_at IS NULL AND l.status = 'Converted' AND d.id IS NULL
 ORDER BY l.lead_no;
\echo '---- 4b. Leads pointing at a client that is deleted or missing'
SELECT l.lead_no, l.company, l.status, l.entity_id
  FROM leads l LEFT JOIN entities e ON e.id = l.entity_id AND e.deleted_at IS NULL
 WHERE l.deleted_at IS NULL AND l.entity_id IS NOT NULL AND e.id IS NULL
 ORDER BY l.lead_no;
\echo '---- 4c. Leads NOT linked to a client although a client of that name exists'
SELECT l.lead_no, l.company, l.status, e.code AS matching_client, e.legal_name
  FROM leads l
  JOIN entities e ON e.deleted_at IS NULL
   AND lower(regexp_replace(e.legal_name, '\s+', ' ', 'g')) = lower(regexp_replace(l.company, '\s+', ' ', 'g'))
 WHERE l.deleted_at IS NULL AND l.entity_id IS NULL
 ORDER BY l.lead_no;
\echo '---- 4d. Linked leads whose company name differs from the client master name'
SELECT l.lead_no, l.company AS lead_company, e.code, e.legal_name AS master_name, l.status
  FROM leads l JOIN entities e ON e.id = l.entity_id
 WHERE l.deleted_at IS NULL AND e.deleted_at IS NULL
   AND lower(regexp_replace(l.company, '\s+', ' ', 'g')) <> lower(regexp_replace(e.legal_name, '\s+', ' ', 'g'))
   AND lower(regexp_replace(l.company, '\s+', ' ', 'g')) <> lower(regexp_replace(coalesce(e.display_name,''), '\s+', ' ', 'g'))
 ORDER BY l.lead_no;
\echo '---- 4e. The same company with more than one live lead'
SELECT lower(regexp_replace(company, '\s+', ' ', 'g')) AS company_key, count(*) AS leads_,
       string_agg(lead_no || ' (' || status || ')', ', ' ORDER BY lead_no) AS lead_nos
  FROM leads WHERE deleted_at IS NULL GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC;
\echo '---- 4f. Active leads whose client already has a live deal (should they be Converted?)'
SELECT l.lead_no, l.company, l.status, d.deal_no, d.stage
  FROM leads l JOIN deals d ON d.entity_id = l.entity_id AND d.deleted_at IS NULL
 WHERE l.deleted_at IS NULL AND l.status = 'Active' AND l.converted_deal_id IS NULL
 ORDER BY l.lead_no;

\echo
\echo '==================== 5. DEALS vs CLIENT MASTER ===================='
\echo '---- 5a. Deals whose client is deleted or missing'
SELECT d.deal_no, d.entity_id, d.stage FROM deals d
  LEFT JOIN entities e ON e.id = d.entity_id AND e.deleted_at IS NULL
 WHERE d.deleted_at IS NULL AND e.id IS NULL ORDER BY d.deal_no;
\echo '---- 5b. Deal number does not carry the client code (deal_no should be <code> or <code>-n)'
SELECT d.deal_no, e.code, e.legal_name FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL AND e.deleted_at IS NULL
   AND (d.deal_no IS NULL OR (d.deal_no <> e.code AND d.deal_no NOT LIKE e.code || '-%'))
 ORDER BY e.code;
\echo '---- 5c. Deal flags vs the tracker lines behind them'
SELECT d.deal_no, e.legal_name,
       d.is_lending, (SELECT count(*) FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS lending_lines,
       d.is_syndication, (SELECT count(*) FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS syn_lines,
       d.is_asset_mon, (SELECT count(*) FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL) AS am_lines
  FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL
   AND (   d.is_lending     <> EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_syndication <> EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
        OR d.is_asset_mon   <> EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL))
 ORDER BY d.deal_no;
\echo '---- 5d. Deals with no product line at all'
SELECT d.deal_no, e.legal_name, d.stage, d.product_type FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
   AND NOT EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL)
 ORDER BY d.deal_no;
\echo '---- 5e. Clients with more than one live deal (fine if they came back for a second facility)'
SELECT e.code, e.legal_name, count(*) AS deals_, string_agg(d.deal_no || ' (' || coalesce(d.stage,'-') || ')', ', ') AS deal_nos
  FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL AND e.deleted_at IS NULL GROUP BY 1,2 HAVING count(*) > 1 ORDER BY 3 DESC;

\echo
\echo '==================== 6. TRACKER LINES vs DEALS AND CLIENTS ===================='
\echo '---- 6a. Lines whose client is deleted or missing'
SELECT 'lending' AS tracker, t.tracker_no, t.entity_id FROM lending_tracker t
  LEFT JOIN entities e ON e.id = t.entity_id AND e.deleted_at IS NULL WHERE t.deleted_at IS NULL AND e.id IS NULL
UNION ALL
SELECT 'syndication', t.tracker_no, t.entity_id FROM syndication_tracker t
  LEFT JOIN entities e ON e.id = t.entity_id AND e.deleted_at IS NULL WHERE t.deleted_at IS NULL AND e.id IS NULL
UNION ALL
SELECT 'asset_mon', t.tracker_no, t.entity_id FROM asset_monetisation t
  LEFT JOIN entities e ON e.id = t.entity_id AND e.deleted_at IS NULL WHERE t.deleted_at IS NULL AND e.id IS NULL
ORDER BY 1, 2;
\echo '---- 6b. Lines with no deal behind them'
SELECT 'lending' AS tracker, t.tracker_no, e.legal_name, t.stage AS stage_or_status FROM lending_tracker t
  JOIN entities e ON e.id = t.entity_id WHERE t.deleted_at IS NULL AND t.deal_id IS NULL
UNION ALL
SELECT 'syndication', t.tracker_no, e.legal_name, t.status FROM syndication_tracker t
  JOIN entities e ON e.id = t.entity_id WHERE t.deleted_at IS NULL AND t.deal_id IS NULL
UNION ALL
SELECT 'asset_mon', t.tracker_no, e.legal_name, t.status FROM asset_monetisation t
  JOIN entities e ON e.id = t.entity_id WHERE t.deleted_at IS NULL AND t.deal_id IS NULL
ORDER BY 1, 2;
\echo '---- 6c. Lines whose deal belongs to a DIFFERENT client than the line (cross-link)'
SELECT 'lending' AS tracker, t.tracker_no, el.legal_name AS line_client, d.deal_no, ed.legal_name AS deal_client
  FROM lending_tracker t JOIN deals d ON d.id = t.deal_id
  JOIN entities el ON el.id = t.entity_id JOIN entities ed ON ed.id = d.entity_id
 WHERE t.deleted_at IS NULL AND t.entity_id <> d.entity_id
UNION ALL
SELECT 'syndication', t.tracker_no, el.legal_name, d.deal_no, ed.legal_name
  FROM syndication_tracker t JOIN deals d ON d.id = t.deal_id
  JOIN entities el ON el.id = t.entity_id JOIN entities ed ON ed.id = d.entity_id
 WHERE t.deleted_at IS NULL AND t.entity_id <> d.entity_id
UNION ALL
SELECT 'asset_mon', t.tracker_no, el.legal_name, d.deal_no, ed.legal_name
  FROM asset_monetisation t JOIN deals d ON d.id = t.deal_id
  JOIN entities el ON el.id = t.entity_id JOIN entities ed ON ed.id = d.entity_id
 WHERE t.deleted_at IS NULL AND t.entity_id <> d.entity_id
ORDER BY 1, 2;
\echo '---- 6d. Lines whose deal is deleted'
SELECT 'lending' AS tracker, t.tracker_no, t.deal_id FROM lending_tracker t JOIN deals d ON d.id = t.deal_id
 WHERE t.deleted_at IS NULL AND d.deleted_at IS NOT NULL
UNION ALL
SELECT 'syndication', t.tracker_no, t.deal_id FROM syndication_tracker t JOIN deals d ON d.id = t.deal_id
 WHERE t.deleted_at IS NULL AND d.deleted_at IS NOT NULL
UNION ALL
SELECT 'asset_mon', t.tracker_no, t.deal_id FROM asset_monetisation t JOIN deals d ON d.id = t.deal_id
 WHERE t.deleted_at IS NULL AND d.deleted_at IS NOT NULL
ORDER BY 1, 2;
\echo '---- 6e. Lines hidden from the desk by an import (reconciliation_status Required/Waived)'
SELECT 'lending' AS tracker, t.tracker_no, e.legal_name, t.reconciliation_status FROM lending_tracker t JOIN entities e ON e.id = t.entity_id
 WHERE t.deleted_at IS NULL AND t.reconciliation_status IN ('Required','Waived')
UNION ALL
SELECT 'syndication', t.tracker_no, e.legal_name, t.reconciliation_status FROM syndication_tracker t JOIN entities e ON e.id = t.entity_id
 WHERE t.deleted_at IS NULL AND t.reconciliation_status IN ('Required','Waived')
UNION ALL
SELECT 'asset_mon', t.tracker_no, e.legal_name, t.reconciliation_status FROM asset_monetisation t JOIN entities e ON e.id = t.entity_id
 WHERE t.deleted_at IS NULL AND t.reconciliation_status IN ('Required','Waived')
UNION ALL
SELECT 'deal', d.deal_no, e.legal_name, d.reconciliation_status FROM deals d JOIN entities e ON e.id = d.entity_id
 WHERE d.deleted_at IS NULL AND d.reconciliation_status IN ('Required','Waived')
ORDER BY 1, 2;
\echo '---- 6f. Duplicate tracker numbers among live lines'
SELECT 'lending' AS tracker, tracker_no, count(*) FROM lending_tracker WHERE deleted_at IS NULL GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'syndication', tracker_no, count(*) FROM syndication_tracker WHERE deleted_at IS NULL GROUP BY 2 HAVING count(*) > 1
UNION ALL
SELECT 'asset_mon', tracker_no, count(*) FROM asset_monetisation WHERE deleted_at IS NULL GROUP BY 2 HAVING count(*) > 1
ORDER BY 1, 2;
\echo '---- 6g. Syndication lines with lender rows adding up to more than the line (over-allocation)'
SELECT t.tracker_no, e.legal_name, t.amount_cr AS line_cr,
       round(sum(sl.amount_cr)::numeric, 2) AS lenders_cr, count(sl.id) AS lenders
  FROM syndication_tracker t JOIN entities e ON e.id = t.entity_id
  JOIN syndication_lenders sl ON sl.syndication_id = t.id AND sl.deleted_at IS NULL
 WHERE t.deleted_at IS NULL AND t.amount_cr IS NOT NULL
 GROUP BY 1,2,3 HAVING sum(sl.amount_cr) > t.amount_cr ORDER BY 1;

\echo
\echo '==================== 7. PER-CLIENT TALLY (every live client) ===================='
\echo 'leads/deals/lines are live-row counts; the three "flag" columns compare deal flags with the lines behind them.'
SELECT e.code, left(e.legal_name, 42) AS client, e.register_status AS reg_status, e.lifecycle,
       (SELECT count(*) FROM leads l WHERE l.entity_id = e.id AND l.deleted_at IS NULL) AS leads,
       (SELECT count(*) FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL) AS deals,
       (SELECT count(*) FROM lending_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL) AS lending,
       (SELECT count(*) FROM syndication_tracker t WHERE t.entity_id = e.id AND t.deleted_at IS NULL) AS syn,
       (SELECT count(*) FROM asset_monetisation t WHERE t.entity_id = e.id AND t.deleted_at IS NULL) AS am,
       CASE WHEN EXISTS (SELECT 1 FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL
                           AND d.is_lending <> EXISTS (SELECT 1 FROM lending_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL))
            THEN 'x' ELSE '' END AS lend_flag_mismatch,
       CASE WHEN EXISTS (SELECT 1 FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL
                           AND d.is_syndication <> EXISTS (SELECT 1 FROM syndication_tracker t WHERE t.deal_id = d.id AND t.deleted_at IS NULL))
            THEN 'x' ELSE '' END AS syn_flag_mismatch,
       CASE WHEN EXISTS (SELECT 1 FROM deals d WHERE d.entity_id = e.id AND d.deleted_at IS NULL
                           AND d.is_asset_mon <> EXISTS (SELECT 1 FROM asset_monetisation t WHERE t.deal_id = d.id AND t.deleted_at IS NULL))
            THEN 'x' ELSE '' END AS am_flag_mismatch
  FROM entities e
 WHERE e.deleted_at IS NULL
 ORDER BY e.legal_name;

\echo
\echo '==================== 8. SOFT-DELETED ROWS (not counted above) ===================='
SELECT 'entities' AS tbl, count(*) FROM entities WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'leads', count(*) FROM leads WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'deals', count(*) FROM deals WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'lending_tracker', count(*) FROM lending_tracker WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'syndication_tracker', count(*) FROM syndication_tracker WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'asset_monetisation', count(*) FROM asset_monetisation WHERE deleted_at IS NOT NULL;
\echo
\echo 'Done. Sections that print "(0 rows)" are clean.'

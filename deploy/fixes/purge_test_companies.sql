-- B03 (one-off): take the TEST companies out of the production book.
--
-- "Bangalore Test Company" / "Chennai Test ..." are real register rows — a
-- client, leads, a deal, a mandate with lender rows, interactions — and every
-- dashboard number counts them. This SOFT-deletes (deleted_at = now(), the
-- register's own delete) every row that belongs to a company whose name
-- matches the pattern, so the rows stay in the database for the audit trail
-- and can be restored. Dry run by default; apply with
--   psql ... -v apply=1 -v pattern="'%test company%'" -f purge_test_companies.sql
-- The default pattern is '%test compan%' (matches "Test Company", "Test Companies");
-- check the dry-run list BEFORE applying — it names every row.
\if :{?apply}
\else
\set apply 0
\endif
\if :{?pattern}
\else
\set pattern '''%test compan%'''
\endif

CREATE TEMP VIEW test_entities AS
  SELECT id, code, legal_name FROM entities
   WHERE deleted_at IS NULL
     AND (lower(legal_name) LIKE lower(:pattern) OR lower(coalesce(display_name,'')) LIKE lower(:pattern));

\echo '---- Companies matched'
SELECT code, legal_name FROM test_entities ORDER BY code;
\echo '---- Rows that would be soft-deleted'
SELECT 'leads' AS tbl, count(*) FROM leads WHERE deleted_at IS NULL AND (entity_id IN (SELECT id FROM test_entities) OR lower(company) LIKE lower(:pattern))
UNION ALL SELECT 'deals', count(*) FROM deals WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'lending_tracker', count(*) FROM lending_tracker WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'syndication_tracker', count(*) FROM syndication_tracker WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'syndication_lenders', count(*) FROM syndication_lenders WHERE deleted_at IS NULL AND syndication_id IN (SELECT id FROM syndication_tracker WHERE entity_id IN (SELECT id FROM test_entities))
UNION ALL SELECT 'asset_monetisation', count(*) FROM asset_monetisation WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'interactions', count(*) FROM interactions WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'documents', count(*) FROM documents WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities)
UNION ALL SELECT 'prospects', count(*) FROM prospects WHERE deleted_at IS NULL AND (entity_id IN (SELECT id FROM test_entities) OR lower(name) LIKE lower(:pattern))
UNION ALL SELECT 'entities', count(*) FROM test_entities;

\if :apply
BEGIN;
UPDATE syndication_lenders SET deleted_at = now(), updated_by = 'fix:purge_test_companies'
 WHERE deleted_at IS NULL AND syndication_id IN (SELECT id FROM syndication_tracker WHERE entity_id IN (SELECT id FROM test_entities));
UPDATE syndication_tracker SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE lending_tracker SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE asset_monetisation SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE interactions SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE documents SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE deals SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND entity_id IN (SELECT id FROM test_entities);
UPDATE leads SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND (entity_id IN (SELECT id FROM test_entities) OR lower(company) LIKE lower(:pattern));
UPDATE prospects SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND (entity_id IN (SELECT id FROM test_entities) OR lower(name) LIKE lower(:pattern));
UPDATE entities SET deleted_at = now(), updated_by = 'fix:purge_test_companies' WHERE deleted_at IS NULL AND id IN (SELECT id FROM test_entities);
COMMIT;
\echo '---- applied; live companies still matching:'
SELECT count(*) FROM entities WHERE deleted_at IS NULL AND lower(legal_name) LIKE lower(:pattern);
\else
\echo '(dry run — nothing changed; re-run with -v apply=1 to write)'
\endif

\set ON_ERROR_STOP on
\connect access
INSERT INTO users (id, tenant_id, email, full_name, short_name)
SELECT '10000000-0000-0000-0000-000000000001', id,
       'e2e.rm@evamfinance.com', 'Scoped Reader', 'Scoped'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
INSERT INTO users (id, tenant_id, email, full_name, short_name)
SELECT '10000000-0000-0000-0000-000000000002', id,
       'e2e.maker@evamfinance.com', 'Denied Reader', 'Denied'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
INSERT INTO user_roles (tenant_id, user_id, role)
SELECT tenant_id, id, 'Deal Analyst' FROM users
WHERE email = 'e2e.rm@evamfinance.com' ON CONFLICT DO NOTHING;
-- The shipped visibility layer grants whole-book READ. This controlled matrix
-- override explicitly exercises SCOPED reads without changing platform defaults.
UPDATE access_grants SET access = 'SCOPED', origin = 'override'
WHERE kind = 'view' AND item = 'lending' AND role = 'Deal Analyst';
\connect register
INSERT INTO tenants (code, name) VALUES ('OTHER', 'Other synthetic tenant')
ON CONFLICT DO NOTHING;
INSERT INTO entities (id, tenant_id, code, legal_name, entity_type)
SELECT '20000000-0000-0000-0000-000000000001', id, 'VISIBLE', 'Visible Co', 'Company'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
INSERT INTO entities (id, tenant_id, code, legal_name, entity_type)
SELECT '20000000-0000-0000-0000-000000000002', id, 'OTHER', 'Other Co', 'Company'
FROM tenants WHERE code = 'OTHER' ON CONFLICT DO NOTHING;
INSERT INTO entities (id, tenant_id, code, legal_name, entity_type)
SELECT '20000000-0000-0000-0000-000000000003', id, 'RESTRICTED', 'Restricted Co', 'Company'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
INSERT INTO lending_tracker (id, tenant_id, entity_id, tracker_no, amount_cr, remarks)
SELECT '30000000-0000-0000-0000-000000000001', id,
 '20000000-0000-0000-0000-000000000001', 'L001', 10, 'Visible amber evidence.'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
INSERT INTO lending_tracker (id, tenant_id, entity_id, tracker_no, amount_cr, remarks)
SELECT '30000000-0000-0000-0000-000000000002', id,
 '20000000-0000-0000-0000-000000000003', 'L002', 90, 'Restricted violet evidence.'
FROM tenants WHERE code = 'EVAM' ON CONFLICT DO NOTHING;
UPDATE lending_tracker SET entity_id = '20000000-0000-0000-0000-000000000003'
WHERE id = '30000000-0000-0000-0000-000000000002';
INSERT INTO lending_tracker (id, tenant_id, entity_id, tracker_no, amount_cr, remarks)
SELECT '30000000-0000-0000-0000-000000000003', id,
 '20000000-0000-0000-0000-000000000002', 'L003', 900, 'Other tenant crimson evidence.'
FROM tenants WHERE code = 'OTHER' ON CONFLICT DO NOTHING;
INSERT INTO line_assignments (tenant_id, user_id, subject_type, subject_id, assignment_role)
SELECT id, '10000000-0000-0000-0000-000000000001', 'Lending',
 '30000000-0000-0000-0000-000000000001', 'Deal Analyst'
FROM tenants WHERE code = 'EVAM'
AND NOT EXISTS (SELECT 1 FROM line_assignments
 WHERE user_id = '10000000-0000-0000-0000-000000000001');

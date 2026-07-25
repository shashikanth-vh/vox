-- Runs on FIRST init of the postgres volume only: the Access service's database on the
-- shared server (one server, a database per service). Existing volumes: run this once
-- by hand (docker compose exec postgres psql -U prism -d register -f /docker-entrypoint-initdb.d/01-create-access-db.sql)
-- or wipe with `down -v`.
CREATE DATABASE access OWNER prism;

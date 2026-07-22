#!/bin/bash
# Least-privilege roles per RFC-002 §10. Runs once on first boot as the superuser.
#   migrator  — owns/creates schema objects; BYPASSRLS; used ONLY by Alembic/CI.
#   app_rw    — runtime read/write; RLS is FORCED (never bypasses); no DDL.
#   app_ro    — read-only (replicas / reporting); RLS-forced.
# Passwords come from container env (see docker-compose.yml + .env).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	-- Roles ---------------------------------------------------------------
	DO \$\$
	BEGIN
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN
	    CREATE ROLE migrator LOGIN PASSWORD '${MIGRATOR_PASSWORD}' BYPASSRLS;
	  END IF;
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
	    CREATE ROLE app_rw LOGIN PASSWORD '${APP_RW_PASSWORD}';
	  END IF;
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') THEN
	    CREATE ROLE app_ro LOGIN PASSWORD '${APP_RO_PASSWORD}';
	  END IF;
	END
	\$\$;

	-- Schema ownership & create rights ------------------------------------
	-- migrator owns everything it creates; app roles only ever get DML.
	GRANT ALL ON SCHEMA public TO migrator;
	ALTER SCHEMA public OWNER TO migrator;

	GRANT USAGE ON SCHEMA public TO app_rw, app_ro;

	-- Existing objects (extensions etc.)
	GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw;
	GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_ro;
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw;
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_ro;

	-- Default privileges for anything migrator creates from now on --------
	ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
	ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
	  GRANT SELECT ON TABLES TO app_ro;
	ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO app_rw, app_ro;
SQL

echo "Relay roles ready: migrator (BYPASSRLS), app_rw, app_ro"

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS btree_gin;
-- Persist timezone across sessions. A bare `SET timezone = ...` would only
-- affect the init-script's own session and subsequent psql connections
-- would revert to UTC. `ALTER DATABASE` writes the setting into
-- pg_database and every new connection picks it up.
ALTER DATABASE fraud_platform SET timezone = 'Europe/London';

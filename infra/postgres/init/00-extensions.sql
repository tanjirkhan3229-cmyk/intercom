-- Extensions Relay relies on (RFC-002 §4, §5). Runs once against POSTGRES_DB on first boot.
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: HNSW ANN for retrieval (R7)
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram: name typeahead (R8)
CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive email on contacts
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_bytes / digest helpers

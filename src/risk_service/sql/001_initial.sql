CREATE SCHEMA IF NOT EXISTS risk;
CREATE TABLE IF NOT EXISTS risk.meta (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS risk.orders (id text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS risk.requests (
    id text PRIMARY KEY, hash text NOT NULL, strategy text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL,
    decision jsonb NOT NULL, audit jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_request_window ON risk.requests(created_at);
CREATE TABLE IF NOT EXISTS risk.events (seq bigint PRIMARY KEY, hash text NOT NULL);
CREATE TABLE IF NOT EXISTS risk.outbox (
    seq bigserial PRIMARY KEY, message_id text UNIQUE NOT NULL, subject text NOT NULL,
    payload jsonb NOT NULL, published boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS risk_outbox_pending ON risk.outbox(seq) WHERE NOT published;

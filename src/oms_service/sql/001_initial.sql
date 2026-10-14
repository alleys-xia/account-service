CREATE SCHEMA IF NOT EXISTS oms;
CREATE TABLE IF NOT EXISTS oms.meta (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS oms.sessions (strategy text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS oms.orders (
    id text PRIMARY KEY, strategy text NOT NULL, client_order_id text NOT NULL, data jsonb NOT NULL,
    UNIQUE(strategy, client_order_id)
);
CREATE TABLE IF NOT EXISTS oms.inbox (
    channel text NOT NULL, id text NOT NULL, hash text NOT NULL, result jsonb NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(channel,id)
);
CREATE TABLE IF NOT EXISTS oms.risk_requests (id text PRIMARY KEY, order_id text NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS oms.execution_commands (id text PRIMARY KEY, order_id text NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS oms.executions (id text PRIMARY KEY, order_id text NOT NULL);
CREATE TABLE IF NOT EXISTS oms.strategy_heads (strategy text PRIMARY KEY, seq bigint NOT NULL);
CREATE TABLE IF NOT EXISTS oms.journal (
    seq bigint PRIMARY KEY, strategy text, strategy_seq bigint, event_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL, UNIQUE(strategy,strategy_seq)
);
CREATE TABLE IF NOT EXISTS oms.outbox (
    seq bigserial PRIMARY KEY, message_id text NOT NULL UNIQUE, subject text NOT NULL,
    payload jsonb NOT NULL, published boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS oms_outbox_pending ON oms.outbox(seq) WHERE published=false;
CREATE TABLE IF NOT EXISTS oms.quarantine (
    id bigserial PRIMARY KEY, channel text NOT NULL, message_id text NOT NULL,
    reason text NOT NULL, payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(channel,message_id,reason)
);

ALTER TABLE oms.inbox ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

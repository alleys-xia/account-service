CREATE SCHEMA IF NOT EXISTS portfolio;
CREATE TABLE IF NOT EXISTS portfolio.meta (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio.events (seq bigint PRIMARY KEY, hash text NOT NULL, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio.orders (id text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio.executions (id text PRIMARY KEY, strategy text NOT NULL, instrument text NOT NULL, data jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS portfolio_execution_pair ON portfolio.executions(strategy,instrument);
CREATE TABLE IF NOT EXISTS portfolio.positions (strategy text NOT NULL, instrument text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(strategy,instrument));
CREATE TABLE IF NOT EXISTS portfolio.broker_snapshots (id text PRIMARY KEY, hash text NOT NULL, data jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT now());

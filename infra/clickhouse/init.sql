-- The ClickHouse exporter creates this database itself when create_schema is on.
-- Creating it up front makes the stack usable before the first span arrives.
CREATE DATABASE IF NOT EXISTS otel;

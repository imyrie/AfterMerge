"""Build a retention-scale copy of the span table for benchmarking.

The rollup's value grows with retention: the scan grows with every day kept while
the answer stays the same size. Demonstrating that needs more history than a
laptop accumulates in an afternoon.

This clones **real spans** -- same schema, same attribute payloads, same
distribution -- across a span of days, giving each copy a fresh trace id so
distinct-count aggregates stay meaningful. It is a retention simulation, not
recorded traffic, and it is written to a separate database so the real `otel`
data used by the pipeline is never touched.

    uv run python scripts/generate_benchmark_data.py --days 30 --repeats 6
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

SOURCE_DB = "otel"
SOURCE_TABLE = "otel_traces"


def ch(sql: str, *, database: str | None = None) -> str:
    command = ["docker", "exec", "aftermerge-clickhouse", "clickhouse-client"]
    if database:
        command += ["--database", database]
    command += ["--query", sql]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"clickhouse error:\n{result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="otel_bench")
    parser.add_argument("--days", type=int, default=30, help="Days of history to spread across.")
    parser.add_argument("--repeats", type=int, default=6, help="Copies of the source per day.")
    args = parser.parse_args()

    source_rows = int(ch(f"SELECT count() FROM {SOURCE_DB}.{SOURCE_TABLE}"))
    if source_rows == 0:
        raise SystemExit(f"{SOURCE_DB}.{SOURCE_TABLE} is empty; run `make dance` first")

    target = f"{args.database}.{SOURCE_TABLE}"
    ch(f"CREATE DATABASE IF NOT EXISTS {args.database}")
    ch(f"DROP TABLE IF EXISTS {target}")
    # AS <table> copies the structure and the engine, so the benchmark measures
    # the same storage layout production uses.
    ch(f"CREATE TABLE {target} AS {SOURCE_DB}.{SOURCE_TABLE}")

    planned = source_rows * args.days * args.repeats
    print(f"source {source_rows:,} rows -> {planned:,} rows across {args.days} days")

    started = time.monotonic()
    inserts = 0
    for day in range(args.days):
        for rep in range(args.repeats):
            # A fresh trace id per copy. Without it every clone shares a trace and
            # uniqExact collapses, which would make spans-per-request nonsense.
            salt = f"{day}-{rep}"
            # Span rows carry wide Map columns, so a default-sized block can want
            # more than a gigabyte to materialise. Capping block size and threads
            # keeps each insert well inside the server's memory budget; without
            # this the run dies partway with MEMORY_LIMIT_EXCEEDED.
            ch(
                f"INSERT INTO {target} SELECT * REPLACE ("
                f"  Timestamp - toIntervalDay({day}) AS Timestamp,"
                f"  lower(hex(MD5(concat(TraceId, '{salt}')))) AS TraceId"
                f") FROM {SOURCE_DB}.{SOURCE_TABLE}"
                f" SETTINGS max_block_size = 8192, max_threads = 2,"
                f" max_insert_threads = 1, min_insert_block_size_rows = 8192"
            )
            inserts += 1
        print(f"  day {day + 1}/{args.days} ({inserts} inserts)", flush=True)

    rows = int(ch(f"SELECT count() FROM {target}"))
    size = ch(
        "SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts "
        f"WHERE active AND database='{args.database}' AND table='{SOURCE_TABLE}'"
    )
    days = int(ch(f"SELECT uniqExact(toDate(Timestamp)) FROM {target}"))
    print(f"\n{target}: {rows:,} rows, {days} distinct days, {size} on disk")
    print(f"took {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

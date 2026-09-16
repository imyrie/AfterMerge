"""Ephemeral shopdemo at a given commit.

Every sandbox is a separate compose project with its own Postgres, its own
collector, and its own ClickHouse database. Teardown runs in a `finally`, and
removes volumes, so a crashed run cannot silently accumulate disk.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from aftermerge.reproducer import worktree

COMPOSE_FILE = Path("infra/sandbox/compose.yml")
COLLECTOR_CONFIG = Path("infra/sandbox/collector.yaml")
SEED_FILE = Path("infra/sandbox/seed.sql")
SCHEMA_RELPATH = Path("fixtures/shopdemo/db/01-schema.sql")

STARTUP_TIMEOUT = 180


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Sandbox:
    """A running, isolated copy of the application at one commit.

    Replay takes this object rather than a URL string. A captured request can
    therefore only be sent somewhere ephemeral -- pointing one at production is
    not an accident that the type system permits.
    """

    sha: str
    project: str
    base_url: str
    trace_database: str

    def health(self) -> dict[str, str]:
        payload: dict[str, str] = httpx.get(f"{self.base_url}/health", timeout=10.0).json()
        return payload

    def span_count(self) -> int:
        """Spans currently visible in this sandbox's own trace database."""
        result = subprocess.run(
            [
                "docker",
                "exec",
                "aftermerge-clickhouse",
                "clickhouse-client",
                "--query",
                f"SELECT count() FROM {self.trace_database}.otel_traces",
            ],
            capture_output=True,
            text=True,
        )
        raw = result.stdout.strip()
        return int(raw) if raw.isdigit() else 0

    def wait_for_spans(self, minimum: int = 1, timeout: float = 30.0) -> int:
        """Block until at least `minimum` spans have been exported.

        Exists because querying straight after a request reliably returns zero:
        the collector batches, and the table is created at collector startup, so
        an empty result looks identical to a broken pipeline. Every caller would
        otherwise rediscover this with an arbitrary sleep.
        """
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            seen = self.span_count()
            if seen >= minimum:
                return seen
            time.sleep(1)
        raise SandboxError(
            f"only {seen} span(s) reached {self.trace_database} within {timeout:.0f}s "
            f"(expected at least {minimum})"
        )


def _compose(project: str, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise SandboxError(result.stderr.strip() or f"compose {' '.join(args)} failed")
    return result.stdout


def _gateway_url(project: str, env: dict[str, str]) -> str:
    raw = _compose(project, "port", "gateway", "8000", env=env).strip()
    match = re.search(r":(\d+)$", raw)
    if not match:
        raise SandboxError(f"could not read the gateway's published port from {raw!r}")
    return f"http://localhost:{match.group(1)}"


def _await_version(url: str, sha: str) -> None:
    """Block until the gateway serves the expected commit.

    Checks the reported version rather than mere reachability: a stale container
    from a previous run answers health checks perfectly well while serving the
    wrong code.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last = "no response"
    while time.monotonic() < deadline:
        try:
            reported = httpx.get(f"{url}/health", timeout=5.0).json().get("version")
            if reported == sha:
                return
            last = f"serving {reported!r}"
        except Exception as exc:  # container still starting
            last = type(exc).__name__
        time.sleep(2)
    raise SandboxError(f"sandbox at {sha} never became ready ({last})")


def _drop_trace_database(database: str) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "aftermerge-clickhouse",
            "clickhouse-client",
            "--query",
            f"DROP DATABASE IF EXISTS {database}",
        ],
        capture_output=True,
        text=True,
    )


@contextmanager
def sandbox(ref: str, *, repo_root: Path, keep_traces: bool = False) -> Iterator[Sandbox]:
    """Bring up the application at `ref`, and guarantee it is torn down."""
    sha = worktree.resolve(ref, repo_root=repo_root)
    worktree.ensure_image(sha, repo_root=repo_root)
    tree = worktree.ensure_worktree(sha, repo_root=repo_root)

    # A unique suffix so two sandboxes at the same commit cannot collide.
    project = f"repro-{sha}-{uuid.uuid4().hex[:6]}"
    database = f"repro_{sha}_{uuid.uuid4().hex[:6]}"

    env = {
        "REPRO_PROJECT": project,
        "REPRO_SHA": sha,
        "REPRO_DATABASE": database,
        "REPRO_SCHEMA": str((tree / SCHEMA_RELPATH).resolve()),
        "REPRO_SEED": str((repo_root / SEED_FILE).resolve()),
        "REPRO_COLLECTOR_CONFIG": str((repo_root / COLLECTOR_CONFIG).resolve()),
    }

    try:
        _compose(project, "up", "-d", "--no-build", env=env)
        url = _gateway_url(project, env)
        _await_version(url, sha)
        yield Sandbox(sha=sha, project=project, base_url=url, trace_database=database)
    finally:
        # -v is deliberate: the Postgres volume is throwaway, and leaving one
        # behind per replay is how a disk quietly fills up.
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                project,
                "down",
                "-v",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )
        if not keep_traces:
            _drop_trace_database(database)

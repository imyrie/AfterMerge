#!/usr/bin/env bash
# Deploy shopdemo at a specific commit (slice 0, step C2).
#
# Builds from a detached git worktree rather than the working tree, so the
# running container genuinely corresponds to the SHA it reports. Building from
# the working tree would let uncommitted edits leak into a "deployed version"
# and quietly invalidate every before/after comparison.
#
#   scripts/deploy.sh <git-ref>

set -euo pipefail

SHA_REF="${1:?usage: scripts/deploy.sh <git-ref>}"
ROOT="$(git rev-parse --show-toplevel)"
SHA="$(git -C "$ROOT" rev-parse --short "$SHA_REF")"
WT="$ROOT/.worktrees/$SHA"

# Capture what is serving BEFORE anything is replaced. Read from the running
# container rather than from git, so prev_commit_sha records what was genuinely
# in production -- not what we assume was there.
PREV_SHA="$(curl -sf http://localhost:8001/health 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' || true)"

if [ ! -d "$WT" ]; then
  git -C "$ROOT" worktree add --detach --quiet "$WT" "$SHA"
fi

echo "building shopdemo:$SHA from $(basename "$WT")" >&2
docker build -q -t "shopdemo:$SHA" "$WT/fixtures/shopdemo" >/dev/null

GIT_SHA="$SHA" docker compose -f "$ROOT/docker-compose.yml" up -d \
  --force-recreate --no-build orders gateway >/dev/null 2>&1

# Do not report success until the service actually serves the expected version.
deadline=$((SECONDS + 120))
gw_version() { curl -sf http://localhost:8000/health 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'; }
ord_version() { curl -sf http://localhost:8001/health 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'; }

until [ "$(gw_version)" = "$SHA" ] && [ "$(ord_version)" = "$SHA" ]; do
  if [ $SECONDS -gt $deadline ]; then
    echo "TIMEOUT: expected $SHA, gateway=$(gw_version) orders=$(ord_version)" >&2
    exit 1
  fi
  sleep 2
done

# Telemetry already splits by service.version. This records what telemetry
# cannot know: the ordering of commits and the wall-clock changeover, which is
# what change correlation joins against.
REPO_URL="$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)"
for svc in orders gateway; do
  (cd "$ROOT" && uv run aftermerge deployments record \
      --service "$svc" \
      --sha "$SHA" \
      ${PREV_SHA:+--prev-sha "$PREV_SHA"} \
      ${REPO_URL:+--repo "$REPO_URL"} \
      --actor "${USER:-unknown}" >/dev/null) ||
    echo "WARNING: could not record deploy of $svc (audit trail incomplete)" >&2
done

echo "deployed $SHA (was ${PREV_SHA:-none})" >&2
echo "$SHA"

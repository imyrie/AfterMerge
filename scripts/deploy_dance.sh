#!/usr/bin/env bash
# Slice 0, step C2: run identical traffic against two commits.
#
#   scripts/deploy_dance.sh <good-ref> <bad-ref> [duration] [rps]

set -euo pipefail

GOOD_REF="${1:?usage: scripts/deploy_dance.sh <good-ref> <bad-ref> [duration] [rps]}"
BAD_REF="${2:?}"
DURATION="${3:-90s}"
RPS="${4:-20}"
ROOT="$(git rev-parse --show-toplevel)"

# --no-deps is load-bearing, not tidiness.
#
# The app services are declared as `image: shopdemo:${GIT_SHA:-dev}`. A plain
# `compose run loadgen` resolves that default, decides the running containers no
# longer match the config, and RECREATES them from shopdemo:dev -- silently
# reverting the deploy that just happened and tagging every span "dev".
# --no-deps leaves them alone; exporting GIT_SHA keeps config resolution honest.
run_load() {
  local sha="$1"
  GIT_SHA="$sha" DURATION="$DURATION" RPS="$RPS" \
    docker compose -f "$ROOT/docker-compose.yml" run --rm --no-deps loadgen 2>&1 |
    grep -E "http_req_duration|http_reqs|checks_succ|dropped_iterations" || true
}

assert_serving() {
  local sha="$1" actual
  actual="$(curl -sf http://localhost:8000/health | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  [ "$actual" = "$sha" ] || { echo "gateway serving '$actual', expected '$sha'" >&2; exit 1; }
  actual="$(curl -sf http://localhost:8001/health | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
  [ "$actual" = "$sha" ] || { echo "orders serving '$actual', expected '$sha'" >&2; exit 1; }
}

echo "================ BASELINE ================"
GOOD_SHA="$("$ROOT/scripts/deploy.sh" "$GOOD_REF")"
run_load "$GOOD_SHA"
assert_serving "$GOOD_SHA"   # re-check AFTER load: proves nothing swapped it out

echo
echo "================ CANDIDATE ==============="
BAD_SHA="$("$ROOT/scripts/deploy.sh" "$BAD_REF")"
run_load "$BAD_SHA"
assert_serving "$BAD_SHA"

echo
echo "baseline=$GOOD_SHA  candidate=$BAD_SHA"

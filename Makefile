.PHONY: up down logs ps probe verify ch reset lint load dance facts truncate test test-all investigate testgen certify

## Bring up storage + telemetry pipe (Phase A)
up:
	@git diff --quiet || echo "WARNING: working tree is dirty; containers will not match $$(git rev-parse --short HEAD)"
	GIT_SHA=$$(git rev-parse --short HEAD) docker compose up -d
	@echo "waiting for clickhouse..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' aftermerge-clickhouse 2>/dev/null)" = "healthy" ]; do sleep 2; done
	@echo "stack ready -- clickhouse :8123/:9000  otlp :4317/:4318  postgres :5432"

down:
	docker compose down

## Wipe volumes too. Destroys all collected telemetry.
reset:
	docker compose down -v

ps:
	docker compose ps

logs:
	docker compose logs -f otel-collector

## Phase A2: emit a probe span, then confirm it reached ClickHouse
probe:
	uv run python scripts/emit_test_span.py

verify:
	uv run python scripts/verify_span.py

## Interactive ClickHouse shell
ch:
	docker compose exec clickhouse clickhouse-client --database otel

## Slice 0 phase C
truncate:
	docker compose exec -T clickhouse clickhouse-client --database otel --query "TRUNCATE TABLE otel_traces"

load:
	docker compose run --rm loadgen

## make dance GOOD=<ref> BAD=<ref> [DUR=90s] [RPS=20]
dance:
	./scripts/deploy_dance.sh $(or $(GOOD),cbb4790) $(or $(BAD),8b4fd77) $(or $(DUR),90s) $(or $(RPS),20)

facts:
	uv run aftermerge facts

## Generate a regression test and keep it only if it fails@bad and passes@good
certify:
	uv run aftermerge certify

## Write a regression test from the incident's evidence
testgen:
	uv run aftermerge testgen

## Detect a regression and write an evidence-backed report
investigate:
	uv run aftermerge investigate --output incident-report.md

test:
	uv run pytest -q

## Includes the slow docker sandbox tests
test-all:
	uv run pytest -q -m ''

lint:
	uv run ruff check .
	uv run ruff format --check .

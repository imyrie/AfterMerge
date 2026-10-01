.PHONY: up down logs ps probe verify ch reset lint load dance facts truncate test test-all investigate testgen certify validate fix pr evaluate dq rollup stream dag ask serve

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

## Create warehouse rollups and verify they agree with raw
rollup:
	uv run aftermerge warehouse apply
	uv run aftermerge warehouse refresh
	uv run aftermerge warehouse benchmark

## Run the scheduled pipeline once, end to end (needs the airflow group)
dag:
	AIRFLOW_HOME=$(or $(AIRFLOW_HOME),$(CURDIR)/.airflow) \
	AIRFLOW__CORE__DAGS_FOLDER=$(CURDIR)/dags \
	AIRFLOW__CORE__LOAD_EXAMPLES=False \
	AFTERMERGE_HOME=$(CURDIR) \
	uv run --group airflow airflow dags test aftermerge_pipeline

## Follow spans on Kafka and report a verdict during the rollout
stream:
	uv run aftermerge stream

## Serve the read API over the rollups (localhost:8000, docs at /docs)
serve:
	uv run aftermerge serve

## Check data quality before drawing conclusions from the data
dq:
	uv run aftermerge dq

## Ask a metric question in plain language: make ask Q="p95 latency by version"
ask:
	uv run aftermerge ask "$(Q)"

## Benchmark models on gate acceptance rate and cost per accepted result
evaluate:
	uv run aftermerge evaluate

## Build a branch and PR body from a validated fix (local; add --push to go outward)
pr:
	uv run aftermerge pr

## Propose a fix and keep it only if validation accepts it
fix:
	uv run aftermerge fix

## Validate a candidate fix: make validate PATCH=<file> [STRATEGY=revert]
validate:
	uv run aftermerge validate --patch $(PATCH) --strategy $(or $(STRATEGY),repair)

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

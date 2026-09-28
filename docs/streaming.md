# Streaming detection

Every other path in AfterMerge is after-the-fact: a deploy completes, traffic accumulates, and a
batch query compares two windows. Kafka carries the same spans to a consumer that can reach a
verdict **while the rollout is still happening**.

```bash
uv run aftermerge stream                       # follow live traffic
uv run aftermerge stream --from-beginning --idle-timeout 25   # replay a topic
```

## The pipeline

The collector fans out. ClickHouse remains the durable store every batch query reads; Kafka carries
the same spans to the consumer.

```
services --OTLP--> collector --+--> ClickHouse   (durable, batch queries)
                               `--> Kafka        (stream, live verdict)
```

Spans are published as `otlp_json` rather than the default protobuf, so the consumer needs no
generated stubs and a message can be read by eye off the topic while debugging.

## It reuses the batch rules, deliberately

The consumer builds the same `Comparison`, `Amplification` and `ErrorRate` the batch detector builds
and calls the same `evaluate`. Nothing about "what counts as a regression" is redefined here.

Two definitions of regression -- one streaming, one batch -- would drift, and the drift would show up
as the two paths disagreeing about a live incident, at the worst possible moment.

What does differ is the evidence threshold: `--min-samples` defaults to 30 against the batch
detector's 100. A streaming verdict is an early signal on thin evidence. It says "this already looks
wrong"; `aftermerge detect` remains what opens an incident.

## Verified

Deploying the good commit, then the regression, then replaying the topic:

```
critical regression detected (cbb4790 -> 8b4fd77, 944 spans)
  - database work per request rose 10.0x (2.0 -> 19.9 spans)
  - p95 rose 30.3x (34ms -> 1027ms)
  - p95 1027ms breaches SLO of 500ms
```

The preceding evaluation reported `insufficient data to decide`, which is the honest streaming
behaviour: it reports what it can justify as the windows fill, rather than staying silent until
certain or guessing early.

## Design notes

**Windows are bounded by sample count.** A consumer that accumulates unbounded state is a memory
leak with a deploy-shaped trigger. Distinct request counts come from a bounded deque of trace ids
rather than a growing set, which keeps "spans per request" exact without the leak.

**Offsets are committed after folding a batch in, not before.** At-least-once is the right guarantee:
a replayed message re-counts a few spans into a rolling window and moves an average slightly, whereas
losing messages would understate the very amplification being watched for.

**Ordering is by last activity, not first sighting.** A replayed topic sees the previously-live
version first; with first-seen ordering it stays "newest" forever, and a rollback or redeploy is then
compared the wrong way round -- naming the fix as the regression. This was a real bug, caught by the
first end-to-end replay reporting `8b4fd77 -> cbb4790` when the deploy order was the reverse.

**A malformed message yields no spans rather than raising.** One bad payload should not stop an
otherwise healthy consumer, and the offset still advances past it.

**`--idle-timeout` exists so a finite topic can be replayed.** Without it the consumer polls a
drained partition forever, which is correct for following live traffic and useless for a replay.

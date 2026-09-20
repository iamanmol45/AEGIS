# AEGIS — Explained

**AEGIS** (Autonomous Engine for Guarding Infrastructure & Systems) is a self-healing
cloud infrastructure system built on AWS. It watches a running service, detects
anomalies (CPU/memory spikes, task crashes, latency spikes, error-rate spikes,
DB connectivity failures, etc.), figures out a likely root cause, checks a
deterministic safety policy, and — if the safety gate allows it — automatically
executes a recovery action (scale out or restart) through AWS Step Functions,
then verifies the recovery actually worked. Everything is logged, auditable,
and archived to S3/DynamoDB.

It is **not** a multi-service microservices demo. The real, deployed system
protects exactly one application: a single FastAPI service (`aegis-api-service`)
running on ECS Fargate, backed by one RDS PostgreSQL instance. A separate
worker service runs the detection loop continuously in the background.

This document explains what exists today, how to run/demo it, the AWS
resources it actually provisions, and what's still missing or worth improving.

---

## 1. The big picture

```
 EventBridge (CloudWatch alarm ALARM) ──▶ SQS ──▶ Lambda ──▶ POST /cycle ─┐
                                                                          │
 supervisor.py (worker service, polls every 60s) ────────────────────────┤
                                                                          ▼
                                                            AegisController.run()
                                                                          │
                          ┌───────────────────────────────────────────────┤
                          ▼                                               │
                 1. Detect (detector.py)                                  │
                    static thresholds + Isolation Forest on CPU/Memory    │
                          │                                               │
                          ▼                                               │
                 2. Correlate (correlation.py)                            │
                    groups CPU+Memory+task_failure signals within a       │
                    60s window into ONE incident instead of 3             │
                          │                                               │
                          ▼                                               │
                 3. Create incident (incident.py → DynamoDB)              │
                          │                                               │
                          ▼                                               │
                 4. Root cause + reasoning (rca.py + ai_reasoning.py)      │
                    rule-based, NOT a real LLM/Bedrock call today          │
                          │                                               │
                          ▼                                               │
                 5. Safety policy gate (policy.py)                        │
                    severity, confidence, cooldown, max-scale checks      │
                          │                        │                      │
                 BLOCKED  │                        │ ALLOWED              │
                   ▼      ▼                        ▼                     │
             leave OPEN,          Step Functions recovery workflow        │
             escalate for         (workflow.py + infra state machine)     │
             human review                │                                │
                                          ▼                                │
                                6. Execute: SCALE_OUT or RESTART_TASKS     │
                                   (real ecs:UpdateService calls)          │
                                          │                                │
                                          ▼                                │
                                7. Verify recovery (poll ECS until         │
                                   RunningCount >= target, Pending == 0)   │
                                          │                                │
                                    ┌─────┴─────┐                          │
                               SUCCESS      still failing after retries    │
                                    │             │                        │
                                    ▼             ▼                        │
                        incident → RESOLVED   leave OPEN                   │
                                          │                                │
                                          ▼                                │
                          8. Archive raw evidence to S3, audit log entry ──┘
```

Every step above is a real, working code path in this repo — this isn't a
mockup. The one deliberately-stubbed piece is step 4: RCA and "AI reasoning"
are rule-based Python today, not a real Bedrock/LLM call (see §4.4).

---

## 2. How it works, module by module

### 2.1 Detection loop (`supervisor.py`, `worker.py`)

- Runs inside a dedicated ECS Fargate service, `AegisWorkerService`, separate
  from the API. Same container image as the API, just a different command
  (`python supervisor.py`).
- `AegisSupervisor.start()` loops forever: call `AegisController.run()`, then
  sleep for `AEGIS_POLL_INTERVAL` seconds (60s in production), interruptibly
  (so SIGTERM/SIGINT shut it down promptly, not after a full 60s sleep).
- Every cycle — success **or** exception — it emits a `HeartbeatCount=1`
  CloudWatch metric under namespace `AEGIS/Supervisor`. A CloudWatch alarm
  (`AegisSupervisorHeartbeatAlarm`) watches this with `treat_missing_data=
  BREACHING`, so if the loop ever actually dies (not just errors), two
  missed 5-minute windows trigger an alert.
- A second, faster path exists alongside the 60s poll: CloudWatch alarms
  (CPU/Memory/TaskCount) → EventBridge rule → SQS → Lambda
  (`aegis-detection-trigger`) → `POST /cycle` on the live API. This means a
  real spike doesn't have to wait up to 60 seconds for the next poll; the
  60s loop is a fallback/backstop that runs regardless.

### 2.2 Anomaly detection (`detector.py`)

Static thresholds, checked first (always wins if breached):

| Metric | Threshold |
|---|---|
| cpu | 80% |
| memory | 80% |
| running_tasks | < 1 |
| latency_ms | 1000ms |
| http_5xx_rate | 5% |
| db_connection_errors | 5 |
| disk_io_saturation | 70% |

If a metric is *below* its threshold, a second, more conservative check runs
for CPU and memory only: an **Isolation Forest** (scikit-learn) fitted on the
last 60 samples. An anomaly is only flagged if all three hold: the model
predicts an outlier, the value is 1.5 standard deviations above the rolling
mean, and the value is at least 20 (so tiny noise near zero is never
flagged). This is genuinely running unsupervised ML — it's just gated very
conservatively so it doesn't create noisy false positives.

`disk_io_saturation` is *intentionally* left out of RCA's knowledge base (see
§2.4) — it's used specifically to produce a "novel anomaly, low confidence"
incident for the `LOW_CONFIDENCE_INCIDENT` chaos scenario.

### 2.3 Multi-signal correlation (`correlation.py`)

If CPU, memory, and/or task-failure signals land within the same 60-second
window, they're merged into **one** correlated incident instead of three
separate ones (`incident_type: "CORRELATED"`, metric name like
`correlated_cpu_memory`). Only these three metric types correlate — latency,
5xx rate, DB errors, and disk I/O never merge with anything.

### 2.4 Root cause analysis (`rca.py`) and "AI reasoning" (`ai_reasoning.py`)

**Important and worth being upfront about in a demo:** neither of these
calls Bedrock or any LLM today. Both are deliberately-designed placeholders:

- `rca.py` has a hardcoded `METRIC_KNOWLEDGE` table (cause narrative +
  recommendation + base confidence per metric) and a hardcoded
  `SERVICE_DEPENDENCIES` map (`aegis-api-service → RDS PostgreSQL`).
  Confidence starts at a fixed base value per metric (0.65–0.85), then gets
  boosted for multi-signal incidents (+0.08 per extra signal) and for
  **precedent** — if a past incident with the same metric+service was ever
  marked RESOLVED, +0.10 confidence and a citation is added. An unrecognized
  metric (like `disk_io_saturation`) gets a hardcoded 0.35 confidence, which
  is *designed* to fall below the policy gate's 0.6 minimum.
- `ai_reasoning.py` just re-packages RCA's output into a human-readable
  `summary` string. No prompt, no model call, no tokens.
- Both modules' docstrings explicitly say they're built with a stable
  `analyze(incident) -> dict` interface so a real Bedrock-backed
  implementation can be swapped in later (see `docs/ARCHITECTURE_V2.md`
  §4) without touching the safety gate or anything downstream.

This is a legitimate, honest design choice for a hackathon timeline — the
important architectural point (reasoning is separate from and gated by a
deterministic safety layer) is fully implemented and demonstrable even
without a real LLM behind it.

### 2.5 The safety gate (`policy.py`)

This is the most important piece of the whole system, and it's checked in a
strict order — the first failing check blocks the action:

1. Incident must be a valid, non-empty dict with `id` and `severity`.
2. **Severity gate**: if `critical_only=True` (default), severity must be
   `CRITICAL` or the action is blocked outright.
3. Action must be one of the two whitelisted actions: `SCALE_OUT` or
   `RESTART_TASKS`. Nothing else is ever executed autonomously.
4. **Confidence gate**: RCA confidence must be ≥ `min_confidence` (0.6
   default) or it's blocked and escalated for human review.
5. **Max-scale gate** (SCALE_OUT only): current desired task count must be
   below `max_desired_count` (4 default).
6. **Cooldown gate**: at least `cooldown_seconds` (120s default) must have
   elapsed since the last autonomous action — a single shared timer across
   both actions, to prevent thrashing.

Live current values: `GET /policy`.

### 2.6 Recovery execution (`workflow.py` + the Step Functions state machine)

The real recovery path (the one chaos scenarios and the controller actually
use) is **AWS Step Functions**, not a direct in-process ECS call:

1. API starts a Step Functions execution (`aegis-recovery-workflow`) with
   the incident + action + target task count as input.
2. The state machine branches on `action`:
   - `SCALE_OUT` → `ecs:UpdateService` with a new `DesiredCount`.
   - `RESTART_TASKS` → `ecs:UpdateService` with `ForceNewDeployment: true`
     (a real rolling redeploy of the whole service, not scale-to-zero).
3. Waits 30s, then describes the ECS service and checks:
   `RunningCount >= target AND PendingCount == 0`. If not yet true, it
   retries up to 4 more times (20s wait each) — 110 seconds of verification
   budget total.
4. Terminal state is `RecoverySuccessful` or `RecoveryFailedOrEscalated`.
5. Back in the API, `wait_for_completion()` polls the execution for up to
   150 seconds and returns the outcome.
6. If recovery is verified, the incident is marked `RESOLVED` in DynamoDB.
   If not, it stays `OPEN` for a human to look at.

> Note: `remediation.py` also contains a second, simpler, *direct* ECS
> remediation path (`scale_out()` / `restart_tasks()`) that bypasses Step
> Functions entirely and — for restarts — does a scale-to-zero-then-back-up
> instead of `ForceNewDeployment`. This path exists but is **not** the one
> exercised by the chaos suite or the main controller; Step Functions is the
> "real" orchestration path for the demo.

### 2.6.1 Self-healing verification loop (closing the loop for real)

Steps 4–6 above answer "did ECS's task count converge?" — but a converged
task count doesn't prove the actual problem is gone. A `SCALE_OUT` can
succeed at the infrastructure level while the real CPU/memory pressure that
triggered the incident is still there (e.g. a genuine code-level leak, not
a load spike that more capacity actually relieves).

This gap is closed for CPU and memory incidents (the only two metrics
backed by real CloudWatch telemetry) with a bounded, self-healing loop
modeled on **CIRCA-SH** (Xiao Ma, *"Distributed Fault Root Cause
Localization and Self-Healing Strategy Generation Based on Causal
Inference"*, Procedia Computer Science 281, 2026 — see `New folder/` for
the source paper):

1. After the chosen action's ECS-level state converges, **re-measure the
   real CloudWatch metric** that triggered the incident. This is a
   counterfactual-style check — "did intervening on this service actually
   fix the symptom?" — rather than trusting infrastructure convergence
   alone as proof of a fix.
2. If the symptom is still breaching its threshold despite ECS converging,
   the system doesn't retry the same action blindly. It escalates **once**
   to the *other* action: persistent high CPU/memory after `SCALE_OUT`
   suggests a stuck process (`RESTART_TASKS` territory), not insufficient
   capacity, and vice versa.
3. The whole loop is capped at **2 actions total** — the paper's "action
   budget" concept. If the symptom still hasn't cleared after that, the
   incident is left `OPEN` and a `SELF_HEALING_EXHAUSTED` audit event is
   logged, explicitly escalating for human review instead of retrying
   forever or reporting false success.

This runs in both the real detection pipeline (`controller.py`, for a
standalone CPU/memory anomaly) and in the two chaos scenarios backed by
real telemetry (`cpu-spike`, `memory-pressure`). Scenarios built on
simulated-only metrics (latency, 5xx rate, DB errors, disk I/O) don't get
this treatment — there's no real telemetry behind them to counterfactually
re-check, and faking a check against fake data would defeat the point.

Verified live against the production deployment: a real `cpu-spike` run
healed in a single attempt, with the response showing the actual measured
CloudWatch value (`0.68%` CPU) confirming the symptom had genuinely
cleared — not just that ECS reported the right task count. See `report.md`
for the full end-to-end test results.

### 2.7 Evidence archiving (`evidence.py`)

Every detection/correlation/RCA payload for an incident is written as a JSON
blob to S3 under `incidents/{incident_id}/{timestamp}.json`. DynamoDB only
stores a pointer (`evidence_s3_key`) to keep incident records small. Evidence
upload is fail-soft — if S3 is unreachable, the pipeline still completes;
you just won't see an evidence block for that incident.

### 2.8 Persistence (`incident_store.py`, DynamoDB)

All incidents (real ones and chaos-test-generated ones) live in a single
DynamoDB table, `AegisIncidents`, partition key `incident_id`, on-demand
billing, point-in-time recovery on. This table is also (as of this session's
fixes) reused to store chaos-run history records, tagged with
`record_type: "chaos_test"`, so `/chaos/history` is consistent no matter
which of the 4 API replicas behind the load balancer answers the request.

---

## 3. How to use it

### 3.1 Base URL

```
http://AegisI-Aegis-9T1qlmEvlgUH-464819824.ap-south-1.elb.amazonaws.com
```
(A plain HTTP ALB DNS name — not a custom domain. See §6 for the caveats
that come with that.)

### 3.2 API endpoints

| Method & path | What it does |
|---|---|
| `GET /health` | Liveness check |
| `GET /status` | One-shot system snapshot: ECS state, CPU/mem, incident counts, chaos totals, policy, workflow config, correlation state |
| `GET /policy` | Current safety-gate configuration |
| `GET /workflow` | Step Functions state machine status |
| `GET /correlation` | Correlation window + pending signal count |
| `GET /incidents` | List all incidents |
| `GET /incidents/{id}` | One incident's full record |
| `GET /incidents/{id}/evidence` | Raw evidence blob from S3 for that incident |
| `POST /reason` | Runs RCA + "AI reasoning" live against a given incident payload |
| `POST /remediate` | Direct (non-Step-Functions) remediation path |
| `POST /verify-recovery` | Direct recovery verification check |
| `GET /audit` | Local audit log (per-instance — see §5 gaps) |
| `GET /chaos` | List the 10 available chaos scenarios |
| `POST /chaos/{scenario}?dry_run=bool` | Run a chaos scenario for real |
| `GET /chaos/history` | Shared, cross-replica chaos run history |
| `POST /detect`, `GET /scan`, `POST /cycle`, `POST /incident` | Lower-level detection primitives, mostly used internally / for testing |

### 3.3 The dashboard (`aegis-light.html`)

A single self-contained HTML file (no build step, no server needed) that
polls the live API every 10–60 seconds (configurable in the top bar) and
renders: system health, CPU/memory trend (sampled client-side from the
moment the page is opened — there's no historical time-series endpoint),
the incidents table with live RCA on the selected incident, safety-gate
config, recovery history, and a chaos control panel with all 10 real
scenarios plus a dry-run toggle.

Just open the file in a browser — it talks directly to the ALB via CORS
(enabled specifically for this).

### 3.4 The 10 chaos scenarios

| Scenario | What it simulates | Expected outcome |
|---|---|---|
| `cpu-spike` | 95% CPU | Auto-recovers (SCALE_OUT) if not on cooldown/at max scale |
| `task-failure` | 0 running tasks | Auto-recovers (RESTART_TASKS) |
| `memory-pressure` | High memory | Auto-recovers (SCALE_OUT) |
| `multi-signal` | CPU + memory together | Correlated into ONE incident, then recovers |
| `latency-spike` | 3,500ms p99 | Auto-recovers |
| `error-rate-spike` | 25% HTTP 5xx | Auto-recovers |
| `db-connectivity-failure` | RDS unreachable | **Always escalates** — not in the allowed-actions list |
| `bad-deployment` | Error spike tagged as a recent deploy | Auto-recovers |
| `low-confidence` | Unrecognized metric (`disk_io_saturation`) | **Always blocked** — confidence 0.35 < 0.6 gate |
| `normal-workload` | Healthy metrics | No incident created — confirms no false positives |

`db-connectivity-failure` and `low-confidence` being "expected failures" is
the point — they demonstrate the safety gate actually gating something.

---

## 4. Technical deep-dive summary

| Concern | Real implementation? | Notes |
|---|---|---|
| Anomaly detection | ✅ Real | Static thresholds + genuine Isolation Forest on CPU/memory |
| Multi-signal correlation | ✅ Real | In-memory, 60s window, 3 metric types |
| Root cause analysis | ⚠️ Rule-based | Hardcoded knowledge table, not Bedrock/LLM |
| "AI reasoning" | ⚠️ Rule-based | String templating over RCA output, not a model call |
| Safety policy gate | ✅ Real | Deterministic, fully enforced, the load-bearing safety mechanism |
| Recovery execution | ✅ Real | Real `ecs:UpdateService` via Step Functions |
| Recovery verification | ✅ Real | Polls real ECS state, tolerant of rolling-deploy overshoot |
| Self-healing symptom verification | ✅ Real (added this session) | Re-measures real CPU/memory from CloudWatch post-action; bounded 2-action escalation, CIRCA-SH-inspired |
| Evidence archival | ✅ Real | Real S3 writes |
| Incident persistence | ✅ Real | Real DynamoDB, shared across replicas |
| Chaos suite | ✅ Real | Hits real endpoints, real ECS actions, real Step Functions executions |
| Auto-resolution of incidents | ✅ Real (added this session) | Verified-recovery → `status: RESOLVED` |
| CORS / browser dashboard | ✅ Real (added this session) | `allow_origins=["*"]` |
| Detection triggering | ✅ Real | 60s poll + EventBridge/SQS/Lambda fast path |

Tech stack: **FastAPI + boto3 + scikit-learn + numpy** on Python, deployed on
**ECS Fargate**, infrastructure as **AWS CDK (Python)**.

---

## 5. Real deployment — what's actually running on AWS

Everything below is provisioned by a single CDK stack,
`infrastructure/infrastructure/infrastructure_stack.py`, and is currently
live in account `097079438330`, region `ap-south-1`.

### 5.1 Networking
- **VPC** (`AegisVpc`) — 2 AZs, 2 NAT gateways (one per AZ, for redundancy).

### 5.2 Compute
- **ECS Cluster** (`aegis-cluster`)
- **API service** (`aegis-api-service`) — Fargate, 256 CPU / 512MB, 4 desired
  tasks (scaled up over this session's testing), behind the ALB.
- **Worker service** (`AegisWorkerService…`) — Fargate, 256/512, 1 task,
  runs `supervisor.py`. Same container image as the API, different command.
- **ECR repo** `aegis-api` (imported, not created by CDK — built/pushed
  manually via `docker build && docker push`).

### 5.3 Load balancing
- **ALB**, internet-facing, HTTP only on port 80 (no HTTPS/TLS — see §7),
  `idle_timeout=200s` (raised this session so long-running chaos requests
  don't get their connection killed mid-flight).

### 5.4 Database
- **RDS PostgreSQL 16**, `db.t3.micro`, 20GB storage, private subnet,
  encrypted at rest, **single-AZ (not Multi-AZ)** — a real availability gap
  if this ever needs to be production-grade (see §7).

### 5.5 Data & storage
- **DynamoDB** `AegisIncidents` — on-demand billing, point-in-time recovery.
- **S3 evidence bucket** — SSE-encrypted, all public access blocked,
  30-day lifecycle expiry, auto-delete on stack teardown.

### 5.6 Observability & alerting
- **CloudWatch alarms**: HighCpu, HighMemory, TaskCount (using the corrected
  `LiveTaskCount` metric — the CDK-default `RunningTaskCount` doesn't exist
  under `AWS/ECS` and would silently never fire), SupervisorHeartbeat
  (missing-data-is-breaching), DetectionDLQ depth.
- **SNS topic** `aegis-alerts` — **has no subscribers configured in code**;
  alarms fire into it but nobody currently receives anything unless a
  subscription was added manually outside CDK.
- **CloudTrail** (`aegis-trail`) — multi-region, all management events,
  its own S3 bucket (kept separate from the evidence bucket).
- **AWS Budgets** — $100/month guardrail, alerts at 50%/80% actual spend to
  a single email address.

### 5.7 Event-driven detection path
- **EventBridge rules**: one routes CloudWatch alarm state changes to SQS
  (fast-path detection trigger); another routes Step Functions execution
  failures to SNS.
- **SQS** detection queue + DLQ (3 retries before dead-lettering).
- **Lambda** (`aegis-detection-trigger`, Python 3.12, inline code, no extra
  dependencies) — consumes the queue and calls `POST /cycle` on the API.

### 5.8 Orchestration
- **Step Functions** state machine `aegis-recovery-workflow` — the real
  recovery orchestrator, described in §2.6.

### 5.9 IAM
Mostly scoped to specific resource ARNs. A handful of wildcard
(`resources=["*"]`) statements remain where AWS genuinely has no
resource-level permission support for that action (`ecs:ListServices`,
`cloudwatch:GetMetricData`/`ListMetrics`, `states:ListStateMachines`,
`ecs:DescribeServices` from the state machine role). This is a real,
documented, narrow gap — not a "grant everything" policy.

### 5.10 Manual/out-of-band deployment steps
Because there's no CI/CD pipeline yet, every code change to the API/worker
requires this manual sequence (done multiple times this session):
```powershell
cd services/api
docker build -t aegis-api .
docker tag aegis-api:latest 097079438330.dkr.ecr.ap-south-1.amazonaws.com/aegis-api:latest
docker push 097079438330.dkr.ecr.ap-south-1.amazonaws.com/aegis-api:latest
aws ecs update-service --cluster aegis-cluster --service aegis-api-service --force-new-deployment --region ap-south-1
```
Infrastructure changes go through:
```powershell
cd infrastructure
cdk diff      # always review before deploying
cdk deploy --require-approval never
```

---

## 6. How to give a demo

**Setup (once, before the audience sees anything):**
1. Confirm the API is healthy: `curl {BASE}/health`.
2. Open `aegis-light.html` in a browser, confirm the top-right shows
   "Connected" and real numbers are populating.
3. Optionally run `GET /chaos/history` once to make sure it's not empty from
   a previous session (it will show real prior runs — that's fine, it's
   real data).

**Suggested demo flow (~5–8 minutes):**
1. **Show the live dashboard** — point out this is polling a real AWS
   deployment, not mock data. Show the ECS task health, CPU/memory ticking.
2. **Run `cpu-spike`** from the Chaos panel. Narrate what's happening while
   it runs (~10–30s if not on cooldown): detection → RCA → policy check →
   Step Functions execution → verification. Show the resulting incident
   flip to a real confidence score and the recommended action.
3. **Run `low-confidence`** — show it gets **blocked**, with the exact
   reason ("confidence 0.35 is below the minimum 0.60"). This demonstrates
   the safety gate is real and actually stops things, not just theater.
4. **Run `db-connectivity-failure`** — show it escalates rather than
   auto-acting, because `RESTART_TASKS`/`SCALE_OUT` can't fix a database
   problem and the action isn't in the allowed list for that failure mode.
5. **Click into an incident** in the dashboard and hit "Re-run" on RCA —
   show the `POST /reason` call happening live in the browser network tab
   if you want to prove it's a real request, not canned data.
6. **Close with the architecture diagram** (§1) and be upfront about what's
   rule-based today vs. what the intended Bedrock upgrade path looks like
   (`docs/ARCHITECTURE_V2.md`). Judges respond well to "here's what's real,
   here's what's the next step" — it reads as engineering maturity, not a
   gap to hide.

**If you want a more dramatic demo:** run the Locust load generator
(`loadtest/locustfile.py`, `RampingBurstShape`) against the ALB for a few
minutes before the demo starts, so CPU is already naturally elevated and a
chaos run (or even organic load) triggers a *real*, non-synthetic recovery
live on stage.

**Things to avoid saying:** don't claim RCA is "AI-powered" using an LLM —
it isn't yet. Say "rule-based root cause engine with a confidence score,
designed to be swapped for a Bedrock agent" instead. It's accurate and still
sounds like exactly what it is: solid infrastructure engineering.

---

## 7. Known gaps and recommended improvements

Roughly in priority order for a post-hackathon iteration:

1. **No real LLM/Bedrock integration yet.** RCA and "AI reasoning" are both
   rule-based placeholders with a stable interface designed for this swap.
   This is the single biggest gap between "what AEGIS is" and "what the
   original spec envisioned." See `docs/ARCHITECTURE_V2.md` §4–5 for the
   concrete plan (Bedrock Agent + Action Groups + Knowledge Base RAG over
   past incidents/runbooks).
2. **No SNS subscribers configured.** The alerts topic exists and alarms
   publish to it, but nobody receives anything until a subscription
   (email/Slack-via-Lambda) is added.
3. **`/audit` and per-instance logs are not shared across the 4 API
   replicas** (each writes to a local file). This class of bug was already
   found and fixed once for `/chaos/history` (moved to DynamoDB); the same
   fix should be applied to `AuditLogger` if the audit trail needs to be
   authoritative rather than best-effort/instance-local.
4. **RDS is single-AZ.** A real availability gap for anything beyond a demo
   — `multi_az=True` is a small cost increase for real HA.
5. **ALB is HTTP-only, open to the whole internet, no auth on any route.**
   Fine for a hackathon; not fine for anything real. Needs HTTPS (ACM +
   Route53), and at minimum an API key or IP allowlist before this is
   anything but a public demo toy. Note that permissive CORS was added
   deliberately on top of this already-open surface — tightening the ALB
   should happen before tightening CORS matters much.
6. **No CI/CD.** Every deploy is a manual `docker build/push` + `cdk
   deploy`. A GitHub Actions workflow (OIDC creds, build → ECR push → `cdk
   deploy`) would remove a whole class of the manual-step errors already
   logged in `CHALLENGES.md`.
7. **Two different restart mechanisms coexist** (`remediation.py`'s
   scale-to-zero vs. the Step Functions path's `ForceNewDeployment`). Only
   one is actually exercised by the real pipeline; the other should either
   be removed or clearly marked as an alternate/manual path to avoid
   confusion for the next person reading the code.
8. **No distributed tracing (X-Ray) and no service dependency graph beyond
   a single hardcoded string** (`aegis-api-service → RDS`). Fine for a
   single-service system; would matter if this ever grows to multiple
   real services.
9. **Detection loop is a single Fargate task with no horizontal scaling
   story.** It's watched by a heartbeat alarm (good), but if it needs to
   watch more than one service, the polling model doesn't scale — the
   EventBridge/SQS/Lambda path is the right direction to lean into further.
10. **Chaos scenario `dry_run` and verification retry budgets are
    hand-tuned constants** (110s state-machine budget, 150s API-side
    timeout, 200s ALB idle timeout) tuned empirically against a 4-task
    Fargate rolling deployment. If task count, task size, or health-check
    grace period changes meaningfully, these budgets should be revisited.
11. **`/status`'s incident totals are silently capped at 100** — found
    during end-to-end testing (`report.md` §3.2). `IncidentStore.get_incidents()`
    defaults to `limit=100`, and `/status` never overrides it, so once the
    table holds more than 100 incidents the reported total stops growing
    and just reflects the scan limit. Likely been wrong for a while; only
    became visible once the table grew past 100 real records.
12. **RCA's "impact" (downstream dependency) field never actually fires** —
    also found during end-to-end testing. Every incident's `service` field
    is `"aegis-api"`, but `rca.py`'s `SERVICE_DEPENDENCIES` dict key is
    `"aegis-api-service"` — the two never match, so the lookup always
    misses and every response shows "No downstream dependencies mapped,"
    even for a real CPU/memory incident that should cite the RDS
    dependency. A one-line string fix once someone's ready to touch it.

---

## 8. Where to look for more detail

- `docs/ARCHITECTURE_V2.md` — the target-state architecture and AWS service
  buildout plan (Bedrock, X-Ray, CI/CD, etc.), written against the paper
  *"Building AI Agents for Autonomous Clouds"* (Shetty et al., Microsoft/SoCC'24).
- `docs/VERIFICATION_RUNBOOK.md` — copy-paste PowerShell checklist to verify
  every phase (reliability, evidence/audit, event-driven detection, RCA/
  policy, chaos suite) is actually live.
- `CHALLENGES.md` — a running log of real infrastructure bugs hit and fixed
  during development (CDK import errors, Docker build-context mistakes,
  IAM permission gaps, stale hardcoded service names, and more).
- `services/api/tests/` — the test suite (40 tests as of this writing,
  covering detection, correlation, RCA confidence scoring, chaos scenarios,
  controller integration, and incident persistence).
- `report.md` — the most recent end-to-end test pass against the live
  deployment: methodology, real results for all 10 chaos scenarios, and
  every issue found (fixed and open).
- `technical.md` — the deep implementation reference (this document's
  companion), including full mechanism write-ups of every fix made this
  session.
- `demo.md` — a time-boxed demo script for presenting AEGIS live.

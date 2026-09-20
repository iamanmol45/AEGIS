# AEGIS — Autonomous Engine for Guarding Infrastructure & Systems

> A self-healing cloud infrastructure system deployed live on AWS.  
> Detects. Diagnoses. Gates. Fixes. Verifies. Closes. All without a human in the loop.

---

## What is AEGIS?

AEGIS watches a running AWS ECS Fargate service, detects anomalies (CPU spikes, task crashes, memory pressure, latency, error rate spikes, DB connectivity failures), performs root cause analysis with a confidence score, checks a deterministic 5-layer safety gate, and — if the gate allows — autonomously executes recovery through AWS Step Functions.

The part most systems skip: **AEGIS re-measures the real CloudWatch metric after acting**, and only marks the incident resolved if the actual symptom has genuinely cleared — not just because ECS said the task count matched.

Everything runs against real AWS infrastructure. Nothing is mocked.

---

## Live Demo

**Base URL:**
```
http://aegis-dashboard-aegis2026.s3-website.ap-south-1.amazonaws.com/
```

**Dashboard:** Open `aegis-light.html` directly in any browser — no server, no build step. It polls the live ALB every few seconds.

**Try it now:**
```bash
# Is the system healthy?
curl http://aegis-dashboard-aegis2026.s3-website.ap-south-1.amazonaws.com/#s-services

# Full system snapshot: ECS state, CPU/memory, incident counts, policy config
curl http://aegis-dashboard-aegis2026.s3-website.ap-south-1.amazonaws.com/#s-incidents

# Inject a real fault
curl -X POST "http://aegis-dashboard-aegis2026.s3-website.ap-south-1.amazonaws.com/#s-chaos

## Architecture

```
EventBridge (CloudWatch alarm ALARM) --> SQS --> Lambda --> POST /cycle --+
                                                                          |
supervisor.py (worker service, polls every 60s) --------------------------+
                                                                          |
                                                            AegisController.run()
                                                                          |
                          +-----------------------------------------------+
                          |
                 1. Detect (detector.py)
                    Static thresholds + Isolation Forest on CPU/Memory
                          |
                          v
                 2. Correlate (correlation.py)
                    Groups CPU+Memory+task signals in 60s window
                    into ONE incident instead of three
                          |
                          v
                 3. Create incident --> DynamoDB
                          |
                          v
                 4. Root cause + confidence score (rca.py)
                    Precedent-boosted from real DynamoDB history
                          |
                          v
                 5. Safety gate (policy.py)
                    Severity --> allowed action --> confidence
                    --> max-scale --> cooldown (first failure blocks)
                          |                  |
                   BLOCKED |                 | ALLOWED
                     v                       v
              Leave OPEN,       Step Functions recovery workflow
              escalate          (real ecs:UpdateService call)
                                             |
                                             v
                                6. Execute: SCALE_OUT or RESTART_TASKS
                                             |
                                             v
                                7. Re-measure real CloudWatch metric
                                   (CIRCA-SH bounded loop, max 2 tries)
                                             |
                                      +------+------+
                                   CLEARED      Still breaching
                                      |              |
                                      v              v
                          Incident --> RESOLVED    Escalate, leave OPEN
                                             |
                                             v
                              8. Archive evidence --> S3, audit log
```

**Two trigger paths run in parallel:**
- **60-second poll** (`supervisor.py`) — always runs as a background Fargate task, catches anything missed
- **Fast path** (CloudWatch alarm → EventBridge → SQS → Lambda → `POST /cycle`) — fires within seconds of a real metric breach

---

## Key Features

| Feature | What makes it real |
|---|---|
| **Anomaly detection** | Static thresholds + scikit-learn Isolation Forest on live CPU/memory — genuine unsupervised ML, conservatively gated |
| **Multi-signal correlation** | CPU + memory + task failure signals within 60s → merged into one correlated incident, not three separate pages |
| **Root cause analysis** | Confidence-scored knowledge table + **precedent boosting** — if this metric on this service was fixed before, confidence goes up and the prior incident is cited |
| **5-layer safety gate** | Severity → allowed action whitelist → confidence floor (0.6) → max desired count ceiling → cooldown timer. First failure blocks. Deterministic, no exceptions |
| **Step Functions recovery** | Real `ecs:UpdateService` calls orchestrated with retry budget and timeout — not a script running locally |
| **Self-healing verification** | After action, **re-measures the actual CloudWatch metric**. If still breaching → escalates to alternate action once. Capped at 2 total actions (CIRCA-SH) |
| **Auto-resolve** | Incident flips `OPEN → RESOLVED` only when the real symptom is confirmed cleared, not just when ECS reports task count match |
| **10 chaos scenarios** | All tested live against real AWS — including 2 deliberately designed to be blocked by the safety gate |
| **Cross-replica consistency** | Chaos history and incidents backed by DynamoDB — all 4 API replicas return identical data |
| **Self-contained dashboard** | `aegis-light.html` — open in browser, no server needed, polls live ALB every 10s |

---

## Test Results

> **40/40 unit tests passing. 10/10 chaos scenarios verified live against real AWS infrastructure.**

Verified in the last full test pass against the live production deployment:

| Scenario | Result | Notes |
|---|---|---|
| `cpu-spike` | RESOLVED in **56 seconds** | Real CloudWatch CPU confirmed cleared (0.68%) before auto-resolve |
| `memory-pressure` | RESOLVED in **51 seconds** | Real memory % confirmed cleared (26.57%) |
| `task-failure` | COMPLETED | RESTART_TASKS executed and verified |
| `multi-signal` | COMPLETED | Correlated into one incident, RESTART_TASKS executed |
| `error-rate-spike` | COMPLETED | Policy checks passed, RESTART_TASKS executed |
| `latency-spike` | BLOCKED_BY_POLICY | Max desired task count reached — correct |
| `bad-deployment` | BLOCKED_BY_POLICY | Cooldown active — correct |
| `db-connectivity-failure` | BLOCKED_BY_POLICY | Action not in allowed list — correct by design |
| `low-confidence` | BLOCKED_BY_POLICY | Confidence 0.35 < minimum 0.60 — correct by design |
| `normal-workload` | NO_ANOMALY_DETECTED | No false positive on healthy metrics |

Every safety guardrail was exercised live: confidence gate, allowed-actions whitelist, cooldown timer, max-scale ceiling.

---

## AWS Services Used

| Service | How AEGIS uses it |
|---|---|
| **ECS Fargate** | API service (4 tasks behind ALB) + worker detection loop (1 task, same image, different command) |
| **ECR** | Container image registry — `docker push` + `ecs update-service --force-new-deployment` |
| **ALB** | Internet-facing load balancer; `idle_timeout=200s` to handle long-running chaos requests |
| **Step Functions** | Orchestrates real `ecs:UpdateService` recovery with retry, timeout, and full execution audit trail |
| **DynamoDB** | `AegisIncidents` table — all incidents + chaos history, on-demand billing, PITR on, shared across all 4 replicas |
| **S3** | Evidence blobs (raw detection/RCA payload) per incident — SSE-encrypted, 30-day lifecycle |
| **CloudWatch** | CPU/memory metrics for detection AND post-action symptom re-verification; custom `AEGIS/Supervisor` heartbeat metric |
| **EventBridge** | Routes CloudWatch alarm state changes → SQS (fast-path detection); Step Functions failures → SNS |
| **SQS** | Detection queue with DLQ (3 retries before dead-lettering) |
| **Lambda** | Consumes SQS → calls `POST /cycle` on the live ALB |
| **RDS PostgreSQL 16** | The service being protected — single-AZ, t3.micro, private subnet |
| **CloudTrail** | Multi-region, all management events — separate S3 bucket from evidence |
| **AWS Budgets** | $100/month guardrail; alerts at 50% and 80% actual spend |
| **AWS CDK (Python)** | All of the above defined as code in `infrastructure/` — one `cdk deploy` provisions everything |

---

## Project Structure

```
AEGIS/
├── services/api/
│   ├── app.py               # All HTTP endpoints (health, status, chaos, incidents, reason, ...)
│   ├── controller.py        # Main orchestration loop + self-healing verification
│   ├── supervisor.py        # Worker polling loop (runs as separate Fargate task)
│   ├── detector.py          # Anomaly detection: static thresholds + Isolation Forest
│   ├── correlation.py       # Multi-signal correlation (60s window)
│   ├── rca.py               # Root cause analysis + DynamoDB precedent lookup
│   ├── ai_reasoning.py      # Human-readable RCA packaging (rule-based; Bedrock interface ready)
│   ├── policy.py            # Safety gate (5 sequential checks)
│   ├── workflow.py          # Step Functions integration
│   ├── chaos.py             # 10 chaos scenarios
│   ├── incident.py          # Incident data model
│   ├── incident_store.py    # DynamoDB read/write layer
│   ├── evidence.py          # S3 evidence archival
│   ├── audit.py             # Audit log
│   ├── remediation.py       # Direct ECS remediation (alternate path)
│   ├── recovery.py          # ECS recovery verification
│   ├── Dockerfile
│   └── tests/               # 40 unit + integration tests
├── infrastructure/          # AWS CDK (Python) — VPC, ECS, ALB, DynamoDB, S3, Step Functions, ...
├── loadtest/                # Locust load generator (RampingBurstShape for real pre-demo load)
├── docs/
│   ├── ARCHITECTURE_V2.md   # Target-state design: Bedrock, X-Ray, CI/CD
│   └── VERIFICATION_RUNBOOK.md
├── aegis-light.html         # Self-contained dashboard — open in browser, no build
├── CHALLENGES.md            # Real bugs hit and fixed during development (CDK, Docker, IAM, ...)
├── explain.md               # Full system explanation, module by module
├── technical.md             # Deep implementation reference
├── report.md                # End-to-end test results against live deployment
└── demo.md                  # Timed 3-minute demo script
```

---

## API Reference

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/status` | Full system snapshot: ECS, CPU/memory, incidents, policy, workflow, correlation |
| `GET` | `/policy` | Current safety gate configuration |
| `GET` | `/workflow` | Step Functions state machine status |
| `GET` | `/correlation` | Correlation window + pending signal count |
| `GET` | `/incidents` | All incidents |
| `GET` | `/incidents/{id}` | Single incident with full RCA |
| `GET` | `/incidents/{id}/evidence` | Raw evidence blob from S3 |
| `POST` | `/reason` | Run RCA + confidence scoring live against an incident |
| `GET` | `/chaos` | List all 10 chaos scenarios |
| `POST` | `/chaos/{scenario}?dry_run=bool` | Run a chaos scenario (set `dry_run=false` for real) |
| `GET` | `/chaos/history` | Cross-replica chaos run history (DynamoDB-backed) |
| `GET` | `/audit` | Per-instance audit log |
| `POST` | `/cycle` | Trigger a detection cycle (called by Lambda on CloudWatch alarm) |

---

## Running Locally

**Prerequisites:** Python 3.11+, Docker, AWS CLI configured

```bash
cd services/api
pip install -r requirements.txt

# Run the API locally
uvicorn app:app --reload --port 8000

# Run the test suite
pytest tests/ -v
```

---

## Deploying to AWS

**One-time infrastructure setup:**
```bash
cd infrastructure
pip install -r requirements.txt
cdk bootstrap   # first time only
cdk deploy --require-approval never
```

**Deploy code changes:**
```bash
cd services/api

# Build and push image
docker build -t aegis-api .
docker tag aegis-api:latest <account_id>.dkr.ecr.ap-south-1.amazonaws.com/aegis-api:latest
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <account_id>.dkr.ecr.ap-south-1.amazonaws.com
docker push <account_id>.dkr.ecr.ap-south-1.amazonaws.com/aegis-api:latest

# Roll out to ECS (zero-downtime rolling deploy)
aws ecs update-service \
  --cluster aegis-cluster \
  --service aegis-api-service \
  --force-new-deployment \
  --region ap-south-1
```

**Verify rollout:**
```bash
aws ecs describe-services \
  --cluster aegis-cluster \
  --services aegis-api-service \
  --region ap-south-1 \
  --query 'services[0].deployments[*].{status:status,running:runningCount,desired:desiredCount}'
```

---

## Pre-Demo Checklist

Run this 10 minutes before presenting — not the night before:

```bash
BASE="http://AegisI-Aegis-9T1qlmEvlgUH-464819824.ap-south-1.elb.amazonaws.com"

# 1. Confirm healthy
curl $BASE/health

# 2. Check desired_count — if it's at 4 (max), SCALE_OUT scenarios will be blocked
#    Scale down first if needed:
aws ecs update-service --cluster aegis-cluster --service aegis-api-service \
  --desired-count 2 --region ap-south-1

# 3. Check for active cooldown
curl $BASE/policy

# 4. Open dashboard and confirm real numbers are loading
#    Open aegis-light.html in browser
```

> **If a chaos call takes up to 150 seconds** — that's normal. Step Functions is doing real retries against real ECS. Narrate it, don't panic.

> **If a scenario hits BLOCKED_BY_POLICY unexpectedly** — read the `reason` field out loud. It's almost always cooldown or max-scale, and it's a good live demonstration that the guardrails are real.

---

## Known Limitations

| Gap | Status |
|---|---|
| **RCA is rule-based, not LLM-powered** | Knowledge table + confidence scoring. Interface is designed for a Bedrock Agent swap-in — see `docs/ARCHITECTURE_V2.md` |
| **No CI/CD pipeline** | Every deploy is manual `docker build/push` + `cdk deploy` |
| **ALB is HTTP-only, no auth** | Fine for hackathon; needs HTTPS + API key before anything production |
| **RDS is single-AZ** | Real availability gap — `multi_az=True` is a small cost increase away |
| **`/audit` is per-replica** | Not shared across 4 ECS tasks — same class of bug already fixed for `/chaos/history` |
| **`/status` incident count silently caps at 100** | DynamoDB `limit=100` default never overridden in the status endpoint |
| **Downstream dependency lookup never fires** | `service` field is `"aegis-api"` but `SERVICE_DEPENDENCIES` key is `"aegis-api-service"` — one-line fix |
| **SNS has no subscribers configured** | Alarms publish to the topic but nobody receives them without a manual subscription |

---

## Self-Healing Loop — Technical Detail

After executing a recovery action, AEGIS does not just check if ECS task count converged. It:

1. **Re-measures the real CloudWatch metric** that triggered the incident — the actual CPU or memory reading, not ECS state
2. If the symptom is still breaching despite ECS converging → escalates **once** to the alternate action (`SCALE_OUT` → `RESTART_TASKS` or vice versa). Persistent CPU after scale-out suggests a stuck process, not insufficient capacity
3. If still unhealed after 2 total actions → logs `SELF_HEALING_EXHAUSTED`, leaves the incident `OPEN`, escalates for human review. Bounded loop, not infinite retry

Modeled on **CIRCA-SH** (*"Distributed Fault Root Cause Localization and Self-Healing Strategy Generation Based on Causal Inference"* — Xiao Ma, Procedia Computer Science 281, 2026).

**Verified live:** a real `cpu-spike` run returned:
```json
{
  "self_healing.attempts": 1,
  "symptom.checked": true,
  "symptom.cleared": true,
  "symptom.current_value": 0.68,
  "self_healing.healed": true
}
```
The system read 0.68% CPU from CloudWatch and confirmed the symptom was genuinely gone before auto-resolving the incident.

---

## References

### Research Papers

- **AIOpsLab**: Shetty et al., *"AIOpsLab: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds"* — evaluation framework for AIOps agents; informed the chaos scenario design and agent evaluation approach used in AEGIS

- **Building AI Agents for Autonomous Clouds**: Shetty et al., *"Building AI Agents for Autonomous Clouds: Challenges and Design Principles"*, Microsoft/SoCC'24 — primary architecture reference; the detect → diagnose → gate → act pipeline in AEGIS directly follows the design principles laid out here

- **CROSS**: *"CROSS — Cloud-Native Automated Remediation and Self-Healing"* — reference for cloud-native self-healing patterns; informed the Step Functions orchestration approach and the bounded remediation loop

- **CIRCA-SH**: Xiao Ma, *"Distributed Fault Root Cause Localization and Self-Healing Strategy Generation Based on Causal Inference"*, Procedia Computer Science 281, 2026 — direct basis for the bounded 2-action self-healing verification loop in `controller.py`; the counterfactual re-measurement of real CloudWatch metrics post-action is modeled on this paper's action budget concept

- **AI-driven Self-Healing Across the Edge–Cloud Continuum**: *"AI-driven Self-Healing Across the Edge–Cloud Continuum"* — reference for multi-tier self-healing strategies; informed the escalation logic between SCALE_OUT and RESTART_TASKS when the first action fails to clear the real symptom

### AWS Practical Resources

- **AWS Self-Healing Mechanism**: Official AWS Builder reference for self-healing architecture patterns on AWS — informed the ECS + Step Functions recovery design  
  [https://builder.aws.com/content/2zSXztKZo3xjMPtjvyJEgk1HQbT/self-healing-mechanism-for-a-lightweight-website](https://builder.aws.com/content/2zSXztKZo3xjMPtjvyJEgk1HQbT/self-healing-mechanism-for-a-lightweight-website)

- **AWS EventBridge Tutorial**: Official AWS tutorial — routing CloudWatch alarm state changes to Lambda functions via EventBridge rules; directly used to build the fast-path detection trigger (CloudWatch alarm → EventBridge → SQS → Lambda → `POST /cycle`)  
  [https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-tutorial-get-started.html](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-tutorial-get-started.html)

- **Microsoft AIOpsLab GitHub**: Open-source framework for deploying microservices, injecting faults, exporting telemetry, and evaluating autonomous AIOps agents — reference implementation for the chaos scenario injection and fault simulation design in AEGIS  
  [https://github.com/microsoft/AIOpsLab](https://github.com/microsoft/AIOpsLab)

---

## More Detail

| Document | Contents |
|---|---|
| [`explain.md`](explain.md) | Full system explanation, module by module |
| [`technical.md`](technical.md) | Deep implementation reference for every mechanism |
| [`report.md`](report.md) | End-to-end test results against the live deployment |
| [`demo.md`](demo.md) | Timed 3-minute demo script with exact talking points |
| [`CHALLENGES.md`](CHALLENGES.md) | Real infrastructure bugs hit and fixed during development |
| [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md) | Target state: Bedrock, X-Ray, CI/CD |
| [`docs/VERIFICATION_RUNBOOK.md`](docs/VERIFICATION_RUNBOOK.md) | Copy-paste verification checklist |

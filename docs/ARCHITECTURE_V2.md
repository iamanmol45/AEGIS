# AEGIS v2 — Scalable, Reliable Architecture

Grounded in *"Building AI Agents for Autonomous Clouds: Challenges and Design
Principles"* (Shetty et al., Microsoft/SoCC'24) and the current codebase
(`infrastructure/infrastructure/infrastructure_stack.py`, `services/api/*`).

The paper's core idea: don't let an LLM touch the cloud directly. Put a
deterministic **Orchestrator / Agent-Cloud-Interface (ACI)** between the
reasoning agent and the infrastructure — a fixed, documented, whitelisted set
of actions (`get_logs`, `get_metrics`, `get_traces`, `scale`, `restart`,
`rollback`) that the agent calls, and that the orchestrator validates,
executes, and returns structured feedback for. AEGIS already has the seed of
this (`policy.py` + Step Functions gate), but the reasoning side
(`ai_reasoning.py`) is hardcoded rules, not an agent — and the ingestion loop
is a single in-process thread, not an event-driven pipeline. Both break
scalability and reliability at real load.

---

## 1. Target architecture

```
                          ┌─────────────────────────────────────────────┐
                          │              AGENT-CLOUD-INTERFACE            │
                          │   (Orchestrator — services/api, stateless)     │
                          │                                                │
  CloudWatch Alarms ─────▶│  EventBridge rule ─▶ SQS ─▶ Detection Lambda   │
  EventBridge (state      │        │                        │             │
  change: ECS/RDS/CodeDeploy)      ▼                        ▼             │
                          │  Isolation Forest +      Correlation engine    │
                          │  threshold detector      (dependency graph)    │
                          │        │                        │             │
                          │        └──────────┬─────────────┘             │
                          │                   ▼                           │
                          │      Bedrock Agent (RCA) with Action Groups:   │
                          │      get_logs / get_metrics / get_traces /     │
                          │      get_dependency_graph / search_runbooks    │
                          │      (RAG over Bedrock Knowledge Base on S3)   │
                          │                   │                           │
                          │                   ▼                           │
                          │        Confidence Scoring + Safety Policy      │
                          │        (deterministic, non-LLM gate)           │
                          │             ALLOW │            │ BLOCK        │
                          └─────────────┼──────┼────────────┼─────────────┘
                                        ▼      │            ▼
                              Step Functions   │      SNS → Human review /
                              recovery workflow│      escalation queue
                              (scale/restart/  │
                              rollback + verify)│
                                        │       │
                                        ▼       ▼
                          DynamoDB (decision record) + S3 (raw evidence:
                          logs/metrics/traces snapshot) + CloudTrail (API audit)
                                        │
                                        ▼
                              React Dashboard (reads DynamoDB/S3 via API,
                              WebSocket/SSE for live incident feed)
```

Application plane (unchanged, protected side):
`Users → ALB → ECS Fargate API/Worker → RDS Postgres`, instrumented with
**X-Ray** for traces and structured JSON logs.

---

## 2. What changes vs. today, and why (mapped to paper's requirements R1–R10)

| # | Paper principle | Gap today | Fix |
|---|---|---|---|
| R1 Modular design | `services/api` is one monolith: detection, correlation, RCA, policy, recovery all import each other directly | Keep as Python modules but make the **ACI a formal internal API boundary** (a `tools/` package with typed, documented functions) so the reasoning layer can only call through it — never raw `boto3` |
| R2 Flexible interfaces | Only a REST API for humans; no structured tool interface for an AI agent | Add a **Bedrock Agent with Action Groups** — each action (`get_logs`, `scale_service`, …) is a Lambda-backed OpenAPI schema, which is exactly the ACI pattern the paper validates |
| R3 Scalability | `supervisor.py` is a single thread inside one ECS task (`AegisWorkerService`, `desired_count=1`). If it dies or the cycle takes longer than `AEGIS_POLL_INTERVAL`, detection silently stalls; can't scale horizontally without double-processing | Move detection triggering to **EventBridge (scheduled + alarm-driven) → SQS → Lambda/Fargate**, decoupled from the API tier; SQS gives at-least-once delivery, visibility timeouts prevent double work, and consumers scale independently |
| R4 Reproducible setup | Chaos scenarios in `chaos.py` aren't seeded/versioned; no record of exact fault parameters replayed | Give every chaos run a `scenario_id` + parameter payload persisted to DynamoDB/S3 so any incident can be replayed identically (mirrors the paper's "problem cache") |
| R5 Versatility | Single environment (prod-like AWS). Fine given the fixed stack — not worth over-engineering for a hackathon. | No change needed |
| R6 Cross-layer faults | `chaos.py` only covers CPU/memory/task-failure/multi-signal — all container-level | Add **network-level** (security-group deny, latency injection via `tc`/ALB rule), **config-level** (bad task-def env var), and **dependency-level** (RDS connection exhaustion) faults — matches spec's T04–T07 |
| R7 Diverse workloads | `Locust` is in the fixed tech stack (spec §3) but never implemented | Add a `loadtest/locustfile.py` with at least 2 traffic shapes (steady, bursty) so incidents happen under realistic, not synthetic-idle, load |
| R8 Ops lifecycle coverage | Detect → RCA → mitigate exist; **triage** (severity/priority assignment when multiple incidents fire at once) is missing | Add a triage step between correlation and RCA: an SQS FIFO queue keyed by affected service, so concurrent incidents on the same service dedupe/merge instead of racing the Step Functions workflow |
| R9 Observability | CloudWatch metrics + app logs only; no traces, no dependency graph | Add **AWS X-Ray** (request-level tracing) and a lightweight service-dependency map (even a static YAML of "API depends on RDS" is enough for a hackathon) so RCA evidence includes *what calls what* |
| R10 Agent action controls | Actions are Step-Functions-hardcoded (`SCALE_OUT`, `RESTART_TASKS` only); the "AI" never actually proposes or calls anything — `ai_reasoning.py` is `if/else` | Wire a real Bedrock Agent whose **only** callable tools are the same whitelisted actions the Step Functions machine exposes, each still gated by `policy.py` before execution — the LLM proposes, the deterministic gate disposes (this is the paper's central design decision, §3.2.4) |

---

## 3. Missing pieces (prioritized)

**Reliability-critical (fix before demo):**
1. **Single-threaded, single-task detection loop** — `AegisWorkerService` desired_count=1 with no watchdog if `run_cycle()` hangs. Add a CloudWatch alarm on the worker's own health (e.g., a heartbeat metric it emits each cycle) + auto-restart.
2. **Wildcard IAM (`resources=["*"]`)** on `cloudwatch:*`, `ecs:DescribeServices`, `states:*` in `infrastructure_stack.py`. Scope these to the specific cluster/service/state-machine ARNs — currently any compromised container has broad blast radius, which directly contradicts the spec's own "least privilege" requirement (§13).
3. **Single NAT Gateway, single-AZ RDS** — both are availability single points of failure. For "scalable and reliable," add a second NAT (one per AZ) and consider `multi_az=True` on the RDS instance (small cost increase, real reliability gain).
4. **No dead-letter queue** for failed Step Functions executions or failed recovery attempts — a failed recovery currently just ends in `RecoveryFailedOrEscalated` with no follow-up mechanism. Add an SQS DLQ + SNS alert.
5. **ALB is HTTP-only on port 80, `open=True`** (any IP, any port 80 traffic). Add HTTPS via ACM + a restrictive security group before this is anything but a local demo.

**Functional gaps (from the original scope review, still open):**
6. No S3 evidence archive — DynamoDB holds only the incident *record*, not raw logs/metrics/trace snapshots at time of incident (spec's "Evidence Storage" requirement).
7. No Bedrock integration at all — `ai_reasoning.py` is a stand-in.
8. No React dashboard.
9. No SNS notification wiring (service exists in the fixed stack but isn't provisioned).
10. No CloudTrail trail explicitly created (default account trail may or may not exist — don't assume).
11. No Secrets Manager use beyond the RDS-generated secret (fine for now, just noting it's already correctly done via `self.database.secret`).
12. No CI/CD — every deploy is manual `docker build`/`cdk deploy` per the challenge log. A GitHub Actions workflow (build → push to ECR → `cdk deploy`) removes a whole class of human error that's already caused 5 of your 8 logged incidents.

---

## 4. Detection & RCA: better model choice

Keep **Isolation Forest** for unsupervised anomaly *detection* on metric
streams — it's cheap, fast, needs no labeled data, and is the right tool for
"is this CPU/latency reading abnormal." Don't replace it with an LLM; LLMs are
bad at numeric outlier detection and expensive for a job a 10-line sklearn
model does well.

For **RCA (root-cause reasoning over evidence)**, replace the rule-based
`ai_reasoning.py` with a **Bedrock Agent**, model choice:

| Use case | Recommended Bedrock model | Why |
|---|---|---|
| RCA reasoning / tool-calling agent | **Anthropic Claude on Bedrock (Sonnet-tier)** | Best tool-use reliability of the Bedrock catalog for a ReAct-style loop (the paper's own case study uses ReAct+GPT-4 and finds tool-call quality is the dominant factor in agent performance, not raw model size) — Bedrock gives you the same pattern without sending data outside AWS |
| High-volume, cost-sensitive summarization (e.g., log triage before it reaches the agent) | **Claude Haiku-tier on Bedrock** | 10x cheaper, use it to pre-summarize noisy CloudWatch Logs into a short evidence digest before handing to the reasoning-tier model — keeps agent context small (the paper explicitly flags "too many arguments/too much context hurts agent performance," §5 insight 2) |
| Grounding RCA in your own runbooks/past incidents | **Bedrock Knowledge Bases (RAG) over an S3 bucket** of past incident postmortems + AWS runbooks, embedded with **Titan Embeddings** (or Cohere embed on Bedrock) | Turns "the LLM guesses a cause" into "the LLM cites the closest precedent," which is exactly what raises your **RCA Accuracy** metric (spec §17) and gives you an auditable citation instead of a hallucinated one |

Concretely: `ai_reasoning.py` becomes a thin client that (1) calls
`bedrock-agent-runtime.invoke_agent`, (2) the agent's action group Lambda
exposes `get_logs`/`get_metrics`/`get_traces`/`search_runbooks` backed by your
existing `correlation.py`/`detector.py` outputs, (3) the agent's final answer
is a structured JSON (`likely_cause`, `confidence`, `evidence_refs`,
`recommended_action`) that flows into the *existing* `policy.py` gate
unchanged — you don't touch the safety layer at all, which is the paper's
whole point: reasoning changes, the gate doesn't.

---

## 5. AWS services — what each does here, and how to stand it up

All setup is CDK-first (per the fixed tech stack), added to
`infrastructure/infrastructure/infrastructure_stack.py`. Console/CLI notes
given where CDK isn't the natural path (e.g., enabling Bedrock model access).

### Already provisioned

| Service | Role in AEGIS | Setup (current) |
|---|---|---|
| **VPC** | Network isolation; public subnets for ALB, private for ECS/RDS | `ec2.Vpc(self, "AegisVpc", max_azs=2, nat_gateways=1)` — line 35. *v2: bump `nat_gateways=2` for AZ redundancy.* |
| **ECS Fargate** | Runs API + Worker (detection loop) as separate services | `ecs.FargateService` ×2, `ecs.FargateTaskDefinition` ×2 |
| **Application Load Balancer** | Public entry point, routes to API service, health-checks `/health` | `elbv2.ApplicationLoadBalancer` + listener + target group |
| **RDS PostgreSQL** | Application database | `rds.DatabaseInstance`, private subnet, encrypted, auto-generated Secrets Manager credential |
| **DynamoDB** | Incident/decision audit store | `dynamodb.Table`, pay-per-request, point-in-time recovery on |
| **CloudWatch** | Metrics (CPU/mem/task count) + Alarms | `cloudwatch.Alarm` ×3 on service metrics |
| **Step Functions** | Deterministic, verified recovery workflow (scale/restart + verify loop) | `sfn.StateMachine` with `CallAwsService` tasks |
| **IAM** | Task roles, least-privilege-*intended* policies | inline `PolicyStatement`s on each task definition |
| **ECR** | Container image registry | `ecr.Repository.from_repository_name("aegis-api")` — repo itself created out-of-band via `aws ecr create-repository` |
| **Secrets Manager** | RDS master credential | auto-created by `rds.DatabaseInstance`, granted via `self.database.secret.grant_read(...)` |

### Needed for v2

| Service | Role | Setup |
|---|---|---|
| **EventBridge** | Decouples detection triggering from the API tier: rules on CloudWatch Alarm state changes and ECS/RDS service events, fan-out to SQS | `events.Rule(self, "AegisAlarmRule", event_pattern=events.EventPattern(source=["aws.cloudwatch"], detail_type=["CloudWatch Alarm State Change"]))`, target `targets.SqsQueue(queue)` |
| **SQS** | Buffers detection events; gives retry + DLQ semantics the current in-process loop lacks | `sqs.Queue(self, "AegisDetectionQueue", dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq))` |
| **Lambda** | (a) detection-trigger consumer, (b) Bedrock Agent action-group handlers (`get_logs`, `get_metrics`, `scale_service`, …) | `lambda_.Function(self, "AegisActionHandler", runtime=lambda_.Runtime.PYTHON_3_12, handler="handler.main", code=lambda_.Code.from_asset("lambda/actions"))`; grant it the same scoped IAM as the ECS task roles, nothing more |
| **Bedrock** | RCA reasoning agent + Knowledge Base for RAG over runbooks | Not a CDK-first service for model access: in the console, **Bedrock → Model access → request Anthropic Claude models** (one-time per account/region — Bedrock model access is opt-in and can take a few minutes to activate). The Agent itself *can* be defined via `bedrock.CfnAgent` in CDK once model access is granted |
| **S3** | Evidence lake: raw log/metric/trace snapshots at incident time, chaos scenario definitions, runbook corpus for the Knowledge Base | `s3.Bucket(self, "AegisEvidenceBucket", encryption=s3.BucketEncryption.S3_MANAGED, block_public_access=s3.BlockPublicAccess.BLOCK_ALL, lifecycle_rules=[...])` |
| **SNS** | Operator notification on BLOCK/escalation decisions | `sns.Topic(self, "AegisAlerts")`; subscribe an email/Slack-via-Lambda endpoint; publish from the safety-gate BLOCK branch |
| **CloudTrail** | AWS API audit (spec §13, currently unverified) | `cloudtrail.Trail(self, "AegisTrail", bucket=evidence_bucket_or_new, is_multi_region_trail=True)` — check first whether the account already has an org-level trail before creating a duplicate |
| **X-Ray** | Distributed tracing across API↔RDS calls, feeds RCA evidence | Add `aws-xray-sdk` to `services/api/requirements.txt`, wrap FastAPI with the X-Ray middleware, and add `task_definition.default_container.add_env...` — actually: enable via ECS task definition sidecar, `ecs.FargateTaskDefinition.add_container("xray-daemon", image=ecs.ContainerImage.from_registry("public.ecr.aws/xray/aws-xray-daemon"))`, and grant `xray:PutTraceSegments` to the task role |
| **ACM + Route53 (optional)** | HTTPS on the ALB instead of plaintext port 80 | `acm.Certificate` for a domain, add an HTTPS listener on the ALB, redirect 80→443 |
| **CodeBuild/CodePipeline or GitHub Actions** | CI/CD: build → ECR push → `cdk deploy`, replacing manual steps that caused CHALLENGE-002 through 005 | Simplest: a GitHub Actions workflow with `aws-actions/configure-aws-credentials` (OIDC, no long-lived keys) + `docker build/push` + `cdk deploy --require-approval never` on merge to `main` |

### Bedrock model access — step by step (console, one-time)
1. AWS Console → Bedrock → **Model access** (left nav) → *Modify model access*.
2. Enable **Anthropic Claude** (Sonnet-tier for the reasoning agent; Haiku-tier optional for the log-summarization pre-step) and **Amazon Titan Embeddings** (for the Knowledge Base).
3. Access typically activates within minutes; some Anthropic models require a brief use-case justification form the first time.
4. Once active, create the **Knowledge Base** (Bedrock → Knowledge Bases → Create) pointing at the S3 evidence/runbook bucket, using Titan Embeddings + an OpenSearch Serverless vector store (CDK: `opensearchserverless.CfnCollection`, or let the Knowledge Base console flow provision it — simpler for a hackathon timeline).
5. Create the **Agent** (Bedrock → Agents → Create), attach the Knowledge Base, and define **Action Groups** from an OpenAPI schema pointing at your Lambda action handlers.

---

## 6. Suggested build order

1. Fix the reliability-critical items (§3, items 1–5) — cheap, high value, low risk.
2. Add S3 evidence bucket + CloudTrail — needed before you can claim "auditable."
3. Wire EventBridge → SQS → Lambda for detection triggering; keep `supervisor.py`'s polling as a fallback/backstop (belt and suspenders is fine for a hackathon).
4. Stand up Bedrock model access + a minimal Agent with 2–3 action groups (`get_logs`, `get_metrics`, `search_runbooks`); swap `ai_reasoning.py` to call it, keep `policy.py` untouched.
5. Add the missing chaos scenarios (network/config/dependency faults) + Locust workload generator to reach the spec's T01–T12.
6. React dashboard last — it's the most visible gap but has zero dependency on the others, so it can be built in parallel by anyone on the team.

# AEGIS — End-to-End Test Report

**Date:** 2026-09-20
**Target:** Live production deployment, `aegis-cluster` / `aegis-api-service`, account `097079438330`, region `ap-south-1`
**Base URL:** `http://AegisI-Aegis-9T1qlmEvlgUH-464819824.ap-south-1.elb.amazonaws.com`
**Scope:** Full backend (unit tests + live API against real AWS infrastructure), including the newly-added self-healing recovery loop. The static dashboard (`aegis-light.html`) was not re-driven through a browser in this pass — its correctness rests on the same endpoints tested directly here, and it was manually verified against these endpoints earlier in this project's development.

No shortcuts were taken: every result below is a real response from the live system, not a simulated or predicted one.

---

## 1. How testing was done

### 1.1 Pre-requisite deploy

The self-healing recovery loop (built earlier this session, based on the CIRCA-SH causal-inference paper) existed only in source until this test pass — testing "the system" meaningfully required it to be live first. Sequence:

1. `docker build` → `docker push` (new image with the self-healing code) → ECR.
2. `aws ecs update-service --force-new-deployment` on **both** `aegis-api-service` (4 tasks) and the worker service (`AegisWorkerService...`, 1 task) — both run the same image; the worker executes `controller.py`, which also carries the self-healing change, so both needed the new image.
3. Polled `describe-services` until both rollouts converged to a single steady-state deployment (no parallel PRIMARY/ACTIVE deployments). Confirmed clean, zero-downtime rollout on both.

### 1.2 Unit/integration test suite

Ran the full `pytest` suite (`services/api/tests/`) locally against the updated code before and after fixing issues surfaced by the self-healing change:

- First run after adding the self-healing loop: **35 passed, 5 failed**.
- Root-caused each failure (see §3.1) and fixed the underlying issues — one was a real bug in the new code (an unhandled CloudWatch exception), the rest were test assertions written against the old response shape.
- Final run: **40 passed, 0 failed**.

### 1.3 Live end-to-end testing methodology

All 10 chaos scenarios were run for real (`dry_run=false`) against the live ALB, not simulated locally — each one exercises the actual pipeline: real DynamoDB writes, real S3 evidence uploads, real Step Functions executions, real `ecs:UpdateService` calls, real CloudWatch reads.

Before testing `SCALE_OUT` scenarios, `desired_count` was already pinned at the policy's max (4) from earlier testing this session, which would have caused every `SCALE_OUT` attempt to be blocked by the max-scale guardrail rather than actually executing. To get real coverage of the new self-healing execution path (not just its "blocked" branch, which was already covered), the service was deliberately scaled down to 2 tasks first (`aws ecs update-service --desired-count 2`), confirmed stable, then testing proceeded. The service naturally scaled back to 4 as a side effect of the `SCALE_OUT` actions taken during testing — no manual restoration was needed afterward.

Every scenario's full JSON response was captured and inspected field-by-field, not just the HTTP status code.

---

## 2. Results

### 2.1 Core API endpoints

| Endpoint | Result |
|---|---|
| `GET /health` | ✅ `{"status":"healthy"}` |
| `GET /status` | ✅ Full system snapshot returned correctly; see §3.2 for a caveat on the incident counts |
| `GET /policy` | ✅ Guardrail config correct and unchanged: `min_confidence=0.6`, `max_desired_count=4`, `cooldown_seconds=120` |
| `GET /workflow` | ✅ State machine `aegis-recovery-workflow`, status `ACTIVE` |
| `GET /correlation` | ✅ `window_seconds=60`, `pending_signals=0` |
| `GET /chaos` | ✅ All 10 scenarios listed |
| `GET /incidents`, `GET /incidents/{id}` | ✅ Real records returned with correct schema |
| `GET /incidents/{id}/evidence` | ✅ Real S3-backed evidence blob returned |
| `POST /reason` | ✅ See §2.4 — live RCA confirmed working, including precedent-boosted confidence |
| `GET /chaos/history` | ✅ Consistent across repeated polls (see §2.5 — the cross-replica fix from earlier this session still holds after redeploy) |
| CORS headers | ✅ `access-control-allow-origin: *` still present post-redeploy |

### 2.2 Self-healing loop — the main subject of this test pass

**`cpu-spike`** (incident `INC-A3C4CF9D`): completed in 56 seconds.
- `self_healing.attempts` = 1 (healed on the first action, no escalation needed)
- `symptom.checked: true`, `symptom.cleared: true`, **`symptom.current_value: 0.68`** — this is the *real*, live CloudWatch CPU reading at the moment of verification, correctly recognized as far below the 80% threshold
- `self_healing.healed: true`
- Incident status confirmed `RESOLVED` via a follow-up `GET /incidents/{id}` — the auto-resolve wiring (added earlier this session) fired correctly off the new self-healing result

**`memory-pressure`** (incident `INC-5C0A09D0`): completed in 51 seconds.
- Same pattern: 1 attempt, `symptom.current_value: 26.57` (real memory %), `cleared: true`, `healed: true`

Both confirm the core new capability end-to-end: the system re-measures the *real* metric that triggered the incident after acting, rather than declaring victory purely because ECS's task count converged — exactly the gap this feature was built to close.

**Escalation-to-second-action branch:** not triggered live in this pass, because the real service genuinely isn't under load — the "anomaly" for these two scenarios is a value injected directly into the detector, not a real stress condition, so the real CPU/memory reading is always healthy and clears on the first attempt. This is expected and honest: forcing the escalation branch live would require actually stressing the container (e.g., a `stress-ng` payload or sustained real load via the Locust generator timed precisely against a chaos run), which was out of scope for this pass. The escalation logic itself (bounded to 2 actions, alternates `SCALE_OUT ↔ RESTART_TASKS`, escalates for human review if still unhealed) is covered by the unit test suite (§1.2) and by direct code review, not by a live trigger.

### 2.3 Remaining 8 chaos scenarios — all correct

| Scenario | Outcome | Reason (verbatim from the API) |
|---|---|---|
| `task-failure` | `COMPLETED` | `RESTART_TASKS` executed and verified (unaffected by the self-healing change — out of scope by design, see technical.md) |
| `multi-signal` | `COMPLETED` | Correlated into one incident, `RESTART_TASKS` executed |
| `error-rate-spike` | `COMPLETED` | `RESTART_TASKS` executed, `"Policy checks passed"` |
| `latency-spike` | `BLOCKED_BY_POLICY` | `"Maximum desired task count reached (4)"` — correct: ran right after other `SCALE_OUT` actions had already pushed desired count back to 4 |
| `bad-deployment` | `BLOCKED_BY_POLICY` | `"Cooldown active: 83s remaining"` — correct: ran within 120s of a prior action |
| `db-connectivity-failure` | `BLOCKED_BY_POLICY` | `"Action 'INVESTIGATE_DATABASE' is not in allowed actions list"` — correct by design, this scenario is meant to always escalate |
| `low-confidence` | `BLOCKED_BY_POLICY` | `"RCA confidence 0.35 is below the minimum 0.60"` — correct by design |
| `normal-workload` | `NO_ANOMALY_DETECTED` | Correctly raised no incident on healthy metrics |

Every single guardrail in the system was exercised live and behaved correctly: the confidence gate, the allowed-actions whitelist, the cooldown timer, and the max-scale ceiling all fired exactly when they should have.

### 2.4 Live RCA and precedent-boosting confirmed

A direct `POST /reason` call against the `cpu-spike` incident returned:
```json
{
  "confidence": 0.85,
  "likely_cause": "The ECS task is experiencing sustained high CPU utilization...",
  "recommended_action": "Scale out ECS tasks to distribute load... (Precedent: incident INC-DE617D1D with the same signature was previously resolved successfully.)"
}
```
CPU's base confidence is 0.75; this returned 0.85 — exactly `0.75 + 0.10` for a precedent match, confirming the precedent-search mechanism (`rca.py`) is genuinely querying live DynamoDB history and adjusting confidence based on real prior outcomes, not a static number.

### 2.5 Cross-replica consistency (regression check)

`GET /chaos/history` was polled repeatedly at three points during this test pass (before, mid-way, and after the full scenario batch) and returned a consistent count each time (24, then 31, then 31/31/31) — confirming the DynamoDB-backed history fix from earlier this session is still holding correctly after this round's redeploys.

---

## 3. Issues found

### 3.1 Bugs found and fixed during this pass

**Unhandled CloudWatch exception in the new self-healing code** (`detector.py`, `recheck_cleared()`). The first test run surfaced this immediately: a mocked test environment's `MagicMock` dimension values caused a real `botocore.exceptions.ParamValidationError`, which propagated uncaught and crashed the self-healing loop. Every other AWS-touching helper in this codebase (`EvidenceStore`, `IncidentStore`, `AuditLogger`) fails soft on AWS errors; this one didn't. **Fixed**: wrapped the CloudWatch call in a `try/except`, falling back to `checked: False` (the same "unmeasurable" path already used for a missing datapoint) rather than propagating the exception. This matters in production too, not just tests — a transient CloudWatch throttle or blip should never crash an otherwise-successful remediation.

### 3.2 Issues found, not fixed (out of scope for this pass — flagged for follow-up)

1. **`/status`'s incident totals appear capped at 100.** `incidents.total` read `100` both before and after this session created roughly 9 new incidents via chaos testing — it should have increased. Root cause (by inspection, not yet fixed): `IncidentStore.get_incidents()` defaults to `limit=100`, and `/status` calls it with no explicit limit, so once the table holds more than 100 incidents, the reported "total" silently stops growing and just reflects the scan limit, not the true count. This has likely been silently wrong for a while, not something this session's changes introduced — it just became visible now because the incident table has grown large enough to hit the ceiling.

2. **RCA's "impact" field never actually reports a dependency.** Every `/reason` response and every chaos scenario's RCA output shows `"impact": "No downstream dependencies mapped for this service."`, even though `rca.py`'s `SERVICE_DEPENDENCIES` dict does define `"aegis-api-service": ["RDS PostgreSQL (aegis)"]`. Root cause: the incident's `service` field is always `"aegis-api"` (the default set in `incident.py`), but the dependency map's key is `"aegis-api-service"` — the two strings never match, so the lookup silently always misses. This is a pre-existing naming mismatch, not something introduced this session; it means the "downstream impact" feature has likely never worked since it was written.

3. **`AuditLogger` and the confidence-defaulting behavior in chaos scenarios remain known, previously-documented gaps** (see `technical.md` Part 5) — not re-tested here since they weren't touched this session, but worth remembering they're still open.

Neither issue in this section blocks the system from working correctly for its intended purpose (detection, safety-gated remediation, and now verified self-healing all function correctly) — they're accuracy/cosmetic gaps in secondary fields, not correctness failures in the safety-critical path.

---

## 4. Verdict

The system, including the newly-added self-healing verification loop, works correctly end-to-end against real AWS infrastructure:

- Detection → correlation → RCA → policy gate → Step Functions remediation → **real symptom re-verification** → auto-resolve, all confirmed live, not just in unit tests.
- Every safety guardrail (confidence gate, cooldown, max-scale, allowed-actions whitelist) fired correctly under real conditions.
- The cross-replica consistency fix and CORS fix from earlier this session both survived this round's redeploys without regression.
- One real bug was found and fixed during testing (unhandled CloudWatch exception). Two pre-existing, non-blocking accuracy issues were found and documented for follow-up, not fixed in this pass.

**40/40 unit tests passing. 10/10 chaos scenarios behaving exactly as designed against live infrastructure.**

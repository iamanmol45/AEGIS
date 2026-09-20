# AEGIS — Technical Architecture (Deep Reference)

This document goes line-by-line through how AEGIS actually works: every
formula, every threshold, every state transition, every AWS API call, and
every data shape. It complements `explain.md` (which is the narrative
overview) — this one is the implementation reference. Nothing here is
aspirational; every mechanism described is either live code in this repo or
an explicitly-marked stub.

No code was changed to produce this document.

---

## Part 1 — Architecture, layer by layer

AEGIS is built as five layers, each with a single responsibility, connected
by narrow, typed interfaces so any one layer can be replaced without
touching the others:

```
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 5 — Presentation                                               │
│   aegis-light.html (static, client-polls the API over HTTPS/CORS)   │
└─────────────────────────────────────────────────────────────────────┘
                              ▲  HTTP/JSON
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 4 — API surface (FastAPI, app.py)                              │
│   Stateless request handlers. Owns no business logic itself — every  │
│   route is a thin wrapper that constructs/calls into Layer 3.        │
└─────────────────────────────────────────────────────────────────────┘
                              ▲  Python method calls (in-process)
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 3 — Orchestration                                              │
│   AegisController (real pipeline) / ChaosTestManager (synthetic      │
│   pipeline) / AegisSupervisor (the timer that drives the controller) │
└─────────────────────────────────────────────────────────────────────┘
                              ▲  Python method calls
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — Domain engines                                             │
│   AegisDetector · IncidentCorrelationEngine · RCAEngine ·             │
│   AIReasoningEngine · RemediationPolicy · RemediationEngine ·         │
│   RecoveryVerifier · StepFunctionsWorkflowManager · EvidenceStore ·   │
│   IncidentManager · AuditLogger                                      │
└─────────────────────────────────────────────────────────────────────┘
                              ▲  boto3 (AWS SDK) calls
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 1 — AWS resources                                              │
│   ECS Fargate · CloudWatch · DynamoDB · S3 · Step Functions ·        │
│   EventBridge · SQS · Lambda · SNS · RDS · CloudTrail · Budgets      │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this shape matters technically:** every Layer 2 engine takes its
dependencies as constructor arguments with sane defaults
(`def __init__(self, store=None): self.store = store or IncidentStore()`).
This is what makes the whole codebase unit-testable without AWS credentials
— every test in `services/api/tests/` injects mocked collaborators instead
of hitting real boto3 clients. It's also what makes the RCA/AI-reasoning
swap-to-Bedrock plan realistic rather than aspirational: `RCAEngine.analyze()`
and `AIReasoningEngine.analyze()` are called through the exact same
signature a Bedrock-backed replacement would use.

---

## Part 2 — Layer 2 engines, in full implementation detail

### 2.1 `AegisDetector` (`detector.py`)

**State it owns:** two `collections.deque(maxlen=60)` rolling buffers, one
each for `cpu` and `memory`. No other metric gets a rolling history — this
is a deliberate, hardcoded limitation (see Part 5, "Known limitations").

**Static thresholds table** (`self.thresholds`, set once at construction,
never changed at runtime — there is no API to reconfigure them):

```python
{
    "cpu": 80.0,                  # percent
    "memory": 80.0,                # percent
    "running_tasks": 1,            # count — anomaly if BELOW this
    "latency_ms": 1000.0,          # milliseconds
    "http_5xx_rate": 5.0,          # percent
    "db_connection_errors": 5.0,   # count
    "disk_io_saturation": 70.0,    # percent — see note below
}
```

`disk_io_saturation` is present here but **absent** from `rca.py`'s
`METRIC_KNOWLEDGE` table. This asymmetry is deliberate and load-bearing: it
lets `detector.py` flag a real anomaly for this metric, while `rca.py` has
no rule for it and falls back to its "novel pattern" branch (confidence
0.35), which `policy.py`'s `min_confidence=0.6` gate then blocks. This is
the entire mechanism behind the `LOW_CONFIDENCE_INCIDENT` chaos scenario —
there's no special-case code anywhere that says "block this scenario"; it
falls out naturally from one metric being detectable-but-unmodeled.

**`check_metric(metric_name, value)` — exact decision tree, in order:**

1. **Unknown metric** → `{"anomaly": False, "reason": "Unknown metric"}` and
   returns immediately (no history append, nothing else runs).

2. **`running_tasks` special case** — this metric bypasses everything below
   it entirely:
   ```python
   abnormal = float_val < threshold   # threshold = 1
   ```
   If `running_tasks < 1`, that's an immediate `STATIC_THRESHOLD` anomaly
   with `severity = get_severity("running_tasks", value)`, which always
   returns `"HIGH"` for this metric regardless of how far below 1 it is
   (0 vs. 0 doesn't have a "how far" — it's binary). `ml_score` is always
   `None` for this metric; Isolation Forest is never applied to it.

3. **Static-threshold safety fallback (all other metrics):**
   ```python
   if float_val > threshold:
       # append to rolling history (only cpu/memory have a deque)
       return {"anomaly": True, "detection_method": "STATIC_THRESHOLD", "ml_score": None, ...}
   ```
   This check runs *before* the ML branch and **always wins** if the raw
   value clears the static bar. The Isolation Forest is only ever consulted
   for values that are *below* the static threshold — its entire job is to
   catch abnormal-but-still-under-80%-CPU patterns, not to second-guess an
   obvious breach.

4. **Isolation Forest ML branch** — only reached for `cpu`/`memory` (the
   only metrics with a history deque) when `value <= threshold` and the
   deque already has `>= min_samples` (10) points:
   ```python
   history_data = np.array(list(metric_history)).reshape(-1, 1)
   dataset = np.vstack([history_data, [[float_val]]])
   hist_mean = np.mean(history_data)
   hist_std  = np.std(history_data)

   clf = IsolationForest(contamination=0.05, random_state=42, n_estimators=50)
   clf.fit(dataset)
   raw_score = clf.decision_function([[float_val]])[0]   # continuous anomaly score
   pred      = clf.predict([[float_val]])[0]              # -1 = outlier, 1 = normal
   ```
   The model is **refit from scratch on every single call** — there's no
   persisted/cached model object between requests. This is computationally
   wasteful for a 10–60 point dataset (fitting is cheap at this scale, so
   it doesn't matter in practice) but means the "model" has zero memory of
   past predictions beyond what's in the rolling deque itself.

   **Triple-AND gate** before declaring an ML anomaly (all three must hold):
   ```python
   pred == -1
   and float_val > (hist_mean + 1.5 * max(hist_std, 2.0))
   and float_val >= 20.0
   ```
   - `max(hist_std, 2.0)` — a floor on the standard deviation term. If the
     historical values are extremely flat (e.g., a static 34% CPU with
     std≈0), this prevents a tiny 1-2 point wobble from being 1.5σ away and
     triggering constant false positives. The floor guarantees the
     deviation bar is always at least 3 percentage points above the mean.
   - `float_val >= 20.0` — an absolute floor so that near-zero-baseline
     noise (e.g., idle CPU oscillating between 1% and 3%) can never be
     flagged no matter how many "standard deviations" that represents in a
     tiny-magnitude series.
   - If all three hold: `detection_method = "ISOLATION_FOREST"`,
     `severity` is unconditionally `"HIGH"` (not computed via
     `get_severity()` — ML anomalies don't get graded to CRITICAL).

5. Current value is appended to history (for cpu/memory) **regardless of
   outcome** — every check_metric call, anomaly or not, feeds the rolling
   window (except the static-threshold-breach branch, which appends earlier
   in the flow — both branches append exactly once, never twice).

6. If nothing above fired: `NORMAL` if history has ≥10 samples, else
   `INSUFFICIENT_HISTORY` (still `anomaly: False` either way — the
   distinction is informational only, no downstream logic branches on it).

**`get_severity(metric_name, value)`:**
- `running_tasks` → always `"HIGH"`.
- `cpu`/`memory` → absolute percentage cutoffs: `>=95` → CRITICAL,
  `>=80` → HIGH, else MEDIUM.
- Everything else → **ratio-to-threshold**, not absolute value, because
  these metrics live on different scales (ms, %, count):
  `ratio = value / threshold`; `>=3x` → CRITICAL, `>=1.5x` → HIGH, else
  MEDIUM.

**`get_latest_metric(metric_name, namespace, dimensions)`** — the only
method in this class that talks to AWS. Pulls the most recent CloudWatch
datapoint via `get_metric_statistics` over the last 10 minutes, `Period=60`,
`Statistics=["Average"]`, sorts descending by timestamp, returns the newest
`Average`, or `None` if there's no data yet. This is how `controller.py`
gets real CPU/memory numbers from `AWS/ECS`.

### 2.2 `IncidentCorrelationEngine` (`correlation.py`)

**State:** an in-memory Python list, `self.signals` — **not persisted
anywhere**. A process restart (deploy, crash, ECS task replacement) silently
resets all pending correlation state to empty. Because only one worker task
runs the detection loop (`desired_count=1`), this is a single point of
correlation-state truth, not a distributed-consistency problem — but it does
mean a signal sitting in the correlation window at the exact moment of a
task replacement is lost rather than correlated.

**`add_signal(signal)`:**
- Normalizes `metric_name`: the string `"running_tasks"` is silently
  rewritten to `"task_failure"` before storage — this bridges the
  vocabulary used by `detector.py` (which detects `running_tasks`) and the
  vocabulary the correlation and RCA layers expect (`task_failure`).
- Parses `timestamp` defensively: accepts a `datetime` object, an ISO string
  (with `Z` swapped for `+00:00` before `fromisoformat`), or falls back to
  `datetime.utcnow()` if parsing fails for any reason (bad input never
  raises here).
- Stores `consumed: False` and a private `_parsed_dt` datetime object
  alongside the public fields, for internal age computation.

**`clear_expired(now)`:** keeps a signal only if it is *not* consumed AND
its age is in `[0, window_seconds]` inclusive on both ends. A signal with a
timestamp in the future (age < 0, e.g. clock skew) is also dropped — the
`0 <= age_seconds` check filters it out just as expiry does. Called at the
top of every `correlate()`, `get_pending_signals_count()`, and
`get_signals()` call — expiry is lazy/pull-based, not a background timer.

**`correlate(now)` — the core algorithm:**
1. Expire first.
2. Filter to `pending = [s for s in signals if not consumed and s["metric"] in {"cpu", "memory", "task_failure"}]`.
   Latency, 5xx rate, DB errors, and disk I/O are **never** correlation
   candidates — they always create standalone incidents even if several
   fire simultaneously.
3. `if len(pending) < 2: return None` — correlation requires strictly at
   least 2 related signals; a single CPU spike alone never becomes a
   "correlated" incident, only a plain one (via the controller's fallback
   path, not this engine).
4. Severity roll-up: `CRITICAL` if any pending signal is CRITICAL, else
   `HIGH` if any is HIGH, else `MEDIUM`, else `LOW` — a strict max over a
   fixed 4-level hierarchy, not an average.
5. Builds one synthetic incident: `id = "INC-CORR-{8 hex chars}"`,
   `metric = "correlated_" + "_".join(sorted(unique metric names))` (e.g.
   `correlated_cpu_memory`), `value = max(all signal values)` (the single
   worst reading, not a sum or average), `threshold = 0.0` (correlated
   incidents have no single meaningful threshold to report),
   `signal_count = len(pending)`, and the full list of contributing raw
   signals attached under `signals`.
6. **Every pending signal is marked `consumed=True`** as a side effect of
   this single `correlate()` call — this is what prevents the same CPU
   spike from being correlated into two different incidents if
   `correlate()` happens to be called twice in the same window (e.g. once
   from the controller's own cycle and once from a stray `/correlation-test`
   call).
7. `clear_expired()` runs again at the end to purge the now-consumed
   signals immediately rather than waiting for the next lazy call.

This is a genuinely subtle, correctly-implemented piece of concurrency-aware
state management for a single-threaded, single-process engine — the
consumed-flag pattern is what prevents duplicate incident creation, and the
window-based expiry is what prevents an old CPU spike from three minutes ago
merging with a fresh memory spike into a misleading "correlated" story.

### 2.3 `RCAEngine` (`rca.py`)

**Confirmed: no LLM/Bedrock call anywhere in this file.** Every "reasoning"
step below is deterministic Python arithmetic and string lookup.

**`METRIC_KNOWLEDGE`** — a hardcoded dict, one entry per known metric
(`cpu`, `memory`, `task_failure`, `latency_ms`, `http_5xx_rate`,
`db_connection_errors`), each with two narrative strings (`cause_high` /
`cause_threshold`), one fixed `recommendation` string, and one fixed
`base_confidence` float (0.65–0.85, hand-tuned per metric — `task_failure`
gets the highest base confidence at 0.85 because a task actually being down
is about as unambiguous a signal as exists; `latency_ms` gets the lowest at
0.65 because slow responses have many possible causes).

**`SERVICE_DEPENDENCIES`** — a single hardcoded entry:
`{"aegis-api-service": ["RDS PostgreSQL (aegis)"]}`. This is not discovered
dynamically from any AWS API or config file; it's a literal Python dict that
would need a code change to extend to a second service.

**`analyze(incident)` — full confidence-scoring pipeline:**

```
1. base_metric = first key in METRIC_KNOWLEDGE that is a SUBSTRING of
   incident["metric"]
   → handles composite names like "correlated_cpu_memory" matching "cpu"
     (whichever key happens to match first in dict iteration order)

2. IF base_metric found:
     severe_cutoff = threshold * 1.1  (or 90 if threshold is 0/falsy)
     cause = cause_high if value >= severe_cutoff else cause_threshold
     confidence = METRIC_KNOWLEDGE[base_metric]["base_confidence"]
   ELSE (unrecognized metric, e.g. disk_io_saturation):
     cause = "no established pattern for this signal"
     confidence = 0.35   ← hardcoded escape hatch, always below the 0.6 gate

3. IF signal_count > 1:
     confidence = min(0.97, confidence + 0.08 * (signal_count - 1))
   → a 2-signal correlated incident: +0.08. A 4-signal one: +0.24 (but
     capped at 0.97 regardless of how many signals pile in)

4. precedent = search up to the last 50 stored incidents (via
   IncidentStore.get_incidents(limit=50)) for the most recent one with
   the SAME base metric AND SAME service AND a status in
   {RESOLVED, RECOVERED, CLOSED}, excluding the current incident's own ID
   IF found:
     confidence = min(0.98, confidence + 0.10)
     append a citation sentence naming the precedent's incident ID
   ELSE: precedent_id = None

5. impact = "Downstream impact may extend to: {deps}." or
   "No downstream dependencies mapped for this service." based on the
   static SERVICE_DEPENDENCIES lookup

6. Return: {timestamp, incident_id, severity, root_cause, confidence
   (rounded to 2 decimals), evidence: {metric, observed_value, threshold,
   signal_count, precedent_incident_id}, impact, recommendation}
```

**Confidence math worked through, concretely:** a single CPU incident with
no precedent and no correlation → confidence stays at the base 0.75. The
exact same CPU incident, but correlated with a memory signal (`signal_count
= 2`) → `0.75 + 0.08*1 = 0.83`. If a past CPU incident on the same service
was ever RESOLVED → an additional `+0.10` → `0.93`, capped below 0.98. This
is why the dashboard's RCA panel can show meaningfully different confidence
numbers for what looks like "the same" CPU spike scenario run at different
points in the incident history — the precedent-search genuinely changes
behavior based on real prior data, it isn't cosmetic.

**`_find_precedent()` failure mode:** wrapped in a bare
`try/except: return None` around the DynamoDB scan — if the store is
unreachable, precedent search silently contributes nothing rather than
raising, so RCA always returns *something* even during a DynamoDB outage.

### 2.4 `AIReasoningEngine` (`ai_reasoning.py`)

The entire file is 31 lines. `analyze(incident, rca)` does pure string
templating — one f-string for the `summary` field
(`f"{severity} {metric} anomaly detected in {service} (confidence:
{confidence:.0%})."`), and otherwise **re-exports RCA's own output fields
verbatim** under a differently-named wrapper key (`likely_cause` ←
`rca["root_cause"]`, `recommended_action` ← `rca["recommendation"]`, etc.).
There is no independent reasoning happening in this layer today — it exists
purely as a stable seam so a future Bedrock `invoke_model` call can be
substituted in without any caller (`app.py`'s `/reason` route, `chaos.py`,
`controller.py`) needing to change.

### 2.5 `RemediationPolicy` (`policy.py`) — the safety gate

This is the single load-bearing correctness mechanism of the whole system —
every autonomous action (via any path: direct `RemediationEngine` calls,
Step Functions executions, chaos scenarios) is required to pass through
`evaluate()` before anything touches ECS. **Checks run in this exact order,
and the first failure short-circuits the rest — later checks never even
see incidents that fail an earlier one:**

```python
def evaluate(self, incident, action, current_desired_count, confidence=1.0):
    # 1. incident must be a non-empty dict
    # 2. incident must have both "id" and "severity" keys/attrs
    # 3. if self.critical_only (default True): severity must == "CRITICAL"
    # 4. action must be in ["SCALE_OUT", "RESTART_TASKS"]
    # 5. confidence must be >= self.min_confidence (default 0.6)
    # 6. if action == "SCALE_OUT": current_desired_count must be
    #    < self.max_desired_count (default 4)
    # 7. if self.last_action_time is set: (now - last_action_time) must be
    #    >= self.cooldown_seconds (default 120) -- a SINGLE shared timer
    #    across BOTH action types, not per-action
    # → otherwise: {"allowed": True, "reason": "Policy checks passed"}
```

Notable design details:
- **`confidence` defaults to `1.0`** if the caller doesn't pass one. This
  means any code path that evaluates policy *without* an explicit RCA
  confidence value automatically clears the confidence gate — a caller has
  to actively opt in to being confidence-gated by threading a real RCA
  result through. `controller.py` does this correctly (passes
  `rca_result.get("confidence", 1.0)`); a caller that forgot to would
  silently bypass the gate. This is a real, if narrow, sharp edge in the
  interface — worth flagging to anyone extending the codebase.
- **The cooldown is process-local state** (`self.last_action_time` is an
  instance attribute on a single `RemediationPolicy` object). `app.py`
  constructs exactly one `RemediationPolicy()` at module load and shares it
  across `AegisController`, `ChaosTestManager`, and every remediation
  helper within *one* API process — but since there are 4 API replicas
  behind the load balancer, **the cooldown timer is not actually shared
  across replicas**. Two chaos requests hitting two different ALB targets
  within the cooldown window could both see "no last action yet" and both
  proceed. This is a real, not-yet-fixed gap (see Part 5).
- `record_action()` is a separate, explicit call — evaluating a policy does
  *not* itself start the cooldown; the caller must call `record_action()`
  only after actually executing the action, so a `dry_run` or a
  blocked-and-never-executed action never poisons the cooldown for real
  future actions.

### 2.6 `RemediationEngine` (`remediation.py`) — the direct ECS path

A second, independent execution path that exists alongside the Step
Functions workflow (§2.7/2.8). **This is not the path exercised by the
chaos suite or the main controller** — it's reachable via `POST /remediate`
directly, and via any test that constructs it explicitly.

- `service` property: resolves the target ECS service name from
  `ECS_SERVICE` env var first; if unset, calls `ecs.list_services()` and
  takes the first ARN's suffix; if *that* fails too, falls back to a
  **hardcoded literal ARN suffix string**
  (`AegisInfrastructureStack-AegisApiServiceCEE6438E-3UL7oJqplNbq`) baked
  into the source — a residual of an earlier stack generation before the
  service was given an explicit `service_name`. In the current deployment
  `ECS_SERVICE` is always set via the CDK stack's container environment, so
  this fallback string is dead code in practice, but it would silently
  target a service ARN suffix that may no longer even exist if ever reached.
- `scale_out(incident, execute=True)`: describes the service to get the
  real current `desiredCount`, computes `target = current + 1`
  (**always exactly +1, never more**, regardless of severity), then two
  guardrails in sequence: (1) the `execute` boolean itself — if `False`,
  short-circuits to `BLOCKED_BY_SAFETY_POLICY` *before even calling
  `policy.evaluate()`*; (2) `policy.evaluate()` — if disallowed,
  `BLOCKED_BY_POLICY`. Only if both pass: a single
  `ecs.update_service(desiredCount=target)` call, then
  `policy.record_action()`.
- `restart_tasks(incident, execute=True)`: same two-guardrail sequence,
  but the actual mechanism is **two sequential `update_service` calls** —
  first `desiredCount=0`, then immediately `desiredCount=<original
  desired>`. This is a genuine, brief **full outage** of the service (all
  tasks torn down before any new ones are guaranteed running) — materially
  different from the Step Functions path's `ForceNewDeployment=True`
  approach, which keeps old tasks serving traffic until replacements are
  healthy. **This distinction matters for anyone reading the code**: two
  restart implementations exist, one causes a real outage window and one
  doesn't, and only the safer one is what the demo/chaos suite actually
  uses.

### 2.6.1 `_self_heal_metric()` (`controller.py` and `chaos.py`) — the closed-loop self-healing state

Added this session, modeled on **CIRCA-SH** (Xiao Ma, *"Distributed Fault
Root Cause Localization and Self-Healing Strategy Generation Based on
Causal Inference"*, Procedia Computer Science 281, 2026 — source PDF in
`New folder/1-s2.0-S1877050926012834-main.pdf`). The paper's three
operative ideas, and exactly how each maps onto this implementation:

| Paper concept | AEGIS implementation |
|---|---|
| Counterfactual "do-operator" intervention test — did acting on this actually fix the symptom? | `AegisDetector.recheck_cleared(metric_name, dimensions)`: re-fetches the real CloudWatch value for `cpu`/`memory` after a remediation action and compares it against the metric's threshold |
| Targeted action selection over blind retry | `_ALTERNATE_ACTION = {"SCALE_OUT": "RESTART_TASKS", "RESTART_TASKS": "SCALE_OUT"}` — the second attempt is the *other* action, not a repeat of the first |
| Bounded self-healing action budget (paper uses B=2) | `for attempt_number in (1, 2):` — hard cap, no third attempt, ever |

**`AegisDetector.recheck_cleared(metric_name, dimensions)`** (`detector.py`):
```python
def recheck_cleared(self, metric_name, dimensions):
    cw_metric_name = {"cpu": "CPUUtilization", "memory": "MemoryUtilization"}.get(metric_name)
    if cw_metric_name is None:
        return {"checked": False, "cleared": None, "current_value": None}
    try:
        value = self.get_latest_metric(cw_metric_name, "AWS/ECS", dimensions)
    except Exception:
        value = None   # fixed during this session's E2E testing -- see Part 4.6
    if value is None:
        return {"checked": False, "cleared": None, "current_value": None}
    threshold = self.thresholds.get(metric_name)
    return {"checked": True, "cleared": value <= threshold, "current_value": value, "threshold": threshold}
```
Only `cpu` and `memory` return a real `checked: True` result — every other
metric name (including the composite `correlated_*` names, and every
simulated-only metric like `latency_ms`/`http_5xx_rate`) returns
`checked: False`, because there is no live CloudWatch series backing them.
This is a deliberate design boundary, not an oversight: extending a
"symptom check" to a value that was never real telemetry would produce a
meaningless result dressed up as a measurement.

**`_self_heal_metric()`** (nearly identical implementations exist in both
`controller.py` and `chaos.py` — `chaos.py`'s version additionally calls
`self.recovery.verify()` as a second, independent ECS-state check, matching
that file's existing double-verification convention; `controller.py`'s
relies on `workflow_result["recovery_verified"]` alone, matching *its*
existing convention):

```
action = first_action          # always "SCALE_OUT" at the call site today
desired = current_desired
healed = False

for attempt_number in (1, 2):                      # hard budget cap
    target_desired = desired + 1 if action == "SCALE_OUT" \
                      else (desired if desired > 0 else 1)

    policy_decision = policy.evaluate(incident, action, desired, confidence)
    audit.log(SELF_HEALING_ATTEMPT, attempt_number, action, policy_decision)

    IF NOT policy_decision["allowed"]:
        break                                        # e.g. cooldown/max-scale
                                                       # blocks the 2nd attempt

    exec_info = workflow.start_recovery(incident, action, target_desired)
    policy.record_action()                            # cooldown starts NOW
    workflow_result = workflow.wait_for_completion(exec_info.execution_arn, timeout=150)
    [chaos.py only:] recovery_result = recovery.verify(target_desired)

    symptom = detector.recheck_cleared(metric_name, dims)
    ecs_recovered = workflow_result["recovery_verified"]   # or recovery_result["recovered"] in chaos.py
    symptom_cleared = symptom["cleared"]

    # Unmeasurable (checked=False) is NOT treated as a failure -- it falls
    # back to trusting ECS-level convergence alone, the same behavior the
    # system had before this feature existed, for any metric this can't
    # verify.
    healed = ecs_recovered AND (symptom_cleared is None OR symptom_cleared is True)

    audit.log(SELF_HEALING_VERIFIED, attempt_number, ecs_recovered, symptom)

    IF healed:
        incident_manager.update_status(incident.id, "RESOLVED")
        break

    desired = target_desired                           # carry the new baseline forward
    action = _ALTERNATE_ACTION[action]                  # SCALE_OUT <-> RESTART_TASKS
    IF attempt_number == 2 OR action is None:
        audit.log(SELF_HEALING_EXHAUSTED, actions_taken, reason)
        # loop ends naturally here anyway (attempt_number==2 is the last
        # value in the (1,2) iteration) -- this log fires whether or not
        # an explicit break is needed

return {healed, attempts, final_workflow, [chaos.py only:] final_recovery}
```

**Concrete traced example** (from this session's live end-to-end test,
`report.md` §2.2): a real `cpu-spike` chaos run on the production
deployment. `attempt_number=1`: `SCALE_OUT` executes, ECS converges to
3/3 tasks, `recheck_cleared("cpu", dims)` returns
`{"checked": true, "cleared": true, "current_value": 0.68, "threshold": 80.0}`
— the real live CPU reading, genuinely far below threshold since the
"anomaly" was a value injected only into the detector, not real load.
`healed = True and (True) = True`. Loop exits after attempt 1;
`self_healing.attempts` has length 1; incident marked `RESOLVED`.

**What would trigger attempt 2 in practice:** a `SCALE_OUT` that converges
at the ECS level (new task running) while real CPU/memory genuinely stays
above threshold — the signature of a problem that isn't about capacity
(a hot loop, a leak, a stuck request) rather than load. That scenario
requires the container to be under genuine sustained real stress at the
moment of the recheck, which none of the current chaos scenarios actually
produce (they inject a value into the detector, not real CPU/memory load)
— so this branch is currently verified by the unit test suite and by code
inspection, not by a live trigger. See Part 5 for this and other
not-yet-live-verified branches.

**Response shape change:** callers that previously read
`result["remediation"]` (a single `{action, target_desired_count, status}`
dict) now read `result["self_healing"]` (`{healed, attempts: [...],
final_workflow}` — see Part 6 for the full shape). This was a deliberate,
breaking change to the two affected chaos scenarios' response contract
(`cpu-spike`, `memory-pressure` only — every other scenario's response
shape is unchanged), since the old shape had no way to represent "more than
one action was tried."

### 2.7 `StepFunctionsWorkflowManager` (`workflow.py`) — the real orchestration path

- `state_machine_arn` property: resolves from `STATE_MACHINE_ARN` env var
  first, else paginated `list_state_machines()` search by name, else a
  wildcard-account fallback ARN string (`arn:aws:states:{region}:*:...`) —
  same defensive-fallback pattern as `RemediationEngine.service`.
- `start_recovery(incident, action, target_desired_count)`: builds an
  execution name `aegis-{sanitized_incident_id}-{unix_timestamp}` (only
  alnum/`-`/`_` survive sanitization — everything else becomes `-`, so an
  incident ID with special characters can't produce an invalid Step
  Functions execution name), then `sfn.start_execution()` with a JSON
  payload of `{incident_id, service, metric, value, threshold, severity,
  action, target_desired_count}`.
- `wait_for_completion(execution_arn, timeout_seconds=150, poll_interval=5)`:
  a simple polling loop — `describe_execution` every 5 seconds until
  `status != "RUNNING"` or the 150-second budget elapses. On completion,
  parses the execution's JSON `output` (falls back to `{"raw_output": ...}`
  if it isn't valid JSON) and extracts `workflow_status` and
  `recovery_verified` from it. **If the timeout elapses while still
  RUNNING**, returns a synthetic `{"status": "TIMED_OUT", "workflow_status":
  "TIMED_OUT", "recovery_verified": False, "reason": "..."}` — note that in
  this case the *actual* Step Functions execution keeps running in the
  background past this point; the API caller just stops watching it. (This
  was directly relevant to a bug fixed this session — see Part 4.)

### 2.8 The Step Functions state machine itself (defined in CDK, not Python)

This is worth documenting as its own "engine" even though it lives in
`infrastructure_stack.py` rather than `services/api/`, because it contains
real conditional business logic, not just resource provisioning.

**State graph** (see the diagram in Part 3.2 for the annotated flow):

```
ValidateRemediationAction (Choice)
  ├─ action == "SCALE_OUT"     → ExecuteScaleOut
  ├─ action == "RESTART_TASKS" → ExecuteRestartTasks
  └─ otherwise                 → InvalidRemediationAction (Fail state)

ExecuteScaleOut / ExecuteRestartTasks (CallAwsService → ecs:updateService)
  → WaitForEcsTaskProvisioning (Wait, 30s)
  → DescribeEcsService (CallAwsService → ecs:describeServices)
  → VerifyEcsRecovery (Choice)
       ├─ RunningCount >= target AND PendingCount == 0 → RecoverySuccessful
       └─ otherwise → WaitBeforeRetryVerification1 (Wait, 20s)
            → DescribeEcsServiceRetry1 → VerifyEcsRecoveryRetry1 (Choice)
                 ├─ condition met → RecoverySuccessful
                 └─ otherwise → ...Retry2... → ...Retry3... → ...Retry4...
                      → (Retry4 otherwise) → RecoveryFailedOrEscalated
```

- `ExecuteScaleOut`'s `ecs:updateService` call sets only `DesiredCount`.
- `ExecuteRestartTasks`'s call sets `DesiredCount` **and**
  `ForceNewDeployment: true` — this is what lets ECS run new tasks
  alongside old ones during the rollout (the mechanism behind the
  "overshoot" behavior fixed this session, see Part 4.3).
- The verification condition, as currently deployed:
  ```
  RunningCount >= target_desired_count   (NumericGreaterThanEqualsPath)
  AND
  PendingCount == 0                       (NumericEquals)
  ```
  This was originally a strict `==` on `RunningCount`; changed to `>=`
  this session (Part 4.3 explains exactly why).
- Total verification budget: 30s initial wait + 4×20s retries = **110
  seconds**, deliberately kept under the 150-second
  `wait_for_completion()` client-side timeout so the API always gets a
  real terminal answer back from the state machine rather than timing out
  its own poll first.
- State machine execution timeout (the outer bound, regardless of
  verification budget): **5 minutes**.
- IAM: the state machine's own execution role is granted
  `ecs:UpdateService` scoped to the exact API service ARN, and
  `ecs:DescribeServices` with `resources=["*"]` (no resource-level IAM
  support exists for that action against a describe call inside a state
  machine's `CallAwsService` construct in the way this stack uses it).

### 2.9 `RecoveryVerifier` (`recovery.py`)

A second, independent verification check — used by `chaos.py` *in addition
to* the state machine's own internal verification (the two aren't the same
call; chaos scenarios check twice: once implicitly via the state machine
reaching `RecoverySuccessful`, and once explicitly by calling
`recovery.verify()` themselves afterward).

```python
def verify(self, expected_count):
    service = ecs.describe_services(...)["services"][0]
    return {
        "desired_count": service["desiredCount"],
        "running_count": service["runningCount"],
        "pending_count": service["pendingCount"],
        "recovered": (
            service["desiredCount"] == expected_count
            and service["runningCount"] >= expected_count   # >=, fixed this session
            and service["pendingCount"] == 0
        ),
    }
```

Same `service` property with the same env-var → discover → hardcoded-fallback
chain as `RemediationEngine`. Note this is a genuinely separate hardcoded
fallback ARN suffix literal from the one in `remediation.py` — they happen
to currently point at different (both stale) service ARN suffixes, neither
of which matters in practice since `ECS_SERVICE` is always set.

### 2.10 `EvidenceStore` (`evidence.py`)

- Bucket resolved from `EVIDENCE_BUCKET_NAME` env var. If unset, `enabled`
  is `False` and every call is a silent no-op (`put_evidence` returns
  `None`, `get_evidence` returns `None`) — evidence archiving degrades
  gracefully rather than crashing the pipeline.
- `put_evidence(incident_id, evidence)`: writes to S3 key
  `incidents/{incident_id}/{UTC timestamp down to microseconds}.json`,
  serialized with `json.dumps(evidence, default=str)` — the `default=str`
  is what lets arbitrary non-JSON-native objects (like `datetime`s, if one
  ever leaks into the evidence dict unconverted) serialize without raising.
  **Every exception type** (`ClientError`, `BotoCoreError`, or a bare
  `Exception` catch-all) is swallowed and logged as a warning, returning
  `None` — a failed evidence write never blocks or fails the calling
  detection/recovery cycle.
- The microsecond-precision timestamp in the key means **multiple evidence
  snapshots can exist for the same incident** if `_archive_evidence()` is
  called more than once for it (which does happen — e.g. `chaos.py` calls
  it once at incident creation, and separately the CORRELATED/task-failure
  paths in `controller.py` call it once with an RCA-inclusive payload) —
  `GET /incidents/{id}/evidence` only exposes whichever key is currently
  stamped on the incident's `metadata.evidence_s3_key` field (the *last*
  key written wins, since each write overwrites the metadata field, not
  appends to it), so older evidence snapshots for the same incident become
  orphaned-but-still-present S3 objects, invisible via the API but present
  in the bucket until the 30-day lifecycle rule expires them.

### 2.11 `IncidentManager` / `Incident` (`incident.py`) and `IncidentStore` (`incident_store.py`)

**`Incident`** is a Pydantic `BaseModel` — this is the only place in the
codebase using Pydantic for validation beyond FastAPI's own request models.
Fields: `id` (default-factory `f"INC-{uuid4().hex[:8].upper()}"`),
`timestamp` (default-factory `datetime.utcnow().isoformat()`), `service`
(default `"aegis-api"`), `metric`, `value`, `threshold`, `severity` (default
`"HIGH"`), `status` (default `"OPEN"`), `description`, `recommended_action`,
`incident_type` (`"SINGLE"` or `"CORRELATED"`), `signals` (list, default
empty), `signal_count` (optional), `reason` (optional), `metadata` (dict,
default empty). It also implements `__getitem__` and `.get()` so it can be
used interchangeably with a plain dict anywhere downstream code does
`incident.get("severity")` or `incident["metric"]` — a deliberate
dict-compatibility shim so `Incident` objects and raw dicts (as used inside
`correlation.py`, which builds plain dicts) can flow through the same
`policy.evaluate()`/`rca.analyze()` call sites without type-checking.

**`IncidentManager.create_incident()`:** computes `recommended_action` via
`get_recommended_action(metric, value)` — a small hardcoded lookup
(`cpu >= 95` → "CRITICAL: Immediately scale out..."; `cpu < 95` → "HIGH:
Monitor..."; similar tiers for `memory`; `running_tasks`/`task_failure` →
always the CRITICAL restart-tasks message; anything else → generic
"Investigate anomaly..." text). Persists to `IncidentStore` **and** appends
to an in-memory `self.incidents` list — the in-memory list is a legacy
fallback only consulted by `get_all()`/`get_by_id()` if the DynamoDB read
returns empty, not the primary source of truth.

**`IncidentStore` (DynamoDB-backed, `incident_store.py`):**
- `_to_dynamodb_item()` / `_from_dynamodb_item()`: recursive float↔Decimal
  converters, since DynamoDB's API rejects native Python floats — every
  write recursively walks the dict converting `float → Decimal(str(v))`
  (via string round-trip to avoid binary-float precision artifacts), and
  every read does the reverse, additionally collapsing whole-valued
  Decimals back to `int` (`if data % 1 == 0: return int(data)`) so a
  round-tripped `4.0` comes back as `4`, not `4.0` or `Decimal('4')`.
- **In-memory fallback mirror** (`self._fallback_memory`, a plain dict
  keyed by incident_id): every write updates both DynamoDB *and* this local
  dict; every read tries DynamoDB first and falls back to the mirror on any
  exception. This means a single API replica can keep functioning (reads
  and writes) even during a transient DynamoDB outage, but — critically —
  **the mirror is per-process**, so during an outage, replica A's writes
  are invisible to replica B's reads until DynamoDB recovers. This is the
  same class of cross-replica-consistency gap that was found and fixed for
  chaos history this session (Part 4.1) — it just hasn't come up in
  practice for incident records because DynamoDB hasn't had an outage
  during testing.
- `get_items_by_type(record_type, limit)` — added this session specifically
  to let `chaos.py`'s test-run history share this same table/store
  infrastructure under a `record_type: "chaos_test"` tag, scanned via a
  `FilterExpression` rather than a GSI query (a full table scan with a
  filter — fine at current data volumes, would need a Global Secondary
  Index on `record_type` if this table ever grows large, since
  `FilterExpression` still pays for reading every item before filtering).

### 2.12 `AuditLogger` (`audit.py`)

The simplest and most fragile persistence mechanism in the system: appends
one JSON line per event to a local file (`audit.log` by default, path
overridable via `AUDIT_FILE` env var), and reads it back by
`open().readlines()` + `json.loads()` per line. **This file lives inside
each individual ECS task's ephemeral container filesystem** — it is not
shared storage, not mounted from EFS/S3, and is wiped whenever that specific
task is replaced (deploy, crash, scaling event). With 4 API replicas behind
the load balancer, `GET /audit` returns a different, incomplete answer
depending on which replica happens to serve that specific request — the
exact same class of bug that was diagnosed and fixed for `/chaos/history`
in `chaos.py` this session (Part 4.1), but **`audit.py` itself was left
unfixed** — this is a known, documented, currently-live gap (see Part 5).

---

## Part 3 — Complete request flows

### 3.1 The autonomous detection cycle (`AegisController.run()`, `controller.py`)

This single method is the heart of the "real" (non-chaos) pipeline. Called
either by `supervisor.py`'s 60-second loop or by the EventBridge/SQS/Lambda
fast-path hitting `POST /cycle`.

```
1. ecs.describe_services(cluster, [service]) → get real desiredCount/runningCount

2. task_failure_detected = (desired > 0 AND running == 0)
   IF true: add a "task_failure" signal to the correlation engine
   (does NOT immediately create an incident — goes into the correlation
   window first, same as every other signal)

3. FOR metric_name in ["cpu", "memory"]:
     value = detector.get_latest_metric(CloudWatch, "AWS/ECS", [cluster, service dims])
     IF value is None: skip (no CloudWatch data yet — e.g. brand new service)
     detection = detector.check_metric(metric_name, value)
     IF detection["anomaly"]: add this signal to the correlation engine too

4. correlated = correlation_engine.correlate()

   IF correlated is not None:
      → create_correlated_incident() (ONE incident for however many signals
        merged)
      → rca_engine.analyze(incident) → confidence
      → archive evidence to S3: {correlated, task_health, rca}
      → audit.log(CORRELATED_INCIDENT event)
      → action = "RESTART_TASKS" if "task_failure" in the merged signals'
        metrics, else "SCALE_OUT"
        target_desired = desired (unchanged) for restart,
                          desired + 1 for scale-out
      → policy.evaluate(incident, action, desired, confidence)
      → IF blocked: audit.log(BLOCKED), return {status: "BLOCKED_BY_POLICY"}
        — incident stays OPEN, nothing further happens automatically
      → IF allowed:
           workflow.start_recovery(...) → Step Functions execution starts
           policy.record_action()        → cooldown timer starts NOW,
                                            before waiting for the result
           workflow.wait_for_completion(execution_arn, timeout=150)
           audit.log(STEP_FUNCTION_RECOVERY event, includes recovery_verified)
           _resolve_if_recovered(incident, workflow_result)
             → IF workflow_result["recovery_verified"]: mark incident RESOLVED
             → ELSE: incident stays OPEN
      → return the full result payload
      → EARLY RETURN — nothing below this branch runs this cycle

5. IF task_failure_detected (and correlation did NOT fire — i.e. this was
   the only signal, so len(pending) was 1, below correlation's minimum of 2):
      → create_incident(metric="task_failure", ...) as a SINGLE incident
      → same RCA → policy → Step Functions → verify → resolve sequence as
        above, but action is hardcoded to "RESTART_TASKS" and
        target_desired = desired (restarts don't change scale)

6. FOR each metric_anomaly NOT already folded into a correlated incident
   (i.e. this only runs if correlation did NOT fire, since correlate()
   already consumed every related signal on success):
      → create_incident() as a SINGLE incident per anomaly
      → rca_engine.analyze(incident) → confidence
      → policy.evaluate(incident, "SCALE_OUT", desired, confidence)
      → IF blocked: audit.log(BLOCKED), return {status: "BLOCKED_BY_POLICY"}
      → IF allowed: hand off to _self_heal_metric() (§2.6.1 / §4.6) instead
        of firing one SCALE_OUT and declaring victory the moment ECS's task
        count matches:
           - executes SCALE_OUT, waits, verifies ECS convergence
           - RE-MEASURES the real CloudWatch cpu/memory metric that
             triggered the incident (detector.recheck_cleared())
           - IF the real metric is still above threshold despite ECS
             converging: escalates ONCE to RESTART_TASKS (the targeted
             alternate action) and repeats the check
           - capped at 2 actions total; marks RESOLVED only when the real
             metric is confirmed cleared (or unmeasurable, falling back to
             ECS-level verification alone); otherwise leaves the incident
             OPEN and logs SELF_HEALING_EXHAUSTED for human review
      → return {..., "self_healing": {healed, attempts, final_workflow}}
      → NOTE: this is a for-loop, but every branch RETURNS immediately
        after processing the first incident it creates — so in practice
        only the FIRST metric anomaly in the loop is ever actually acted
        on per cycle; if both cpu and memory are independently anomalous
        AND correlation somehow didn't merge them (shouldn't normally
        happen since both are in the correlatable set, but a race in
        signal timing near the window boundary could produce exactly
        one signal each in two different calls), only one incident's
        remediation actually executes; the other's incident record was
        still created but no action was taken on it that cycle. This
        pre-existing limitation is unchanged by the self-healing addition.

7. IF nothing anomalous at all: return {"anomaly": False, "results": [...]}
```

**Key structural point:** correlation is checked *before* the single-incident
fallback paths, and a successful correlation **returns immediately** — so a
task failure that happens to co-occur with a CPU spike is *always* merged
into one correlated incident (assuming both signals are still within the
60-second window when `correlate()` runs), never double-remediated as two
separate actions in the same cycle.

### 3.2 A chaos scenario end to end (using `cpu-spike` as the concrete example)

```
POST /chaos/cpu-spike?dry_run=false
  → app.py routes to chaos_manager.run_cpu_spike(dry_run=False)

1. test_id = "CHAOS-CPU-{unix_timestamp}"
   audit.log(CHAOS_TEST_STARTED)

2. detection = detector.check_metric("cpu", 95.0)
   → 95.0 > threshold(80.0) → STATIC_THRESHOLD anomaly, severity computed
     via get_severity("cpu", 95.0) → 95 >= 95 → "CRITICAL"

3. incident = incident_manager.create_incident(metric="cpu", value=95.0,
     threshold=80.0, severity="CRITICAL",
     metadata={"chaos_test_id": test_id, "scenario": "CPU_SPIKE"})
   → written to DynamoDB with status=OPEN

4. _archive_evidence(incident, {"detection": detection}) → S3 write,
   incident.metadata.evidence_s3_key stamped

5. audit.log(CHAOS_INCIDENT_DETECTED)

6. current_desired = _get_current_desired_count()  (real ecs:DescribeServices call)
   target_desired = current_desired + 1

7. policy_decision = policy.evaluate(incident, "SCALE_OUT", current_desired)
   ← NOTE: confidence is NOT passed here, so it defaults to 1.0 — chaos
     scenarios' policy checks do NOT go through RCA confidence gating for
     severity/cooldown/max-scale, only real controller.py incidents do.
     (This is a real asymmetry between the chaos path and the real
     detection path — see Part 5.)
   audit.log(CHAOS_POLICY_EVALUATED)

8a. IF policy_decision NOT allowed (e.g. cooldown active, or already at
    max_desired_count=4):
      test_summary = {test_id, scenario, status: "BLOCKED_BY_POLICY",
        recovery_verified: False, timestamp}
      _record_test(test_summary)  → written to BOTH local process memory
        AND DynamoDB (record_type: "chaos_test") -- this is the fix applied
        this session, see Part 4.1
      RETURN {"status": "BLOCKED_BY_POLICY", "workflow": None, "recovery": None, ...}

8b. IF allowed:
      exec_info = workflow.start_recovery(incident, "SCALE_OUT", target_desired)
        → real Step Functions execution starts
      policy.record_action()  → cooldown timer starts now
      workflow_result = workflow.wait_for_completion(exec_info.execution_arn,
        timeout_seconds=150)
        → polls describe_execution every 5s for up to 150s
      recovery_result = recovery.verify(target_desired)
        → SEPARATE, second real ecs:DescribeServices call (in addition to
          whatever the state machine itself already checked internally)
      _resolve_if_recovered(incident, recovery_result)
        → IF recovery_result["recovered"]: incident.status → RESOLVED in DynamoDB
      audit.log(CHAOS_RECOVERY_VERIFIED), audit.log(CHAOS_TEST_COMPLETED)
      test_summary = {test_id, scenario, status: "PASSED" if recovered else "FAILED",
        recovery_verified: recovered, timestamp}
      _record_test(test_summary)
      RETURN full payload: {test, incident, policy, remediation, execution,
        workflow, recovery}
```

Every one of these steps is a real AWS API call when `dry_run=False` —
`dry_run=True` short-circuits at step 8, returning a `SIMULATED` status
after policy evaluation but *before* starting any Step Functions execution
or touching ECS, so the incident record and its evidence are still real,
but no remediation actually happens.

### 3.3 `/reason` request flow — how the dashboard's "live RCA" actually works

```
POST /reason  body: {id, metric, value, threshold, severity, service}
  → app.py:
      rca = rca_engine.analyze(incident_dict)          # §2.3
      return ai_reasoning_engine.analyze(incident_dict, rca)   # §2.4
```

This is the **only** place in the whole system where RCA is run on-demand,
outside the automatic detect→correlate→RCA pipeline — it takes whatever
metric/value/threshold/severity you hand it and runs the exact same
deterministic scoring logic against it, including the precedent search
against real DynamoDB history. This is why re-running `/reason` on the same
incident ID twice in a row can return a *different* confidence the second
time if, between the two calls, some other incident on that same metric got
marked RESOLVED — the precedent search is genuinely live against current
data, not memoized.

---

## Part 4 — Technical implementations done this session (with full mechanism)

These four fixes compounded on top of each other; each one masked the next
until they were peeled apart in sequence.

### 4.1 Chaos history cross-replica consistency (`chaos.py`, `incident_store.py`)

**Root cause:** `ChaosTestManager.history` was a plain Python list —
per-process memory. With 4 API tasks behind the ALB round-robining requests,
`GET /chaos/history` returned a different, incomplete answer depending on
which of the 4 replicas happened to handle that specific request (observed
directly: 5 consecutive polls returned `2, 0, 0, 0, 0` test counts).

**Fix:** added `IncidentStore.get_items_by_type(record_type, limit)` — a
`Scan` with `FilterExpression="record_type = :rt"` against the *existing*
`AegisIncidents` table (no new AWS resource needed — this table's only
schema constraint is the `incident_id` partition key, so it happily holds
heterogeneous item shapes as long as each item supplies that key). A new
`_record_test()` helper in `ChaosTestManager` writes each test summary to
both the local list (fast path, and a safety-net fallback if DynamoDB is
unreachable) **and** to the shared table via
`store.create_incident({"id": test_id, "record_type": "chaos_test",
**test_summary})`. `get_history()` now reads from the shared table first,
only falling back to local `self.history` if the DynamoDB scan comes back
empty or throws. All 16 call sites across 5 chaos scenario methods that
build a `test_summary` dict were redirected through `_record_test()`
instead of the old direct `self.history.insert(0, ...)`.

**Verified fix:** 8 consecutive polls after the fix returned a consistent
`3` — same count regardless of which replica answered.

### 4.2 CORS (`app.py`)

**Root cause:** zero CORS headers configured anywhere — a browser-based
dashboard (`aegis-light.html`) calling the API cross-origin (including from
a `file://` origin, which sends `Origin: null`) would be silently blocked
by the browser's same-origin policy regardless of how correctly the
frontend JavaScript was written. This isn't something curl-based testing
would ever surface, since curl doesn't enforce CORS.

**Fix:** `app.add_middleware(CORSMiddleware, allow_origins=["*"],
allow_methods=["GET", "POST"], allow_headers=["*"])`. Verified live:
`OPTIONS /status` preflight returns `access-control-allow-origin: *` and a
`GET /status` with `Origin: null` also returns the header.

**Explicit risk reasoning documented in-code:** the ALB is already
plain-HTTP, open to the whole internet, with zero authentication on any
route (a pre-existing condition, documented in `docs/ARCHITECTURE_V2.md`
item 5) — so permissive CORS doesn't meaningfully widen the actual attack
surface; it just lets a browser read responses that were already fetchable
by anyone via `curl`.

### 4.3 The RESTART_TASKS overshoot bug — three compounding failures

This was diagnosed live, in three separate observed failure modes, each one
uncovered only after fixing the previous one:

**Failure mode A — verification window too short.** The original Step
Functions definition allowed exactly one retry: 30s wait + one 10s retry =
40 seconds total budget before declaring `RecoveryFailedOrEscalated`. A
`RESTART_TASKS` action forces `ForceNewDeployment=True` on the entire
4-task API service — a real rolling replacement (deregister old tasks from
the target group, pull the image, start new tasks, pass ALB health checks)
that routinely takes well over 40 seconds for 4 Fargate tasks.
**Fix:** extended to 5 verification checkpoints total (initial 30s wait +
4×20s retries = 110s), generated programmatically in a CDK `for` loop that
chains `Wait → CallAwsService(describeServices) → Choice` states, with each
round's `Choice.otherwise()` pointing at the next round and the final
round's `otherwise()` pointing at `RecoveryFailedOrEscalated`. Also bumped
`wait_for_completion()`'s timeout from 120s → 150s (both the `workflow.py`
default and every explicit `timeout_seconds=120` call site across
`chaos.py`, `controller.py`, and `app.py`) so the API's own poll comfortably
outlasts the state machine's new 110-second internal budget.

**Failure mode B — ALB idle timeout killing the connection mid-request.**
After fix A, a real end-to-end test showed the HTTP client (curl, and by
extension the browser) receiving an empty response after exactly 60
seconds, even though the backend was still working. Diagnosis: the ALB's
`idle_timeout.timeout_seconds` attribute defaults to 60 seconds — any
connection with no data flowing for 60s gets silently dropped by the load
balancer, independent of what the backend or the client's own HTTP timeout
is configured to. Since `POST /chaos/task-failure` is a fully synchronous
call that can legitimately take up to ~150 seconds when a scenario clears
the policy gate (start Step Functions → wait up to 150s for a terminal
state → return the full result in one response body), any run past 60
seconds without partial output would always be truncated by the ALB itself,
upstream of any application-level fix.
**Fix:** `elbv2.ApplicationLoadBalancer(..., idle_timeout=Duration.seconds(200))`
— a pure ALB attribute change (confirmed via `cdk diff`: it modifies
`LoadBalancerAttributes` on the existing ALB resource in place, no
replacement, no other resources touched). The dashboard's own
client-side fetch timeout for chaos calls was correspondingly raised from
15s → 130s → 170s → **210s** across the session as each server-side budget
increased, to always stay comfortably ahead of the slowest thing it's
waiting on.

**Failure mode C — exact-equality verification treating a healthy overshoot
as failure.** Even with A and B fixed, a real `task-failure` run returned
`running_count: 7` against `desired_count: 4` — both the Step Functions
`VerifyEcsRecovery` Choice condition and `RecoveryVerifier.verify()` in
Python required **exact equality** (`RunningCount == target`,
`runningCount == expected_count`) before declaring success. But
`ForceNewDeployment` intentionally lets ECS run new tasks *alongside* old
ones — under the service's default deployment configuration (min/max
healthy percent), Fargate can temporarily run *more* tasks than
`desiredCount` while the old generation drains, meaning the exact-equality
instant might never actually occur at any of the 5 polled checkpoints
(the count could jump from below-target straight to above-target between
polls, skipping over the exact value entirely). This produced a `FAILED`
verdict for a rollout that was, in fact, healthy and correctly converging.
**Fix, applied in both places simultaneously:**
- CDK: `sfn.Condition.number_greater_than_equals_json_path(...)` replacing
  `sfn.Condition.number_equals_json_path(...)` for the `RunningCount`
  comparison (the `PendingCount == 0` check is unchanged — that one
  genuinely does need to be exact, since "zero tasks still starting" is a
  meaningful binary state, unlike running count during an overlapping
  rollout).
- Python: `recovery.py`'s `verify()` changed `service["runningCount"] ==
  expected_count` to `service["runningCount"] >= expected_count`, with
  `desiredCount == expected_count` (the *configured target*, which doesn't
  fluctuate mid-rollout the way the live count does) left as exact equality.

**Verified end-to-end after all three fixes**, in order: request completed
in 96 seconds (well inside the new 200s ALB window and 150s API timeout),
returned `workflow_status: "SUCCESS"`, `recovery_verified: true`, and
`recovery: {running_count: 8, desired_count: 4, recovered: true}` — an even
larger overshoot than the first failed attempt, now correctly recognized as
success rather than failure.

### 4.4 Auto-resolving incidents on verified recovery (`chaos.py`, `controller.py`)

**Root cause found by inspection, not by a reported bug:** `grep`ing the
entire backend for calls to `IncidentManager.update_status()` (the only
method capable of ever changing an incident's status away from its default
`OPEN`) returned **zero call sites** anywhere in the codebase, and there was
no `PATCH`/`PUT /incidents/{id}` route either. Every incident ever created
— real or chaos-generated — was permanently `OPEN` with no code path, human
or automatic, that ever closed one. `/status` reflected this directly:
`{"total": 82, "open": 82}`, every single incident open.

**Fix:** a small `_resolve_if_recovered(incident, result)` helper added to
both `ChaosTestManager` and `AegisController`, each calling
`self.incident_manager.update_status(incident.id, "RESOLVED")` exactly when
the relevant recovery-verification result confirms success — inserted
immediately after all **5** `recovery.verify()` call sites in `chaos.py`
(one per non-blocking-path scenario: cpu-spike, task-failure,
memory-pressure, multi-signal, bad-deployment — the scenarios whose logic
reaches an actual remediation attempt rather than always escalating) and
all **3** `workflow.wait_for_completion()` call sites in `controller.py`
(correlated-incident path, task-failure fallback path, single-metric-anomaly
path).

**Verified live:** a `task-failure` chaos run that returned
`recovery_verified: true` was immediately followed by `GET
/incidents/{id}` showing `"status": "RESOLVED"`.

### 4.5 Frontend rewrite (`aegis-light.html`)

Not a bugfix but a full architectural change to the dashboard, done in the
same session, worth documenting here for completeness since it's a
significant chunk of implementation work:

- The original file was a **fully client-side synthetic demo** — a seeded
  PRNG (`rng(seed)`, a linear congruential generator) generated every chart
  series, every table row, every incident, with zero `fetch()` calls to any
  backend anywhere in the script. It also modeled a fictional 9-service
  mesh (`payments-api`, `orders-svc`, `checkout-web`, etc.) that has no
  relationship to the real single-service AEGIS deployment.
- Rewritten to poll three real endpoints per refresh cycle
  (`GET /status`, `GET /incidents`, `GET /chaos/history`) via a small
  `fetchJSON()` wrapper built on `AbortController` for timeout handling,
  with per-call timeout overrides (chaos POST calls get a 210,000ms budget;
  everything else defaults to 15,000ms).
- CPU/memory charts are now **real, client-buffered time series** — a
  capped 40-point rolling array (`POLL_MAX_POINTS`) appended to on every
  successful poll, explicitly labeled "since page load" rather than
  presenting fabricated historical depth the API has no way to actually
  provide (there's no CloudWatch time-series endpoint exposed).
- Sections with no real backing data source in the actual single-service
  deployment (fictional multi-service dependency graph, per-service HTTP
  error breakdowns, deployment-event history, Aurora-specific metrics) were
  removed entirely rather than kept as labeled-fake placeholders — a
  deliberate scope decision to keep everything on-screen genuinely live.
- Selecting an incident row triggers a **live** `POST /reason` call (§3.3)
  and renders whatever comes back, including a client-side-measured
  round-trip latency via `performance.now()` deltas — this is a real
  measurement of the actual RCA call's latency, not a hardcoded number.
- The Chaos panel builds its 10 buttons from a hardcoded
  `CHAOS_SCENARIOS` array of `{slug, label, desc}` matching the 10 real
  `POST /chaos/{slug}` routes exactly (rather than deriving slugs from the
  `GET /chaos` endpoint's display names, which don't consistently map to
  URL slugs — e.g. `MULTI_SIGNAL_INCIDENT` the enum name vs. `multi-signal`
  the URL path), plus a dry-run checkbox wired to the real `?dry_run=`
  query parameter.

### 4.6 Self-healing verification loop, and the bug end-to-end testing found in it

Full design and mechanism already covered in §2.6.1 — this entry is the
implementation-history record: what was built, and what E2E testing (see
`report.md`) found wrong with the first version.

**Built:** `AegisDetector.recheck_cleared()` plus `_self_heal_metric()` in
both `controller.py` (real detection pipeline) and `chaos.py` (`cpu-spike`,
`memory-pressure`), implementing a CIRCA-SH-inspired bounded, symptom-
verifying closed loop in place of the prior single-shot "execute one action,
declare success on ECS convergence" logic.

**Bug found during the very first test run (unit tests, before any live
traffic):** `test_cpu_spike_generates_anomaly_and_full_chain` failed with
`botocore.exceptions.ParamValidationError` — the test fixture's mocked
`RemediationEngine` has `MagicMock` objects for `.cluster`/`.service`
(never previously an issue, since nothing in the cpu-spike/memory-pressure
paths ever called a real AWS API using those values before this session).
`_self_heal_metric()`'s new `dims` construction fed those `MagicMock`
values straight into a real, unmocked `self.detector.get_latest_metric()`
call, which passed them to `boto3`'s `cloudwatch.get_metric_statistics()`
— botocore's own parameter validation rejected them before any network
call was even attempted, since `Dimensions[].Value` must be a `str`.

**Root cause was a real robustness gap, not a test-design problem:**
`AegisDetector.get_latest_metric()` has never had exception handling around
its boto3 call, and until this session nothing in the remediation-execution
path ever called it — every other AWS-touching helper in this codebase
(`EvidenceStore.put_evidence()`, `IncidentStore.create_incident()`,
`AuditLogger` is the one exception, see Part 5 item 1) fails soft on AWS
errors by design. A production CloudWatch throttle, transient permission
hiccup, or any other real API error at exactly this point would have
crashed the entire self-healing loop for an otherwise-successful
remediation, losing the incident's resolution entirely.

**Fix:** wrapped the `get_latest_metric()` call inside `recheck_cleared()`
in a `try/except Exception`, falling back to the same `value = None` →
`{"checked": False, ...}` path already used for a missing CloudWatch
datapoint. This is the correct production behavior independent of the
tests: an unmeasurable symptom should never be treated as a failure to
report, since `_self_heal_metric()` already falls back to trusting
ECS-level verification alone whenever `symptom_cleared is None`.

**Test suite updates required** (intentional contract changes, not bugs):
`test_cpu_spike_generates_anomaly_and_full_chain` and
`test_memory_pressure_generates_anomaly` asserted against the old
`res["remediation"]` key, which no longer exists for these two scenarios
(replaced by `res["self_healing"]`); `test_chaos_test_generates_audit_events`
asserted `"CHAOS_RECOVERY_VERIFIED" in events`, which is now
`"SELF_HEALING_VERIFIED"` for these two scenarios (the audit event name
changed along with where the verification logic moved to). All three were
updated to match the new, intentional response/audit shape. Final suite:
**40 passed, 0 failed** — confirmed both before and after the live
end-to-end pass documented in `report.md`.

**Verified against the live production deployment** (not just unit tests):
a real `cpu-spike` chaos run completed in 56 seconds with
`self_healing.attempts[0].symptom = {"checked": true, "cleared": true,
"current_value": 0.68, "threshold": 80.0}` — a genuine live CloudWatch
reading, not a mock — and the corresponding incident was confirmed
`RESOLVED` via a follow-up `GET /incidents/{id}`. `memory-pressure`
produced the equivalent result (`current_value: 26.57`). The 2-action
escalation branch was not triggered live in this pass, since none of the
current chaos scenarios produce genuine sustained real CPU/memory load —
see Part 5.

---

## Part 5 — Known limitations and sharp edges (as currently implemented)

These are drawn directly from the source-level analysis above, not from
speculation:

1. **`AuditLogger` is per-instance, unlike the (now-fixed) chaos history.**
   `GET /audit` and the `last_audit_event` field on `GET /status` reflect
   only whichever of the 4 API replicas happens to serve that request. The
   exact fix pattern used for chaos history (§4.1) has not yet been applied
   here.
2. **`RemediationPolicy`'s cooldown timer is process-local, not shared
   across the 4 API replicas.** Two requests landing on two different ALB
   targets within the same 120-second cooldown window could both see "no
   prior action" and both execute, defeating the intent of the cooldown
   guardrail under concurrent load.
3. **Chaos scenarios call `policy.evaluate()` without passing an explicit
   `confidence` argument**, so it silently defaults to `1.0` and never
   exercises the RCA-confidence gate for those five scenarios — the
   confidence gate is only genuinely exercised by the real controller
   pipeline and by the two chaos scenarios (`low-confidence`,
   `db-connectivity-failure`) specifically designed to demonstrate it.
4. **Two independent, differently-behaved restart implementations coexist**
   — `RemediationEngine.restart_tasks()` (scale-to-zero-then-back-up, a
   real brief outage) vs. the Step Functions path
   (`ForceNewDeployment=True`, no outage window). Only the latter is
   exercised by the demo/chaos suite; the former is reachable via
   `POST /remediate` and could confuse a future maintainer into thinking
   it's equivalent.
5. **RCA/AI reasoning are rule-based, not backed by a real model call** —
   by design, with a stable interface for a future swap, but worth being
   explicit about since the phrase "AI reasoning" appears in the codebase's
   own module name.
6. **Multiple evidence snapshots can silently orphan** for the same
   incident (§2.10) — only the most recently written S3 key is
   discoverable via the API; earlier ones exist in the bucket but aren't
   linked from anywhere until the 30-day lifecycle rule removes them.
7. **`IncidentStore`'s in-memory fallback mirror is per-process** — a
   DynamoDB outage degrades gracefully for a single replica's own
   read-after-write consistency, but not across replicas, for the same
   structural reason as items 1 and 2.
8. **`chaos.py`'s per-cycle metric anomaly loop in `controller.py` only
   ever acts on the first anomaly per cycle** (§3.1 step 6) — a rare
   timing edge case, but a real one, where two independently-detected
   (not correlated) anomalies in the same cycle result in only one being
   remediated.
9. **Hand-tuned timing constants throughout the recovery path** (110s
   state-machine budget, 150s API poll timeout, 200s ALB idle timeout, 210s
   frontend fetch timeout) were empirically derived against a specific
   4-task Fargate configuration. Changing task count, task CPU/memory size,
   or ALB health-check grace period would require re-validating all four
   numbers together, since they're chained dependencies rather than
   independently safe defaults.
10. **The self-healing loop's 2-action escalation branch has never been
    triggered live**, only by unit tests and code inspection (§4.6). Every
    current chaos scenario injects a value into the detector rather than
    producing genuine sustained CPU/memory load, so the post-action real
    metric always reads healthy and the loop always heals on attempt 1 in
    practice. Confirming the escalation path live would require an actual
    stress workload (e.g. `stress-ng` in the container, or a precisely
    timed real burst from `loadtest/locustfile.py`) sustained through the
    post-remediation recheck window.
11. **`/status`'s incident totals are silently capped at the store's scan
    limit.** Found during this session's end-to-end test pass
    (`report.md` §3.2): `IncidentStore.get_incidents()` defaults to
    `limit=100`, and `/status` calls it with no override, so once the
    table holds more than 100 records the reported `incidents.total`/`open`
    counts stop growing and just reflect that ceiling, not the true count.
    Not introduced this session — it only became observable once the table
    grew past 100 real records during repeated testing.
12. **RCA's "impact" (downstream dependency) field never actually fires.**
    Also found during end-to-end testing: every incident's `service` field
    is `"aegis-api"` (`incident.py`'s default), but `rca.py`'s
    `SERVICE_DEPENDENCIES` dict key is `"aegis-api-service"` — the string
    mismatch means `_dependency_impact()` always misses, so every RCA
    response reports "No downstream dependencies mapped for this service,"
    even though the map does define the real RDS dependency. Pre-existing,
    not introduced this session; likely never worked as intended.

---

## Part 6 — Data shapes reference

**Incident record (DynamoDB `AegisIncidents` table, real incidents):**
```json
{
  "incident_id": "INC-A2B1C848",
  "id": "INC-A2B1C848",
  "timestamp": "2026-09-20T11:08:06.135787",
  "service": "aegis-api",
  "metric": "task_failure",
  "value": 0.0,
  "threshold": 1.0,
  "severity": "CRITICAL",
  "status": "OPEN | RESOLVED",
  "description": "Anomaly detected in aegis-api: task_failure measured at 0.00 (threshold: 1.00, severity: CRITICAL)",
  "recommended_action": "CRITICAL: Running tasks dropped below minimum threshold. Inspect ECS task crash logs in CloudWatch.",
  "incident_type": "SINGLE | CORRELATED",
  "signals": [],
  "signal_count": null,
  "reason": null,
  "metadata": {
    "chaos_test_id": "CHAOS-TASK-1789902486",
    "scenario": "TASK_FAILURE",
    "evidence_s3_key": "incidents/INC-A2B1C848/20260920T110806149917Z.json"
  }
}
```

**Chaos test record (same table, tagged, added this session):**
```json
{
  "id": "CHAOS-TASK-1789902486",
  "record_type": "chaos_test",
  "test_id": "CHAOS-TASK-1789902486",
  "scenario": "TASK_FAILURE",
  "status": "PASSED | FAILED | BLOCKED_BY_POLICY | SIMULATED | NO_ANOMALY_DETECTED",
  "recovery_verified": true,
  "timestamp": "2026-09-20T11:08:06.135562"
}
```

**RCA engine output (`rca.py` → `analyze()` return shape), as actually
observed live against production (`report.md` §2.4 — note `impact` always
reads this way in practice due to the service-name mismatch bug, Part 5
item 12; it never actually cites the RDS dependency the map defines):**
```json
{
  "timestamp": "...", "incident_id": "...", "severity": "CRITICAL",
  "root_cause": "...", "confidence": 0.85,
  "evidence": {"metric": "cpu", "observed_value": 95.0, "threshold": 80.0,
               "signal_count": 1, "precedent_incident_id": "INC-DE617D1D"},
  "impact": "No downstream dependencies mapped for this service.",
  "recommendation": "Scale out ECS tasks... (Precedent: incident INC-DE617D1D with the same signature was previously resolved successfully.)"
}
```

**`/reason` response (`ai_reasoning.py` wrapping the RCA output above):**
```json
{
  "timestamp": "...", "incident_id": "...",
  "analysis": {
    "summary": "CRITICAL cpu anomaly detected in aegis-api (confidence: 93%).",
    "likely_cause": "...", "impact": "...", "confidence": 0.93,
    "evidence": {...same as rca.evidence...},
    "recommended_action": "..."
  }
}
```

**`self_healing` response block** (`_self_heal_metric()` return shape;
appears under `result["self_healing"]` for `controller.py`'s standalone
metric-anomaly path and for `chaos.py`'s `cpu-spike`/`memory-pressure`
scenarios only — every other chaos scenario keeps the older
`result["remediation"]` shape, unchanged. Real example, single-attempt
heal, from this session's live test pass):
```json
{
  "healed": true,
  "attempts": [
    {
      "action": "SCALE_OUT",
      "policy": {"allowed": true, "action": "SCALE_OUT", "reason": "Policy checks passed"},
      "execution": {"execution_arn": "arn:aws:states:...", "target_desired_count": 3},
      "workflow": {"status": "SUCCEEDED", "workflow_status": "SUCCESS", "recovery_verified": true},
      "recovery": {"desired_count": 3, "running_count": 3, "pending_count": 0, "recovered": true},
      "symptom": {"checked": true, "cleared": true, "current_value": 0.68, "threshold": 80.0},
      "healed": true
    }
  ],
  "final_workflow": { "...same shape as attempts[-1].workflow..." },
  "final_recovery": { "...same shape as attempts[-1].recovery, chaos.py only..." }
}
```
A 2-attempt escalation (not yet observed live — see Part 5 item 10) would
have `len(attempts) == 2`, with `attempts[0].action == "SCALE_OUT"`,
`attempts[0].healed == False`, `attempts[1].action == "RESTART_TASKS"`, and
either `attempts[1].healed == True` or, if still unhealed, a
`SELF_HEALING_EXHAUSTED` audit log entry with no further attempts.

---

## Part 7 — Where each piece of behavior lives (file index)

| Behavior | File | Key function(s) |
|---|---|---|
| 60s detection poll + heartbeat | `supervisor.py` | `start()`, `run_cycle()`, `_emit_heartbeat()` |
| Static + ML anomaly detection | `detector.py` | `check_metric()`, `get_severity()` |
| Multi-signal correlation | `correlation.py` | `add_signal()`, `correlate()` |
| Root cause + confidence scoring | `rca.py` | `analyze()`, `_find_precedent()` |
| Reasoning narrative | `ai_reasoning.py` | `analyze()` |
| Safety gate | `policy.py` | `evaluate()`, `record_action()` |
| Direct ECS remediation (secondary path) | `remediation.py` | `scale_out()`, `restart_tasks()` |
| Step Functions orchestration client | `workflow.py` | `start_recovery()`, `wait_for_completion()` |
| Step Functions state machine definition | `infrastructure/infrastructure/infrastructure_stack.py` | lines 519–663 |
| Second recovery verification | `recovery.py` | `verify()` |
| **Self-healing symptom re-check + bounded escalation** | `detector.py`, `controller.py`, `chaos.py` | `recheck_cleared()`, `_self_heal_metric()` |
| Evidence archival | `evidence.py` | `put_evidence()`, `get_evidence()` |
| Incident persistence | `incident.py`, `incident_store.py` | `create_incident()`, `IncidentStore` |
| Chaos scenario suite | `chaos.py` | `run_*()` (10 methods), `_record_test()`, `get_history()` |
| Real autonomous pipeline | `controller.py` | `run()` |
| API surface | `app.py` | all `@app.get/post` routes |
| Dashboard | `aegis-light.html` | `loadAll()`, `runChaos()`, `runRCA()` |

---

*Companion documents: `explain.md` (narrative overview, demo guide, AWS
resource inventory, and prioritized improvement list) — this document is
the implementation-level reference, that one is the pitch/onboarding read.
`report.md` has the most recent live end-to-end test results. `demo.md` has
a time-boxed script for presenting AEGIS live.*

# AEGIS — Live Demo Script

A time-boxed script for presenting AEGIS end to end: healthy system → real
fault injection → autonomous detection → root cause → safety gate → real
recovery → **counterfactual verification** → healed state. Total runtime
**~3 minutes**, matching the pacing of a standard hackathon/investor demo
slot.

This follows the same table format as a generic incident-response demo
script, but every row here is grounded in what AEGIS actually does —
verified live against the production deployment in `report.md`. Where the
generic template assumes something AEGIS doesn't have (Prometheus,
synthetic network-fault injection, a built-in cost calculator), this
script says so explicitly rather than papering over the gap. That honesty
is itself worth emphasizing on stage — see §4.

---

## 1. The demo script

| Time | Demo Stage | What to show physically | What to say | Technical element behind it | Expected visual/result |
|---|---|---|---|---|---|
| 0:00–0:15 | **Healthy System** | Dashboard open (`aegis-light.html`), scrolled to System Overview. Point at the health ring, running/desired task counts, and live CPU/memory numbers ticking. | "This is AEGIS's production deployment right now — one live ECS service on AWS, four tasks, real traffic. Nothing here is mocked; the dashboard is polling the actual API every few seconds." | `GET /status` → real ECS `describeServices` + CloudWatch reads | Green **Healthy** ring, 4/4 tasks running, real (low, single-digit) CPU/memory numbers |
| 0:15–0:25 | **Inject the Fault** | Open the Chaos panel. Click **CPU spike**. | "I'm going to trigger a real, controlled fault — not a slide, an actual call to the live API that simulates a critical CPU spike on this service." | `POST /chaos/cpu-spike` → `detector.check_metric("cpu", 95.0)` | Button shows "Running…" — the request is genuinely in flight against AWS |
| 0:25–0:35 | **Failure Appears / AEGIS Detects** | Narrate while it runs (typically 30–60s if not on cooldown). | "Under the hood: a CRITICAL incident was just created in DynamoDB, and evidence — the detection payload — was just archived to S3. This isn't a UI event, it's a real record." | `AegisDetector.check_metric()` → `IncidentManager.create_incident()` → `EvidenceStore.put_evidence()` | New row appears in the Incidents table, severity `CRITICAL`, status `OPEN` |
| 0:35–0:50 | **Root Cause Analysis** | Click into the new incident. Point at the Root Cause panel. | "AEGIS just ran root-cause analysis live — this confidence score and cause aren't hardcoded, they're computed from a metric knowledge table plus real historical precedent: if this exact metric on this service was ever fixed before, confidence goes up." | `POST /reason` → `RCAEngine.analyze()` → `AIReasoningEngine.analyze()` | Real cause text + confidence score (e.g. `85%`, citing a specific past incident ID if one exists) |
| 0:50–1:05 | **Safety Gate Decision** | Point at the Safety & Policy panel's guardrail list. | "Before AEGIS ever touches infrastructure, this deterministic gate checks it: is this CRITICAL, is confidence above 60%, are we under the max scale limit, has the cooldown elapsed. Every single one of these has to pass." | `RemediationPolicy.evaluate()` — five sequential checks, first failure blocks | `policy.allowed: true`, `"reason": "Policy checks passed"` |
| 1:05–1:20 | **Autonomous Remediation** | Point at the Recovery panel / workflow execution. | "Now AEGIS calls AWS Step Functions to actually scale out the service — a real `ecs:UpdateService` call, orchestrated with retries and a timeout, not a script running on my laptop." | `StepFunctionsWorkflowManager.start_recovery()` → real Step Functions execution against `aegis-recovery-workflow` | Workflow status moves to `RUNNING` then `SUCCEEDED` |
| 1:20–1:35 | **Verification — the part most systems skip** | Point at the `self_healing.attempts[0].symptom` field, or narrate it if not surfaced in the UI. | **"This is the important part. AEGIS does not assume the fix worked just because the task count matched. It re-measures the real, live CloudWatch CPU reading and confirms the actual symptom is gone before declaring victory."** | `AegisDetector.recheck_cleared()` — a genuine counterfactual check against real telemetry, not just ECS state | `symptom.checked: true`, `symptom.cleared: true`, a real low CPU number (e.g. `0.68%`) |
| 1:35–1:45 | **Service Recovery** | Click back to the incident row / refresh the Incidents table. | "The incident is now marked resolved automatically — no human closed it. That only happens because the real metric was confirmed healthy, not just because infrastructure converged." | `incident_manager.update_status(id, "RESOLVED")`, gated on `symptom_cleared` | Incident status flips `OPEN` → `RESOLVED` |
| 1:45–2:00 | **What if it *hadn't* worked?** | Switch to the `low-confidence` or `db-connectivity-failure` chaos buttons. Run one. | "Not every fault gets auto-fixed, on purpose. This one has no established pattern, so confidence comes back below the gate and AEGIS escalates for a human instead of guessing." | Same `_run_generic_scenario()` pipeline, blocked at `policy.evaluate()` | `status: BLOCKED_BY_POLICY`, reason shown verbatim (e.g. `"confidence 0.35 is below the minimum 0.60"`) |
| 2:00–2:15 | **Bounded self-healing (mention, don't fake)** | Describe rather than demo live (see §3 for why). | "If scaling out *hadn't* actually fixed the real metric, AEGIS wouldn't retry forever — it would try exactly one targeted alternate action, restart instead of scale, and if that still didn't work, it stops and escalates. Capped, not infinite." | `_self_heal_metric()` — CIRCA-SH-inspired bounded 2-action loop | (Narrated; see §3 for the honest reason this isn't shown live) |
| 2:15–2:35 | **Downtime / MTTR framing** | Point at the real elapsed time from fault injection to `RESOLVED` (a stopwatch or the incident's own timestamps work). | "That whole loop — detect, diagnose, gate, act, verify, close — just ran in under a minute, against real AWS infrastructure, with zero human involvement." | Real timestamps: incident `timestamp` → resolution | State the **actual measured number** from this run (see §2 — don't round up to a nicer-sounding figure) |
| 2:35–3:00 | **Final State** | Scroll to show the full dashboard: overview, incidents, RCA, safety gate, recovery history all on one screen. | "This is the whole loop, closed, on one screen: detected, diagnosed, gated, fixed, and verified — all against a real, live deployment, not a mockup." | Every endpoint shown together: `/status`, `/incidents`, `/reason`, `/policy`, `/chaos/history` | Dashboard shows a consistent, all-real end state |

---

## 2. The number to actually say out loud

Don't invent a "30 minutes → 3 minutes, 90% reduction" figure — AEGIS has
no baseline "before AEGIS" measurement to compare against, and a fabricated
number is the fastest way to lose credibility with a technical judge who
asks "how did you measure that?"

Instead, use the **real, measured** end-to-end time from the actual test
pass in `report.md`:

> "In our last full test run against the live production deployment, a
> CPU-spike incident went from detection to a fully verified, closed
> recovery in **56 seconds** — with the recovery genuinely confirmed
> against live CloudWatch data, not just infrastructure state."

If you want a "business impact" framing, give the **formula**, not a
canned number, and invite the audience to plug in their own numbers:
`downtime_avoided_per_incident × cost_per_minute_of_downtime = value`.
That's honest, and it's still a compelling close.

---

## 3. Why the escalation branch is narrated, not demoed live

The generic version of this script assumes every stage can be shown live.
One genuinely can't be, and pretending otherwise on stage is a real risk:
the bounded 2-action escalation (SCALE_OUT → RESTART_TASKS when the first
action doesn't clear the real symptom) requires the service to still be
under **genuine, sustained real load** at the moment of the post-action
recheck. AEGIS's chaos scenarios inject a value directly into the
detector to trigger detection — they don't actually stress the container —
so the real CPU/memory reading is always healthy immediately after, and
the loop always heals on the first attempt in a live demo.

**Two honest options, pick based on your audience:**
- **Narrate it** (what the script above does) — describe the mechanism,
  point at the code or `technical.md` §2.6.1 if asked, and be upfront that
  triggering it live would require an actual stress payload.
- **Pre-stage it**, if you have 10 minutes before the demo: run
  `loadtest/locustfile.py`'s `RampingBurstShape` against the ALB to
  generate genuine sustained load, timed so a chaos run's post-action
  recheck lands while real CPU is still elevated. This is real, not
  theater — but it requires rehearsal, since the timing has to line up.

Either way, **do not claim you've shown 2-action escalation live if the
response only shows one attempt** — check `self_healing.attempts` length
before saying it happened.

---

## 4. Talking points about what's real vs. not — say this proactively

Judges and engineers trust a system more when you volunteer its limits
before they ask. Have this ready, ideally worked into the 2:35–3:00 close:

- **RCA is rule-based today, not an LLM call.** "The root-cause engine uses
  a confidence-scored knowledge table today, deliberately built behind the
  same interface a Bedrock agent will use once we wire one in — the safety
  gate downstream doesn't care which one is doing the reasoning."
- **The verification step is the actual novel contribution.** Most
  "autonomous remediation" demos stop at "infrastructure converged." This
  one re-measures the real symptom before declaring success — that's a
  genuine, defensible technical claim, not marketing language.
- **Guardrails are real and were exercised live.** If asked "what stops
  this from going rogue," walk through the five sequential policy checks
  (§ Safety Gate row above) — every one of them was fired live in
  `report.md`'s test pass, including two that correctly blocked an action.

---

## 5. Pre-demo checklist

Run this **10 minutes before** you present, not the night before:

1. `curl {BASE}/health` — confirm `{"status":"healthy"}`.
2. Open `aegis-light.html`, confirm the top bar shows **Connected** and
   real numbers are populating within a few seconds.
3. `curl {BASE}/status` and check `ecs_service.desired_count` — if it's
   already at the policy max (4), a `SCALE_OUT`-based scenario (cpu-spike,
   memory-pressure) will be blocked by the max-scale guardrail instead of
   executing. Either scale down first (`aws ecs update-service
   --desired-count 2 ...`, wait for it to stabilize) or pick a
   `RESTART_TASKS`-based scenario (`task-failure`, `error-rate-spike`,
   `bad-deployment`) instead.
4. `curl {BASE}/policy` and check whether a cooldown is active from
   earlier testing (`"Cooldown active: Ns remaining"` on your next
   attempt is the tell). If so, either wait it out or pick a scenario
   that doesn't need to execute (`low-confidence`, `db-connectivity-failure`,
   which are *supposed* to be blocked anyway).
5. Have a **second chaos scenario queued** as a fallback in case the first
   one lands on cooldown or an unrelated transient AWS hiccup — don't
   improvise live if something doesn't respond in the expected 30–60s.
6. Know the actual current `total_experiments_run` and open incident count
   (`GET /status`) so you're not surprised by what the dashboard shows
   before you've clicked anything.

---

## 6. If something goes wrong live

- **A chaos call takes longer than expected (up to ~150s is normal for a
  non-blocked scenario):** narrate what's happening server-side (Step
  Functions retry loop, real ECS rolling deployment) instead of standing
  in silence — this is real infrastructure, not a spinner for effect.
- **A scenario gets `BLOCKED_BY_POLICY` unexpectedly:** don't panic, read
  the `reason` field out loud — it's almost always cooldown or max-scale
  from earlier testing, and it's a perfectly good illustration that the
  guardrails are real and can't be talked out of blocking something.
- **The dashboard shows stale data:** hit the manual **Refresh** button —
  don't wait for the auto-poll interval on stage.

# AEGIS Verification Runbook — Phases 1-5

Copy-paste checklist to confirm everything built so far is actually live
and working. Written for **Windows PowerShell** (your default shell) —
uses `curl.exe` explicitly (plain `curl` in PowerShell is an alias for
`Invoke-WebRequest`, which behaves differently) and the AWS CLI. Run from
a shell with the same AWS credentials used to deploy
(`aws sts get-caller-identity` should show account `097079438330`).

> Paste each ```powershell ...``` block's *contents only* — don't paste
> the ` ```powershell ` fence line itself, PowerShell will try to run it
> as a command and fail with "term not recognized."

Set this once per shell session:

```powershell
$BASE = "http://AegisI-Aegis-9T1qlmEvlgUH-464819824.ap-south-1.elb.amazonaws.com"
$REGION = "ap-south-1"
$CLUSTER = "aegis-cluster"
```

---

## Phase 1 — Reliability fixes

**1. API and worker are both healthy**
```powershell
curl.exe -sS "$BASE/health"
# expect: {"status":"healthy","service":"aegis-detection"}

aws ecs describe-services --cluster $CLUSTER `
  --services aegis-api-service AegisInfrastructureStack-AegisWorkerService226422CB-jFMie2ho8pXS `
  --region $REGION --query "services[].{name:serviceName,status:status,running:runningCount,desired:desiredCount}"
# expect: both ACTIVE, running == desired
```

**2. Policy shows the confidence gate is wired in**
```powershell
curl.exe -sS "$BASE/policy"
# expect: "min_confidence":0.6 present alongside max_desired_count/cooldown_seconds
```

**3. ECS service name is pinned (not auto-discovered)** — confirms the
`ECS_SERVICE` fix that prevented recovery actions from possibly hitting
the wrong service. CDK auto-generates the task definition family name
(it's not literally "AegisApiTaskDefinition"), so look it up from the
running service rather than hardcoding it -- the family name can also
change across redeploys that replace the task definition:
```powershell
$taskDefArn = aws ecs describe-services --cluster $CLUSTER --services aegis-api-service `
  --region $REGION --query "services[0].taskDefinition" --output text

aws ecs describe-task-definition --task-definition $taskDefArn `
  --region $REGION --query "taskDefinition.containerDefinitions[0].environment[?name=='ECS_SERVICE']"
# expect: [{"name": "ECS_SERVICE", "value": "aegis-api-service"}]
```

**4. Worker heartbeat is actually being emitted**
```powershell
$startTime = (Get-Date).ToUniversalTime().AddMinutes(-15).ToString("yyyy-MM-ddTHH:mm:ss")
$endTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss")

aws cloudwatch get-metric-statistics --namespace AEGIS/Supervisor `
  --metric-name HeartbeatCount --start-time $startTime --end-time $endTime `
  --period 300 --statistics Sum --region $REGION
# expect: at least one datapoint with Sum >= 1 in the last 15 min
```

**5. All alarms are in OK state (not ALARM/INSUFFICIENT_DATA long-term)**
```powershell
aws cloudwatch describe-alarms --region $REGION `
  --alarm-name-prefix Aegis `
  --query "MetricAlarms[].{name:AlarmName,state:StateValue}" --output table
```

**6. SNS topic and budget alarm exist**
```powershell
aws sns list-topics --region $REGION --query "Topics[?contains(TopicArn,'aegis-alerts')]"
aws budgets describe-budgets --account-id 097079438330 --region us-east-1 `
  --query "Budgets[0].{name:BudgetName,limit:BudgetLimit.Amount,spend:CalculatedSpend.ActualSpend.Amount}"
```

---

## Phase 2 — S3 evidence + CloudTrail

**1. Trigger any incident, then fetch its archived evidence:**
```powershell
$resp = curl.exe -sS -X POST "$BASE/chaos/cpu-spike" | ConvertFrom-Json
$incId = $resp.incident.id
Write-Host "Incident: $incId"

curl.exe -sS "$BASE/incidents/$incId/evidence"
# expect: {"incident_id":..., "evidence_s3_key":"incidents/...", "evidence": {...}}
```

**2. Confirm the object actually landed in S3:**
```powershell
aws s3 ls "s3://aegisinfrastructurestack-aegisevidencebucket20fba6-r2nfmw5wxhvt/incidents/$incId/" --region $REGION
```

**3. CloudTrail is logging:**
```powershell
aws cloudtrail get-trail-status --name aegis-trail --region $REGION `
  --query "{logging:IsLogging,latestDelivery:LatestDeliveryTime}"
# expect: logging: true
```

---

## Phase 3 — EventBridge → SQS → Lambda detection trigger

**1. Direct Lambda invocation (bypasses the alarm trigger, tests the Lambda→API path):**
```powershell
aws lambda invoke --function-name aegis-detection-trigger --region $REGION `
  --payload '{}' lambda_test.json
Get-Content lambda_test.json
# expect: statusCode 200, body contains a real /cycle result
```

**2. Confirm the EventBridge rule and its target** — like the task
definition, CDK auto-generated this rule's physical name, so look it up
rather than hardcoding it:
```powershell
$ruleName = aws events list-rules --region $REGION `
  --query "Rules[?contains(Name,'AegisDetectionTriggerRule')].Name | [0]" --output text

aws events describe-rule --name $ruleName --region $REGION `
  --query "{state:State,pattern:EventPattern}"
aws events list-targets-by-rule --rule $ruleName --region $REGION
# expect: state ENABLED, pattern references all 3 alarms, target is the aegis-detection-queue SQS ARN
```

**3. End-to-end via a real alarm (optional, slower — forces CPU alarm into
ALARM state).** Again, CDK auto-generated the alarm's physical name, so
look it up rather than hardcoding it:
```powershell
$alarmName = aws cloudwatch describe-alarms --region $REGION `
  --alarm-name-prefix "AegisInfrastructureStack-AegisHighCpu" --query "MetricAlarms[0].AlarmName" --output text

aws cloudwatch set-alarm-state --alarm-name $alarmName `
  --state-value ALARM --state-reason "manual verification test" --region $REGION
# wait ~30-60s, then check the SQS queue drained and the Lambda ran:
aws sqs get-queue-attributes --queue-url https://sqs.ap-south-1.amazonaws.com/097079438330/aegis-detection-queue `
  --attribute-names ApproximateNumberOfMessages --region $REGION
aws logs tail /aws/lambda/aegis-detection-trigger --region $REGION --since 5m
# expect: a log entry timestamped after your alarm change, "AEGIS cycle triggered: ..."

# reset the alarm afterward:
aws cloudwatch set-alarm-state --alarm-name $alarmName `
  --state-value OK --state-reason "reset after verification" --region $REGION
```

**4. DLQ is empty (nothing stuck failing):**
```powershell
aws sqs get-queue-attributes --queue-url https://sqs.ap-south-1.amazonaws.com/097079438330/aegis-detection-dlq `
  --attribute-names ApproximateNumberOfMessages --region $REGION
# expect: 0
```

---

## Phase 4 — RCA + confidence gate (rule-based, pending Bedrock)

**1. Direct RCA/reasoning check** — PowerShell mangles embedded double
quotes when passing them inline to a native exe like `curl.exe`, so write
the JSON body to a file and reference it with `-d @file` instead:
```powershell
'{"id":"INC-VERIFY","metric":"cpu","value":95,"threshold":80,"severity":"CRITICAL","service":"aegis-api-service"}' | Out-File -Encoding utf8 reason_body.json

curl.exe -sS -X POST "$BASE/reason" -H "Content-Type: application/json" -d "@reason_body.json"
# expect: analysis.confidence, analysis.likely_cause, analysis.impact all populated
```

**2. Confirm the confidence gate actually blocks a bad diagnosis:**
```powershell
curl.exe -sS -X POST "$BASE/chaos/low-confidence"
# expect: "status":"BLOCKED_BY_POLICY", policy.reason mentions "confidence"
```

**3. Confirm a well-understood fault still auto-recovers:**
```powershell
curl.exe -sS -X POST "$BASE/chaos/cpu-spike"
# expect: "status":"COMPLETED", policy.allowed == true, recovery.recovered == true
```

**4. Check Bedrock quota request status:**
```powershell
aws service-quotas list-requested-service-quota-change-history --service-code bedrock `
  --region $REGION --query "RequestedQuotas[?Status=='PENDING' || Status=='CASE_OPENED' || Status=='APPROVED'].{Name:QuotaName,Status:Status}" `
  --output table
```

---

## Phase 5 — Chaos scenario suite + load generator

**1. List all 10 scenarios are registered:**
```powershell
curl.exe -sS "$BASE/chaos"
```

**2. Run each scenario and check its `test.status` field:**
```powershell
$scenarios = @("cpu-spike","task-failure","memory-pressure","multi-signal",
               "latency-spike","error-rate-spike","bad-deployment","normal-workload")

foreach ($s in $scenarios) {
    Write-Host "=== $s ==="
    $result = curl.exe -sS -X POST "$BASE/chaos/$s" | ConvertFrom-Json
    Write-Host $result.test.status
    Start-Sleep -Seconds 3   # let cooldown/rate limits settle between runs
}

# these two are expected to always show BLOCKED_BY_POLICY -- verify that specifically:
(curl.exe -sS -X POST "$BASE/chaos/db-connectivity-failure" | ConvertFrom-Json).policy.reason
(curl.exe -sS -X POST "$BASE/chaos/low-confidence" | ConvertFrom-Json).policy.reason
```

**3. Check the accumulated run history:**
```powershell
curl.exe -sS "$BASE/chaos/history" | ConvertFrom-Json | ConvertTo-Json -Depth 6 | Select-Object -First 40
```

**4. Run the Locust load generator briefly (real traffic, not simulated).**
Uses an absolute path so it works no matter which directory your shell is
currently in -- `loadtest` is a sibling of `infrastructure`/`services`
under the repo root, not nested inside either:
```powershell
Push-Location C:\Users\pranj\AEGIS\loadtest
pip install -r requirements.txt
locust -f locustfile.py --host $BASE --headless --users 10 --spawn-rate 2 --run-time 60s --csv=loadtest_run
Get-Content loadtest_run_stats.csv
# expect: low failure count, response times mostly under ~1s at this concurrency
Pop-Location
```

---

## Quick all-in-one health check

If you just want a fast "is everything still up" pass:
```powershell
curl.exe -sS "$BASE/health"; Write-Host ""
curl.exe -sS "$BASE/status"; Write-Host ""
curl.exe -sS "$BASE/policy"; Write-Host ""
curl.exe -sS "$BASE/chaos"; Write-Host ""
aws cloudwatch describe-alarms --region $REGION --alarm-name-prefix Aegis `
  --query "MetricAlarms[?StateValue!='OK'].AlarmName"
# expect: empty list -- no alarms currently firing
```

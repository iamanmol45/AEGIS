<#
.SYNOPSIS
    Autonomous AEGIS verification run: injects one or more chaos scenarios,
    polls the resulting incident/evidence/alarm state, and prints a single
    readable pass/fail report at the end.

.PARAMETER Scenario
    A single scenario slug (e.g. "cpu-spike") or "all" to run the full
    Phase 5 suite. Default: all.

.PARAMETER Base
    Base URL of the API. Defaults to the known ALB DNS name; override with
    -Base if the stack was redeployed and the ALB address changed.

.EXAMPLE
    .\scripts\Invoke-AegisVerification.ps1
    .\scripts\Invoke-AegisVerification.ps1 -Scenario cpu-spike
#>

param(
    [string]$Scenario = "all",
    [string]$Base = "http://AegisI-Aegis-9T1qlmEvlgUH-464819824.ap-south-1.elb.amazonaws.com",
    [string]$Region = "ap-south-1"
)

$ErrorActionPreference = "Stop"

$AllScenarios = @(
    "cpu-spike", "task-failure", "memory-pressure", "multi-signal",
    "latency-spike", "error-rate-spike", "bad-deployment", "normal-workload",
    "db-connectivity-failure", "low-confidence"
)

# these two are expected to always be blocked by the policy gate, not auto-remediated
$ExpectedBlocked = @("db-connectivity-failure", "low-confidence")

$scenariosToRun = if ($Scenario -eq "all") { $AllScenarios } else { @($Scenario) }

$results = @()

function Invoke-AegisJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$Method = "GET"
    )

    $args = @("-sS", "--retry", "2", "--retry-delay", "2", "--retry-all-errors")
    if ($Method -eq "POST") {
        $args += @("-X", "POST")
    }
    $args += $Uri

    $raw = & curl.exe @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed with exit code ${LASTEXITCODE}: $raw"
    }

    try {
        return $raw | ConvertFrom-Json
    } catch {
        throw "invalid JSON response: $raw"
    }
}

function Invoke-Scenario {
    param([string]$Name)

    $row = [pscustomobject][ordered]@{
        Scenario       = $Name
        InvokeOk       = $false
        TestStatus     = ""
        PolicyAllowed  = ""
        PolicyReason   = ""
        Recovered      = ""
        IncidentId     = ""
        EvidenceOk     = $false
        Expected       = if ($ExpectedBlocked -contains $Name) { "BLOCKED_BY_POLICY" } else { "COMPLETED or NO_ANOMALY" }
        Verdict        = "FAIL"
        Error          = ""
    }

    try {
        $resp = Invoke-AegisJson -Method "POST" -Uri "$Base/chaos/$Name"
        $row.InvokeOk = $true
    } catch {
        $row.Error = "invoke failed: $_"
        return $row
    }

    $row.TestStatus    = if ($resp.test) { $resp.test.status } else { "" }
    $row.PolicyAllowed = if ($resp.policy) { $resp.policy.allowed } else { "" }
    $row.PolicyReason  = if ($resp.policy) { $resp.policy.reason } else { "" }
    $row.Recovered     = if ($resp.recovery) { $resp.recovery.recovered } else { "" }
    $row.IncidentId    = if ($resp.incident) { $resp.incident.id } else { "" }

    if ($row.IncidentId) {
        try {
            $ev = Invoke-AegisJson -Uri "$Base/incidents/$($row.IncidentId)/evidence"
            $row.EvidenceOk = [bool]$ev.evidence_s3_key
        } catch {
            $row.EvidenceOk = $false
        }
    }

    # verdict logic
    if ($ExpectedBlocked -contains $Name) {
        $row.Verdict = if ($row.TestStatus -eq "BLOCKED_BY_POLICY" -and $row.PolicyReason -match "confidence|database|not.*whitelisted|INVESTIGATE") { "PASS" } else { "FAIL" }
    } elseif ($Name -eq "normal-workload") {
        $row.Verdict = if ($row.TestStatus -eq "NO_ANOMALY" -or $row.TestStatus -eq "COMPLETED") { "PASS" } else { "FAIL" }
    } else {
        $row.Verdict = if ($row.TestStatus -eq "COMPLETED" -and $row.PolicyAllowed -eq $true) { "PASS" } else { "FAIL" }
    }

    return $row
}

Write-Host "=== AEGIS Autonomous Verification Run ===" -ForegroundColor Cyan
Write-Host "Base: $Base"
Write-Host "Scenarios: $($scenariosToRun -join ', ')"
Write-Host ""

foreach ($s in $scenariosToRun) {
    Write-Host "Running $s..." -NoNewline
    $r = Invoke-Scenario -Name $s
    $results += $r
    $color = if ($r.Verdict -eq "PASS") { "Green" } else { "Red" }
    Write-Host " $($r.Verdict)" -ForegroundColor $color
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "=== System-level checks ===" -ForegroundColor Cyan

$health = try { Invoke-AegisJson -Uri "$Base/health" } catch { $null }
$healthOk = $health.status -eq "healthy"

$dlqAttrs = try {
    aws sqs get-queue-attributes `
        --queue-url "https://sqs.$Region.amazonaws.com/097079438330/aegis-detection-dlq" `
        --attribute-names ApproximateNumberOfMessages --region $Region | ConvertFrom-Json
} catch { $null }
$dlqCount = if ($dlqAttrs) { [int]$dlqAttrs.Attributes.ApproximateNumberOfMessages } else { -1 }

$firingAlarms = try {
    aws cloudwatch describe-alarms --region $Region --alarm-name-prefix Aegis `
        --query "MetricAlarms[?StateValue!='OK'].AlarmName" | ConvertFrom-Json
} catch { @() }

Write-Host ("Health endpoint:  {0}" -f $(if ($healthOk) { "OK" } else { "FAIL" }))
Write-Host ("DLQ depth:        {0}" -f $(if ($dlqCount -eq 0) { "0 (clean)" } else { "$dlqCount -- INVESTIGATE" }))
Write-Host ("Firing alarms:    {0}" -f $(if ($firingAlarms.Count -eq 0) { "none" } else { ($firingAlarms -join ", ") }))

Write-Host ""
Write-Host "=== Report ===" -ForegroundColor Cyan
$results | Select-Object Scenario, Verdict, TestStatus, PolicyAllowed, Recovered, EvidenceOk, Expected, Error |
    Format-Table -AutoSize

$passCount = ($results | Where-Object { $_.Verdict -eq "PASS" }).Count
$failCount = ($results | Where-Object { $_.Verdict -eq "FAIL" }).Count

Write-Host ""
if ($failCount -eq 0 -and $healthOk -and $dlqCount -eq 0 -and $firingAlarms.Count -eq 0) {
    Write-Host "OVERALL: PASS  ($passCount/$($results.Count) scenarios, system healthy)" -ForegroundColor Green
} else {
    Write-Host "OVERALL: FAIL  ($passCount/$($results.Count) scenarios passed)" -ForegroundColor Red
}

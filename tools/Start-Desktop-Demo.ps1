[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [ValidateSet('Notepad', 'Visual Studio Code', 'Google Chrome', 'Microsoft Edge', 'File Explorer', 'Windows Terminal', 'unknown')]
    [string]$ExpectedApp = 'Notepad',
    [string]$EvidenceDirectory
)

$ErrorActionPreference = 'Stop'
$demoRepo = Split-Path $PSScriptRoot -Parent
$demoConfig = (Resolve-Path -LiteralPath $Config).Path
if (-not $EvidenceDirectory) {
    $EvidenceDirectory = Join-Path $demoRepo '.agent_workspace\desktop-demo'
}
$demoHead = (git -C $demoRepo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not establish the demo commit.' }

Write-Host "Expected target: $ExpectedApp. Select its window during the next 8 seconds."
Write-Host 'Keep that window foreground until the tool result and model reply appear.'
Start-Sleep -Seconds 8
Push-Location (Join-Path $demoRepo 'src')
try {
    $demoRaw = & python -m agentcore.cli --config $demoConfig chat --enable-desktop --message 'Jarvis, what application am I using right now?' --json 2>$null
    $demoExit = $LASTEXITCODE
} finally {
    Pop-Location
}
try { $demoResult = ($demoRaw -join "`n") | ConvertFrom-Json } catch {
    Write-Host '[Demo] conversation_process_failed: no valid result was received. No pass established.'
    return
}
$demoSafeCategories = @('model_timeout','model_authentication','model_rate_limit','model_connection','model_configuration','model_response_invalid','model_response_budget_exhausted','model_http_error','model_failure','desktop_not_enabled','tool_disabled','desktop_service_unavailable','permission_denied','inaccessible','desktop_unavailable','unsupported_os','service_unavailable','native_failure','operator_cancelled')
$demoCategory = if ($demoResult.error_category -in $demoSafeCategories) { $demoResult.error_category } else { $null }
if ($demoCategory) { Write-Host ('[Demo] ' + $demoCategory) }
$demoCalls = @($demoResult.desktop_tool_calls | Where-Object { $null -ne $_ })
$demoObserved = @($demoResult.desktop_observations | Where-Object { $null -ne $_ })
Write-Host 'MODEL TOOL REQUEST / EXECUTOR METADATA:'
$demoCalls | ConvertTo-Json -Depth 4 | Write-Host
Write-Host 'NATIVE TOOL EVIDENCE:'
$demoObserved | ConvertTo-Json -Depth 4 | Write-Host
Write-Host 'MODEL REPLY:'
Write-Host $demoResult.reply

$demoCallVerified = $demoCalls.Count -eq 1 -and $demoCalls[0].name -eq 'get_foreground_app' -and
    $demoCalls[0].arguments -is [pscustomobject] -and @($demoCalls[0].arguments.PSObject.Properties).Count -eq 0 -and
    $demoCalls[0].status -eq 'ok' -and $demoCalls[0].executor_returned -eq $true -and $demoCalls[0].executed -eq $true
$demoMatches = $demoObserved.Count -eq 1 -and (
    ($ExpectedApp -eq 'unknown' -and $demoObserved[0].status -eq 'unknown' -and $null -eq $demoObserved[0].app) -or
    ($demoObserved[0].status -eq 'observed' -and $demoObserved[0].app -eq $ExpectedApp)
)
$demoNativeAnswer = 'no'
$demoReplyAnswer = 'no'
if ($demoObserved.Count -eq 1 -and $demoObserved[0].status -in @('observed', 'unknown')) {
    $demoNativeAnswer = Read-Host 'Did you keep the expected app foreground, and does NATIVE TOOL EVIDENCE correctly describe it? Type yes or no'
}
if ($demoCallVerified -and $demoMatches -and $demoExit -eq 0 -and $demoResult.ok -and
        -not [string]::IsNullOrWhiteSpace($demoResult.reply)) {
    $demoReplyAnswer = Read-Host 'Does the MODEL REPLY accurately report that native result, without inventing an app? Type yes or no'
}
$demoNativeConfirmed = $demoNativeAnswer.Trim().ToLowerInvariant() -eq 'yes'
$demoReplyConfirmed = $demoReplyAnswer.Trim().ToLowerInvariant() -eq 'yes'
$demoPass = $demoCallVerified -and $demoMatches -and $demoExit -eq 0 -and $demoResult.ok -and
    $demoNativeConfirmed -and $demoReplyConfirmed -and -not [string]::IsNullOrWhiteSpace($demoResult.reply)
$demoEvidence = [ordered]@{
    commit = $demoHead
    mode = 'Chat'
    windows_build = [Environment]::OSVersion.Version.Build
    recorded_at_utc = [DateTime]::UtcNow.ToString('o')
    expected_app = $ExpectedApp
    conversation_ok = [bool]($demoExit -eq 0 -and $demoResult.ok)
    error_category = $demoCategory
    desktop_tool_calls = $demoCalls
    desktop_observations = $demoObserved
    model_reply = $demoResult.reply
    structured_call_verified = [bool]$demoCallVerified
    tool_matched_expected = [bool]$demoMatches
    operator_confirmed_native = $demoNativeConfirmed
    operator_confirmed_reply = $demoReplyConfirmed
    live_conversation_pass = [bool]$demoPass
}
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$demoRecord = Join-Path $EvidenceDirectory ('conversation-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff') + '.json')
$demoEvidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $demoRecord -Encoding utf8
Write-Host "Recorded: $demoRecord"
if ($demoPass) {
    Write-Host 'LIVE CONVERSATION PASS for this single operator-confirmed case. Complete other cases separately.'
} else {
    Write-Host 'LIVE CONVERSATION PASS not established. Review the separate evidence and confirmations.'
}

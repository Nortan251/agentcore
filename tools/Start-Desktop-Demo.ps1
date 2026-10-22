[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [ValidateSet('Notepad', 'Visual Studio Code', 'Google Chrome', 'Microsoft Edge', 'File Explorer', 'Windows Terminal', 'unknown')]
    [string]$ExpectedApp = 'Notepad',
    [string]$EvidenceDirectory,
    [switch]$NotepadDenial
)

$ErrorActionPreference = 'Stop'
$demoRepo = Split-Path $PSScriptRoot -Parent
$demoConfig = (Resolve-Path -LiteralPath $Config).Path
if (-not $EvidenceDirectory) {
    $EvidenceDirectory = Join-Path $demoRepo '.agent_workspace\desktop-demo'
}
$demoHead = (git -C $demoRepo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not establish the demo commit.' }

if ($NotepadDenial) {
    Write-Host "Denial test only. At the genuine [Desktop approval] prompt, type no and press Enter."
    Write-Host "Do not type the approval phrase. Existing Notepad windows are not evidence of a new launch."
    New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
    $demoRecord = Join-Path (Resolve-Path -LiteralPath $EvidenceDirectory).Path ('notepad-denial-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff') + '.json')
    Push-Location (Join-Path $demoRepo 'src')
    try {
        # Inherit the real console stdin; --json would disable the approval gate.
        & python -m agentcore.cli --config $demoConfig chat --enable-desktop --message 'Jarvis, open Notepad.' --desktop-evidence $demoRecord 2>$null
        $demoExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($demoExit -eq 2) {
        Write-Host 'Interactive turn refused; no new denial verdict. Retain any existing receipt. Do not retry automatically.'
        return
    }
    try { $demoEvidence = Get-Content -LiteralPath $demoRecord -Raw | ConvertFrom-Json } catch {
        Write-Host 'No complete structured receipt. The action outcome is unconfirmed; do not retry automatically.'
        return
    }
    Write-Host 'STRUCTURED MODEL REQUEST, APPROVAL, EXECUTOR AND NATIVE EVIDENCE:'
    $demoEvidence | ConvertTo-Json -Depth 6 | Write-Host
    $demoCalls = @($demoEvidence.desktop_tool_calls | Where-Object { $null -ne $_ })
    $demoLaunches = @($demoEvidence.launch_results | Where-Object { $null -ne $_ })
    $demoDenied = $demoExit -eq 0 -and $demoEvidence.conversation_ok -eq $true -and
        $demoCalls.Count -eq 1 -and $demoCalls[0].name -eq 'open_notepad' -and
        $demoCalls[0].arguments -is [pscustomobject] -and @($demoCalls[0].arguments.PSObject.Properties).Count -eq 0 -and
        $demoCalls[0].status -eq 'denied' -and $demoCalls[0].executor_returned -eq $true -and $demoCalls[0].executed -eq $false -and
        $demoEvidence.approval.request_count -eq 1 -and $demoEvidence.approval.decision -eq 'denied' -and
        $demoEvidence.approval.explicit_denial -eq $true -and $demoLaunches.Count -eq 1 -and
        $demoLaunches[0].arguments_validated -eq $true -and $demoLaunches[0].executor_category -eq 'permission_denied' -and
        $demoLaunches[0].launch_assessment -eq 'not_launched' -and $demoLaunches[0].native_action_invoked -eq $false -and
        $null -eq $demoLaunches[0].native_outcome -and -not [string]::IsNullOrWhiteSpace($demoEvidence.model_reply)
    $demoVisual = 'no'
    $demoReply = 'no'
    if ($demoDenied) {
        $demoVisual = Read-Host 'Did you see the real approval prompt, type no, and observe NO NEW Notepad window from this request? Type yes or no'
        $demoReply = Read-Host 'Does the final MODEL REPLY accurately report denial without claiming Notepad opened? Type yes or no'
    }
    $demoVisualConfirmed = $demoVisual.Trim().ToLowerInvariant() -eq 'yes'
    $demoReplyConfirmed = $demoReply.Trim().ToLowerInvariant() -eq 'yes'
    $demoEvidence | Add-Member -NotePropertyMembers @{
        commit = $demoHead
        mode = 'NotepadDenial'
        windows_build = [Environment]::OSVersion.Version.Build
        recorded_at_utc = [DateTime]::UtcNow.ToString('o')
        operator_confirmed_no_new_window = $demoVisualConfirmed
        operator_confirmed_reply = $demoReplyConfirmed
        denial_test_pass = [bool]($demoDenied -and $demoVisualConfirmed -and $demoReplyConfirmed)
    } -Force
    $demoEvidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $demoRecord -Encoding utf8
    Write-Host "Recorded: $demoRecord"
    if ($demoEvidence.denial_test_pass) { Write-Host 'LIVE NOTEPAD DENIAL PASS for this single operator-confirmed case.' }
    else { Write-Host 'DENIAL TEST NOT CONFIRMED. Do not retry automatically.' }
    Write-Host 'Stop here. Approval testing requires separate review of this receipt.'
    return
}

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

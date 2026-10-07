<#
.SYNOPSIS
  Canary Auto-Resume 작업을 Windows 작업 스케줄러에 등록/해제한다 (Phase 18E.1).

.DESCRIPTION
  run_targeting_canary_resume.bat 을 8시간 간격으로 실행하는 작업을 만든다.

  이 작업은 **실제 쓰기를 보장하지 않는다.** 깨어날 뿐이고, 실제로 누를지는
  Runner 안의 상태/한도/게이트가 결정한다. Claude 한도가 비었거나 자격 후보가
  없으면 아무 것도 하지 않고 조용히 끝난다.

  관리자 권한을 요구하지 않는다(현재 사용자 계정 작업으로 등록).
  기본은 Dry Run이다. 실제 등록하려면 -Install 을 붙인다.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File targeting_agent\scripts\install_canary_scheduler.ps1
  powershell -ExecutionPolicy Bypass -File targeting_agent\scripts\install_canary_scheduler.ps1 -Install
  powershell -ExecutionPolicy Bypass -File targeting_agent\scripts\install_canary_scheduler.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [int]$IntervalHours = 8,
    [string]$TaskName = "DailyReels_Targeting_CanaryResume",
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# 프로젝트 루트: 이 스크립트(targeting_agent\scripts\)의 두 단계 위.
# $PSScriptRoot 가 비는 호출 방식이 있어 $MyInvocation 으로 한 번 더 받는다.
$scriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if ([string]::IsNullOrEmpty($scriptDir)) {
    Write-Error "스크립트 위치를 알 수 없습니다. 프로젝트 폴더에서 실행하세요."
    exit 1
}
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$batPath     = Join-Path $projectRoot "run_targeting_canary_resume.bat"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[완료] 작업을 제거했습니다: $TaskName"
    } else {
        Write-Host "[알림] 등록된 작업이 없습니다: $TaskName"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $batPath)) {
    Write-Error "실행 파일을 찾을 수 없습니다: $batPath"
    exit 1
}

if ($IntervalHours -lt 8) {
    # Canary의 최소 Run 간격은 8시간이다. 더 자주 깨워 봐야 Runner가 거절한다.
    Write-Error "간격은 8시간 이상이어야 합니다(Canary 최소 간격): $IntervalHours"
    exit 1
}

# 경로에 공백/한글이 있어도 안전하도록 따옴표로 감싼다.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
                                  -Argument "/c `"`"$batPath`"`"" `
                                  -WorkingDirectory $projectRoot

# 지금부터 8시간 간격으로 반복. 기간 제한을 두지 않아도 Runner가
# COMPLETED / STOPPED_* 상태에서 항상 no-op 하므로 추가 쓰기는 없다.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) `
                                    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

# MultipleInstances IgnoreNew: 겹쳐 떠도 새 인스턴스를 만들지 않는다.
# (코드 쪽에도 lock이 있지만 한 겹 더 둔다)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                                         -DontStopIfGoingOnBatteries `
                                         -MultipleInstances IgnoreNew `
                                         -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Write-Host ""
Write-Host "작업 이름 : $TaskName"
Write-Host "실행 간격 : $IntervalHours 시간마다"
Write-Host "실행 파일 : $batPath"
Write-Host "작업 폴더 : $projectRoot"
Write-Host ""
Write-Host "이 작업은 깨우기만 합니다. 실제 LIKE 여부는 Runner의 상태/한도가 정합니다."
Write-Host "  - Claude 한도 소진  -> WAITING_AI_BUDGET (아무 것도 하지 않음)"
Write-Host "  - 자격 후보 없음    -> WAITING_ELIGIBLE  (임계값을 낮추지 않음)"
Write-Host "  - Canary 종료 상태  -> no-op"
Write-Host ""

if (-not $Install) {
    Write-Host "[Dry Run] 아직 등록하지 않았습니다."
    Write-Host "실제로 등록하려면 -Install 을 붙여 다시 실행하세요."
    exit 0
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[알림] 기존 작업을 교체합니다."
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Settings $settings `
                       -Description "DailyReels Targeting Agent — Limited Live Canary 이어가기(LIKE 전용)" | Out-Null
Write-Host "[완료] $IntervalHours 시간 간격으로 등록했습니다."
Write-Host "제거: powershell -ExecutionPolicy Bypass -File targeting_agent\scripts\install_canary_scheduler.ps1 -Uninstall"

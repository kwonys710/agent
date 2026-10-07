<#
.SYNOPSIS
  DailyReels Targeting Agent를 Windows 작업 스케줄러에 등록/해제한다.

.DESCRIPTION
  매일 지정한 시각에 run_targeting_scheduled.bat 을 실행하는 작업을 만든다.
  기본은 Dry Run이다. 실제 등록하려면 -Install 을 붙인다.
  관리자 권한을 요구하지 않는다(현재 사용자 계정 작업으로 등록).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Time "19:00"
  powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Time "19:00" -Install
  powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Time = "19:00",
    [string]$TaskName = "DailyReels_Targeting_Agent",
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# 프로젝트 루트: 이 스크립트(targeting_agent\scripts\)의 두 단계 위
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$batPath    = Join-Path $projectRoot "run_targeting_scheduled.bat"

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

if ($Time -notmatch '^([01]?\d|2[0-3]):[0-5]\d$') {
    Write-Error "시간 형식이 잘못되었습니다(HH:mm): $Time"
    exit 1
}

# 경로에 공백/한글이 있어도 안전하도록 따옴표로 감싼다.
$action    = New-ScheduledTaskAction -Execute "cmd.exe" `
                                     -Argument "/c `"`"$batPath`"`"" `
                                     -WorkingDirectory $projectRoot
$trigger   = New-ScheduledTaskTrigger -Daily -At $Time
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                                          -DontStopIfGoingOnBatteries `
                                          -MultipleInstances IgnoreNew

Write-Host ""
Write-Host "작업 이름 : $TaskName"
Write-Host "실행 시각 : 매일 $Time"
Write-Host "실행 파일 : $batPath"
Write-Host "작업 폴더 : $projectRoot"
Write-Host ""

if (-not $Install) {
    Write-Host "[Dry Run] 아직 등록하지 않았습니다."
    Write-Host "실제로 등록하려면 위 내용을 확인한 뒤 -Install 을 붙여 다시 실행하세요:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Time `"$Time`" -Install"
    exit 0
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[알림] 기존 작업을 교체합니다."
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Settings $settings -Description "DailyReels Targeting Agent 일일 실행" | Out-Null
Write-Host "[완료] 매일 $Time 에 실행되도록 등록했습니다."
Write-Host "제거: powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1 -Uninstall"

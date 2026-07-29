# Makes Jarvis behave like an installed app: Desktop + Start Menu shortcuts to
# launch it, and a Startup shortcut so it is already running (and the global
# double-Esc already listening) when you log in.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$main = Join-Path $root 'main.py'
$icon = Join-Path $root 'assets\jarvis.ico'

$desktop = [Environment]::GetFolderPath('Desktop')
$startup = [Environment]::GetFolderPath('Startup')
$programs = [Environment]::GetFolderPath('Programs')

$targets = @(
    (Join-Path $desktop 'Jarvis.lnk'),
    (Join-Path $programs 'Jarvis.lnk'),
    (Join-Path $startup 'Jarvis.lnk')
)

$taskName = 'Jarvis'

if ($Uninstall) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "removed $t" }
        else { Write-Host "not present: $t" }
    }
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        Write-Host "removed scheduled task '$taskName'"
    } catch { Write-Host "no scheduled task to remove" }
    Write-Host "`nJarvis shortcuts and autostart removed. The project folder is untouched."
    return
}

# pythonw.exe runs without a console window - that is what makes it feel native
$pyw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to python.exe" }
if (-not (Test-Path $main)) { throw "main.py not found at $main" }
if (-not (Test-Path $icon)) {
    Write-Host "icon missing - generating it"
    & (Get-Command python).Source (Join-Path $root 'scripts\make_icon.py') | Out-Null
}

function New-Shortcut {
    param($Path, $Arguments, $Description)
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Path)
    $lnk.TargetPath = $pyw
    $lnk.Arguments = '"' + $main + '"' + $(if ($Arguments) { ' ' + $Arguments } else { '' })
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$icon,0"
    $lnk.Description = $Description
    $lnk.WindowStyle = 7          # minimised - no flash on launch
    $lnk.Save()
    Write-Host "created $Path"
}

New-Shortcut -Path (Join-Path $desktop 'Jarvis.lnk')  -Arguments '' -Description 'Jarvis - personal desktop assistant'
New-Shortcut -Path (Join-Path $programs 'Jarvis.lnk') -Arguments '' -Description 'Jarvis - personal desktop assistant'

# Autostart via Task Scheduler, NOT the Startup folder.
#
# Explorer starts Startup-folder items last and staggers them behind the
# registry Run keys, so with a dozen other startup programs Jarvis was taking
# about a minute to appear. An "at log on" task fires straight away, and
# because it isn't queued behind OneDrive/Discord/etc it shows the window while
# the rest of the login is still settling.
if (Test-Path (Join-Path $startup 'Jarvis.lnk')) {
    Remove-Item (Join-Path $startup 'Jarvis.lnk') -Force
    Write-Host "removed the old Startup-folder shortcut (superseded by the task)"
}

$action = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + $main + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Starts Jarvis at log on' -Force | Out-Null
Write-Host "registered scheduled task '$taskName' (at log on, no delay)"

Write-Host @"

Done.
  Desktop / Start Menu -> opens Jarvis
  Scheduled task       -> starts Jarvis at log on, without the Startup folder's delay

Pin the Start Menu entry to the taskbar if you want it there too.
Remove everything again with:  scripts\install.ps1 -Uninstall
"@

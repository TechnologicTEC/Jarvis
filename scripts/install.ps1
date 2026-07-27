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

if ($Uninstall) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "removed $t" }
        else { Write-Host "not present: $t" }
    }
    Write-Host "`nJarvis shortcuts removed. The project folder is untouched."
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

# Desktop + Start Menu: open the window. Startup: silent in the tray.
New-Shortcut -Path (Join-Path $desktop 'Jarvis.lnk')  -Arguments '' -Description 'Jarvis - personal desktop assistant'
New-Shortcut -Path (Join-Path $programs 'Jarvis.lnk') -Arguments '' -Description 'Jarvis - personal desktop assistant'
New-Shortcut -Path (Join-Path $startup 'Jarvis.lnk')  -Arguments '--tray' -Description 'Jarvis (background)'

Write-Host @"

Done.
  Desktop / Start Menu -> opens Jarvis
  Startup              -> runs hidden in the tray at login, so double-Esc always works

Pin the Start Menu entry to the taskbar if you want it there too.
Remove everything again with:  scripts\install.ps1 -Uninstall
"@

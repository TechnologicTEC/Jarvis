# Creates a Startup shortcut that launches Jarvis silently in the tray at login.
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'
if (-not (Test-Path $py)) { $py = (Get-Command pythonw.exe).Source }
$main = (Resolve-Path (Join-Path $PSScriptRoot '..\main.py')).Path
$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $startup 'Jarvis.lnk'))
$lnk.TargetPath = $py
$lnk.Arguments = '"' + $main + '" --tray'
$lnk.WorkingDirectory = (Split-Path $main)
$lnk.Save()
Write-Host "Installed: $((Join-Path $startup 'Jarvis.lnk')) -> pythonw main.py --tray"

$WshShell = New-Object -ComObject WScript.Shell
$StartupFolder = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder 'WorklogApp.lnk'

# Remove existing shortcut if present
if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
}

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = 'C:\Users\Administrator\worklog-app\start_server.bat'
$Shortcut.WorkingDirectory = 'C:\Users\Administrator\worklog-app'
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'Worklog App Flask Server'
$Shortcut.Save()

Write-Host "Startup shortcut created: $ShortcutPath"
Write-Host "Target: $($Shortcut.TargetPath)"

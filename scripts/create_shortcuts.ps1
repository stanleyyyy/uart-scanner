<#
    create_shortcuts.ps1
    Creates "UART Scanner" shortcuts (Desktop + Start Menu) that launch
    uart_scan.bat. After running, you can right-click the Start Menu entry and
    choose "Pin to Start" / "Pin to taskbar".

    Run from anywhere; it resolves paths relative to the repo root.
#>

$ErrorActionPreference = 'Stop'

# Repo root = parent of this script's folder.
$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $root 'uart_scan.bat'

if (-not (Test-Path $bat)) {
    throw "uart_scan.bat not found next to the repo root ($bat)"
}

$ws = New-Object -ComObject WScript.Shell
$targets = @(
    [System.Environment]::GetFolderPath('Desktop'),
    (Join-Path $env:AppData 'Microsoft\Windows\Start Menu\Programs')
)

foreach ($dir in $targets) {
    $lnkPath = Join-Path $dir 'UART Scanner.lnk'
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath       = $bat
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = "$env:SystemRoot\System32\cmd.exe,0"
    $lnk.Description       = 'Scan all UART/COM ports for a shell'
    $lnk.WindowStyle       = 1
    $lnk.Save()
    Write-Host "created: $lnkPath"
}

Write-Host ""
Write-Host "Done. To pin: open Start, type 'UART Scanner', right-click -> Pin to Start / taskbar."

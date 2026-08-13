<#
    create_shortcuts.ps1
    Creates "UART Scanner" shortcuts (Desktop + Start Menu) that launch
    uart_scan.bat. After running, you can right-click the Start Menu entry and
    choose "Pin to Start" / "Pin to taskbar".

    Run from anywhere; it resolves paths relative to the repo root.
#>

$ErrorActionPreference = 'Stop'

# Repo root = parent of this script's folder.
$root    = Split-Path -Parent $PSScriptRoot
$vbs     = Join-Path $root 'scripts\launch.vbs'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$exe     = Join-Path $root 'dist\uart_scan.exe'

if (-not (Test-Path $vbs)) {
    throw "launch.vbs not found ($vbs)"
}

# Use the built exe as the icon if present, else the generic console icon.
$icon = if (Test-Path $exe) { "$exe,0" } else { "$env:SystemRoot\System32\cmd.exe,0" }

$ws = New-Object -ComObject WScript.Shell
$targets = @(
    [System.Environment]::GetFolderPath('Desktop'),
    (Join-Path $env:AppData 'Microsoft\Windows\Start Menu\Programs')
)

foreach ($dir in $targets) {
    $lnkPath = Join-Path $dir 'UART Scanner.lnk'
    $lnk = $ws.CreateShortcut($lnkPath)
    # wscript runs launch.vbs with no console of its own -> the only window that
    # appears is the pre-sized "UART Scanner" console (no resize flash).
    $lnk.TargetPath       = $wscript
    $lnk.Arguments        = '"' + $vbs + '"'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = $icon
    $lnk.Description       = 'Scan all UART/COM ports for a shell'
    $lnk.WindowStyle       = 1
    $lnk.Save()
    Write-Host "created: $lnkPath"
}

Write-Host ""
Write-Host "Done. To pin: open Start, type 'UART Scanner', right-click -> Pin to Start / taskbar."

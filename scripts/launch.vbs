' ---------------------------------------------------------------------------
' launch.vbs  -  flash-free launcher for the UART scanner.
'
' The visible "jump" you get from `mode con` happens because the window is
' created at the default size and then resized. To avoid it we:
'   1. Write the desired console geometry to HKCU\Console\UART Scanner.
'   2. Open a NEW console whose title is exactly "UART Scanner" -- conhost
'      applies that registry geometry AT WINDOW CREATION, so it opens already
'      sized. No resize, no flash.
' The bootstrap cmd is run hidden (window style 0) so only the sized window shows.
' ---------------------------------------------------------------------------
Option Explicit

Dim sh, fso, q, root, cols, rows, buf, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
q = Chr(34)

' Repo root = parent of this script's \scripts folder.
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

cols = 100
rows = 40
buf  = 1000     ' scrollback height (>= window height)

' DWORD packing: low word = width (cols), high word = height (rows).
sh.RegWrite "HKCU\Console\UART Scanner\WindowSize",      rows * 65536 + cols, "REG_DWORD"
sh.RegWrite "HKCU\Console\UART Scanner\ScreenBufferSize", buf  * 65536 + cols, "REG_DWORD"

' Open the pre-sized, titled window and run the inner batch inside it.
cmd = "cmd /c start " & q & "UART Scanner" & q & " " & q & root & "\run_app.bat" & q
sh.Run cmd, 0, False   ' 0 = hide the transient bootstrap window

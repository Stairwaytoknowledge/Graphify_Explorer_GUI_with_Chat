' Graphify Explorer - silent launcher (no console window).
' Double-click this file (or the Desktop shortcut) to open the GUI.
'
' Order of preference:
'   1. The venv's pythonw.exe stub (smallest, fastest path).
'   2. The venv's "home" python (read from .venv\pyvenv.cfg). This
'      bypasses the venv stub when WDAC / AppLocker blocks it.
'   3. pythonw on PATH (system install).
' graphify_gui.py self-bootstraps the venv site-packages, so any of
' the above interpreters work.
Option Explicit
Dim sh, fso, here, gui, venvPyw, cfg, homeDir, homePyw
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
gui  = here & "\graphify_gui.py"
sh.CurrentDirectory = here

' --- 1. venv stub (preferred) ------------------------------------------
venvPyw = here & "\.venv\Scripts\pythonw.exe"
If LaunchAndCheck(sh, venvPyw, gui) Then WScript.Quit 0

' --- 2. venv "home" interpreter (fallback when stub is blocked) --------
cfg = here & "\.venv\pyvenv.cfg"
homeDir = ParsePyvenvHome(fso, cfg)
If Len(homeDir) > 0 Then
    homePyw = homeDir & "\pythonw.exe"
    If fso.FileExists(homePyw) Then
        If LaunchAndCheck(sh, homePyw, gui) Then WScript.Quit 0
    End If
End If

' --- 3. system pythonw on PATH -----------------------------------------
On Error Resume Next
sh.Run "pythonw """ & gui & """", 1, False
If Err.Number = 0 Then
    On Error Goto 0
    WScript.Quit 0
End If
On Error Goto 0

MsgBox "Graphify could not start. The venv interpreter appears to be" & _
       " blocked by an Application Control policy, and no fallback" & _
       " python was found." & vbCrLf & vbCrLf & _
       "Open a terminal in this folder and run:" & vbCrLf & _
       "    python graphify_gui.py" & vbCrLf & _
       "to see the underlying error.", vbCritical, "Graphify Explorer"
WScript.Quit 1


' --- helpers -----------------------------------------------------------
Function LaunchAndCheck(sh, exePath, scriptPath)
    Dim fso2
    Set fso2 = CreateObject("Scripting.FileSystemObject")
    LaunchAndCheck = False
    If Not fso2.FileExists(exePath) Then Exit Function
    On Error Resume Next
    sh.Run """" & exePath & """ """ & scriptPath & """", 1, False
    If Err.Number = 0 Then LaunchAndCheck = True
    On Error Goto 0
End Function

Function ParsePyvenvHome(fso, cfgPath)
    Dim ts, line, idx, val
    ParsePyvenvHome = ""
    If Not fso.FileExists(cfgPath) Then Exit Function
    Set ts = fso.OpenTextFile(cfgPath, 1)
    Do Until ts.AtEndOfStream
        line = ts.ReadLine
        idx = InStr(line, "=")
        If idx > 0 Then
            If LCase(Trim(Left(line, idx - 1))) = "home" Then
                val = Trim(Mid(line, idx + 1))
                ParsePyvenvHome = val
                Exit Do
            End If
        End If
    Loop
    ts.Close
End Function

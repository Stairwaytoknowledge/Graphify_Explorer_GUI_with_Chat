' Graphify Explorer - silent launcher (no console window)
' Double-click this file (or the Desktop shortcut) to open the GUI.
Option Explicit
Dim sh, fso, here, pyw, gui
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\.venv\Scripts\pythonw.exe"
gui = here & "\graphify_gui.py"
If Not fso.FileExists(pyw) Then
    MsgBox "Graphify is not installed yet. Please run Install-Windows.bat first.", _
        vbExclamation, "Graphify Explorer"
    WScript.Quit 1
End If
sh.CurrentDirectory = here
sh.Run """" & pyw & """ """ & gui & """", 1, False

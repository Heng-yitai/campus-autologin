' Campus Network One-Click Login - silent launcher
' Double-click the desktop shortcut which points here.
Option Explicit

Dim fso, sh, baseDir, pyw, cmd, candidates, i, found

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)

candidates = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "pythonw.exe" _
)

found = ""
For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        found = candidates(i)
        Exit For
    End If
Next

If found = "" Then
    MsgBox "Python 3.8+ not found." & vbCrLf & vbCrLf & _
           "Please install Python and make sure pythonw.exe is on PATH, " & _
           "or run: pythonw campus_login.py", _
           16, "Campus Login"
    WScript.Quit 1
End If

sh.CurrentDirectory = baseDir
cmd = """" & found & """ """ & baseDir & "\campus_login.py"""
sh.Run cmd, 0, False

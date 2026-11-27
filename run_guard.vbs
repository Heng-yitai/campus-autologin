' Campus Network Guard - silent launcher / stopper
' Usage: wscript run_guard.vbs           -> start in background (pythonw, no window)
'        wscript run_guard.vbs stop      -> stop via net_guard.py --stop
Option Explicit

Dim fso, sh, baseDir, pyw, mode, candidates, i, found, py, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
mode = ""
If WScript.Arguments.Count > 0 Then mode = LCase(WScript.Arguments(0))

candidates = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe" _
)

found = ""
For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        found = candidates(i)
        Exit For
    End If
Next

If found = "" Then
    MsgBox "Python 3.8+ not found." & vbCrLf & vbCrLf & "Please install Python and make sure pythonw.exe is on PATH.", 16, "Campus Guard"
    WScript.Quit 1
End If

sh.CurrentDirectory = baseDir

If mode = "stop" Then
    ' console python for visible output
    py = Replace(found, "pythonw.exe", "python.exe")
    If Not fso.FileExists(py) Then py = found
    cmd = """" & py & """ """ & baseDir & "\net_guard.py"" --stop"
    sh.Run cmd, 1, True
Else
    cmd = """" & found & """ """ & baseDir & "\net_guard.py"""
    sh.Run cmd, 0, False
End If

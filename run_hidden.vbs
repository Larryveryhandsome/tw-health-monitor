' Run one health check hidden (pythonw, no console window). Used by Task Scheduler.
Option Explicit
Dim sh, fso, here, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
On Error Resume Next
For Each exe In Array("pythonw", "pyw")
  Err.Clear
  sh.Run exe & " -X utf8 ""monitor.py""", 0, False
  If Err.Number = 0 Then WScript.Quit 0
Next

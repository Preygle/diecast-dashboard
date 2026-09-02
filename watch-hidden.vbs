' Runs Watch.bat with no console window. Arg 1 is passed through ("" or "blinkit").
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
arg = ""
If WScript.Arguments.Count > 0 Then arg = " " & WScript.Arguments(0)
sh.CurrentDirectory = root
sh.Run """" & root & "\Watch.bat""" & arg, 0, False

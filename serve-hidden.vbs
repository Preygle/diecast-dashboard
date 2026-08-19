' Starts Serve.bat with no visible console window, for the logon task.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run """" & sh.CurrentDirectory & "\Serve.bat""", 0, False

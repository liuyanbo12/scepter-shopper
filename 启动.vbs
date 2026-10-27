Option Explicit
Dim shell, fso, folder, nodePath, launcherPath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
nodePath = shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\nodejs\node.exe"
launcherPath = folder & "\launcher.cjs"
If Not fso.FileExists(nodePath) Then
  nodePath = "node"
End If
If Not fso.FileExists(folder & "\.venv\Scripts\python.exe") Then
  MsgBox "Please run the dependency installer first.", 48, "Scepter Shopper"
Else
  shell.CurrentDirectory = folder
  shell.Run """" & nodePath & """ """ & launcherPath & """", 0, False
End If

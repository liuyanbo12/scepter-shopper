Option Explicit
Dim shell, fso, folder, scriptPath, result
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = folder & "\restart-services.ps1"
shell.CurrentDirectory = folder
result = shell.Run("powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & scriptPath & """", 0, True)
If result <> 0 Then
  MsgBox "Restart failed. Please run restart-services.ps1 from this project folder or check logs/launcher-error.txt.", 48, "Scepter Shopper"
End If

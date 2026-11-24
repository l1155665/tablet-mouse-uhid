Option Explicit
Dim shell, root, quote
Set shell = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
quote = Chr(34)
shell.CurrentDirectory = root
shell.Run quote & root & "\.venv\Scripts\pythonw.exe" & quote & " " & quote & root & "\tablet_mouse_gui.py" & quote, 0, False

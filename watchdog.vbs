' watchdog.vbs - relaunch the meeting-recorder tray if it isn't running, or if
' it's running but hung. Runs invisibly (wscript, window style 0). Safe to run
' repeatedly: the tray's single-instance guard makes a redundant launch exit harmlessly.
'
' "Hung" = the tray's heartbeat file (written by its tk thread every 30 s as
' "<pid> <recording>") names this process and is older than STALE_SECS. A tray
' whose last heartbeat says it was recording is never killed (that would lose the
' meeting), and neither is one with no heartbeat for its pid (an older build).
'
' PYTHONW defaults to pyw.exe, the Python launcher's windowless twin: the python.org
' installer puts it on PATH even when python/pythonw themselves aren't. Set it to a
' full pythonw.exe path to pin a specific interpreter.
Option Explicit
Const STALE_SECS = 180
Const PYTHONW = "pyw.exe"
Dim wmi, procs, p, proc, sh, fso, DIR_
Set fso = CreateObject("Scripting.FileSystemObject")
DIR_ = fso.GetParentFolderName(WScript.ScriptFullName)   ' lives next to meeting_tray.py
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe'")
Set proc = Nothing
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(LCase(p.CommandLine), "meeting_tray.py") > 0 Then Set proc = p
    End If
Next

Sub Log(msg)
    Dim f
    Set f = fso.OpenTextFile(DIR_ & "\watchdog.log", 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
End Sub

If Not proc Is Nothing Then
    Dim hb, parts, age
    hb = DIR_ & "\tray.heartbeat"
    parts = Array()
    If fso.FileExists(hb) Then
        On Error Resume Next      ' empty / mid-write heartbeat: just skip this round
        parts = Split(Trim(Replace(Replace(fso.OpenTextFile(hb).ReadAll, vbCr, ""), vbLf, "")), " ")
        age = DateDiff("s", fso.GetFile(hb).DateLastModified, Now)
        If Err.Number <> 0 Then parts = Array()
        On Error GoTo 0
        If UBound(parts) >= 1 Then
            If CStr(parts(0)) = CStr(proc.ProcessId) And age > STALE_SECS Then
                If parts(1) = "0" Then
                    Log "tray pid " & proc.ProcessId & " heartbeat " & age & "s old - killing and relaunching"
                    proc.Terminate
                    WScript.Sleep 1500
                    Set proc = Nothing
                Else
                    Log "tray pid " & proc.ProcessId & " heartbeat " & age & "s old but was recording - leaving it alone"
                End If
            End If
        End If
    End If
End If

If proc Is Nothing Then
    Set sh = CreateObject("WScript.Shell")
    sh.CurrentDirectory = DIR_
    sh.Run """" & PYTHONW & """ """ & DIR_ & "\meeting_tray.py""", 0, False
End If

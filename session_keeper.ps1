param(
    [string]$User = "",
    [int]$SessionId = -1,
    [switch]$DryRun
)
# Keeps a Windows session drawing after Remote Desktop disconnects, so the WeChat
# automation can still take screenshots and send input.
#   Scheduled task (as SYSTEM, on session disconnect): -User <name>
#     moves that user's *disconnected* sessions to the physical console.
#   Manual (keep_running_and_disconnect.bat): -SessionId <id>
#     moves the given session to the console right away.
# The console desktop is left unlocked; this is a deliberate, user-accepted trade-off.
$ErrorActionPreference = "Stop"
$logPath = Join-Path $PSScriptRoot "logs\session_keeper.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
function Write-Log([string]$Message) {
    Add-Content -Path $logPath -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
}

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class WtsSessions {
    [StructLayout(LayoutKind.Sequential)]
    struct WTS_SESSION_INFO { public int SessionId; public IntPtr WinStationName; public int State; }
    // CharSet.Unicode binds the W variants; the default (ANSI) returned garbled user
    // names, so a disconnected session never matched and was never moved.
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode)] static extern bool WTSEnumerateSessions(IntPtr server, int reserved, int version, out IntPtr info, out int count);
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode)] static extern bool WTSQuerySessionInformation(IntPtr server, int sessionId, int infoClass, out IntPtr buffer, out int bytes);
    [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr memory);
    static string Query(int id, int infoClass) {
        IntPtr buffer; int bytes;
        if (!WTSQuerySessionInformation(IntPtr.Zero, id, infoClass, out buffer, out bytes)) return "";
        try { return Marshal.PtrToStringUni(buffer) ?? ""; } finally { WTSFreeMemory(buffer); }
    }
    // Returns "id|state|user"; state 0 = active, 4 = disconnected. Locale-independent.
    public static string[] List() {
        IntPtr info; int count;
        var result = new List<string>();
        if (!WTSEnumerateSessions(IntPtr.Zero, 0, 1, out info, out count)) return result.ToArray();
        try {
            int size = Marshal.SizeOf(typeof(WTS_SESSION_INFO));
            for (int i = 0; i < count; i++) {
                var s = (WTS_SESSION_INFO)Marshal.PtrToStructure(new IntPtr(info.ToInt64() + i * size), typeof(WTS_SESSION_INFO));
                result.Add(s.SessionId + "|" + s.State + "|" + Query(s.SessionId, 5));  // 5 = WTSUserName
            }
        } finally { WTSFreeMemory(info); }
        return result.ToArray();
    }
}
"@

function Move-ToConsole([int]$Id, [string]$Why) {
    if ($DryRun) { Write-Log "dry run: would move session $Id to console ($Why)"; return }
    & "$env:SystemRoot\System32\tscon.exe" $Id /dest:console
    Write-Log "moved session $Id to console ($Why), exit code $LASTEXITCODE"
}

try {
    if ($SessionId -ge 0) {
        Move-ToConsole $SessionId "manual"
        exit 0
    }
    if (-not $User) { throw "Pass -User <name> or -SessionId <id>." }
    Start-Sleep -Seconds 2  # let the session state settle after the disconnect event
    $targets = @()
    foreach ($entry in [WtsSessions]::List()) {
        $id, $state, $name = $entry -split "\|", 3
        # Only disconnected sessions of this user; logging on locally or reconnecting
        # leaves the session active, so those disconnect events are ignored.
        if ([int]$state -eq 4 -and $name -ieq $User) { $targets += [int]$id }
    }
    if (-not $targets) { Write-Log "no disconnected session for $User, nothing to do"; exit 0 }
    foreach ($id in $targets) { Move-ToConsole $id "remote desktop disconnected" }
} catch {
    Write-Log "error: $($_.Exception.Message)"
    exit 1
}

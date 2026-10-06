param([string]$Artifact = 'MailGroup-3.0.0-x64-portable.exe')
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$executable = Join-Path $workspace "release/desktop-v3/$Artifact"
$profilePath = Join-Path ([IO.Path]::GetTempPath()) ("mailgroup-release-" + [guid]::NewGuid().ToString('N'))
$outputPath = Join-Path $PSScriptRoot 'desktop-v3'
$pythonPath = Join-Path $workspace '.venv/Scripts/python.exe'
$savedPath = $env:PATH
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class MailGroupWindowCheck {
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int index);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out Rect r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
}
'@
$runs = @()
try {
    # Child processes see only Windows system binaries, no developer runtimes.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $launch = Start-Process -FilePath $executable -ArgumentList "--user-data-dir=$profilePath" -WindowStyle Hidden -PassThru
        $appProcess = $null
        $service = $null
        $port = $null
        $deadline = [DateTime]::UtcNow.AddSeconds(70)
        while ([DateTime]::UtcNow -lt $deadline) {
            $candidate = Get-CimInstance Win32_Process -Filter "Name='邮件群发助手.exe'" | Where-Object { $_.CommandLine -like "*$profilePath*" -and $_.CommandLine -notlike '*--type=*' } | Select-Object -First 1
            if ($candidate) {
                $appProcess = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
                $service = Get-CimInstance Win32_Process -Filter "Name='mail-group-service.exe'" | Where-Object ParentProcessId -eq $candidate.ProcessId | Select-Object -First 1
                if ($service) {
                    $socket = Get-NetTCPConnection -OwningProcess $service.ProcessId -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($socket -and $appProcess.MainWindowHandle -ne 0) {
                        $port = $socket.LocalPort
                        try {
                            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
                            if ($health.status -eq 'ok') { break }
                        } catch { }
                    }
                }
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $port -or -not $appProcess -or $appProcess.MainWindowHandle -eq 0) { throw 'Packaged application did not become ready' }
        $unauthorized = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/sender-configs" -SkipHttpErrorCheck -TimeoutSec 3
        if ($unauthorized.StatusCode -ne 401) { throw 'Anonymous packaged API was not rejected' }
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -TimeoutSec 3
        if ($response.StatusCode -ne 200 -or $response.Content -notlike '*assets/index-*') { throw 'Bundled frontend is missing' }
        $style = [MailGroupWindowCheck]::GetWindowLong($appProcess.MainWindowHandle, -16)
        $title = $appProcess.MainWindowTitle
        $mainId = $appProcess.Id
        $serviceId = $service.ProcessId
        if ($attempt -eq 1) {
            Start-Sleep -Milliseconds 1000
            Add-Type -AssemblyName System.Drawing
            $oldDpiContext = [MailGroupWindowCheck]::SetThreadDpiAwarenessContext([IntPtr](-4))
            $rect = New-Object MailGroupWindowCheck+Rect
            [void][MailGroupWindowCheck]::GetWindowRect($appProcess.MainWindowHandle, [ref]$rect)
            $bitmap = New-Object System.Drawing.Bitmap(($rect.Right-$rect.Left), ($rect.Bottom-$rect.Top))
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $dc = $graphics.GetHdc()
            try { [void][MailGroupWindowCheck]::PrintWindow($appProcess.MainWindowHandle, $dc, 2) }
            finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
            $bitmap.Save((Join-Path $outputPath 'portable-native-window.png'), [System.Drawing.Imaging.ImageFormat]::Png)
            $bitmap.Dispose()
            [void][MailGroupWindowCheck]::SetThreadDpiAwarenessContext($oldDpiContext)
        }
        [void][MailGroupWindowCheck]::PostMessage($appProcess.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
        Wait-Process -Id $mainId -Timeout 55 -ErrorAction SilentlyContinue
        if (Get-Process -Id $mainId,$serviceId -ErrorAction SilentlyContinue) { throw 'Application or service survived closing the window' }
        $databasePath = Join-Path $profilePath 'data/mail_group.sqlite3'
        $env:MAIL_GROUP_TEST_DB = $databasePath
        $counts = & $pythonPath -B -c 'import os, sqlite3, json; c=sqlite3.connect(os.environ["MAIL_GROUP_TEST_DB"]); print(json.dumps({t:c.execute("select count(*) from "+t).fetchone()[0] for t in ["sender_configs","recipients","saved_mails","task_runs"]})); c.close()'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect isolated database' }
        $counts = $counts | ConvertFrom-Json
        if ($counts.sender_configs -ne 0 -or $counts.saved_mails -ne 0 -or $counts.task_runs -ne 0) { throw 'Personal/test data was bundled' }
        if ($counts.recipients -ne ($attempt - 1)) { throw 'Fresh start or restart persistence failed' }
        # Electron can retain WS_CAPTION for resizing/shadow while drawing a frameless window.
        # Visual confirmation uses PrintWindow above; this style bit is not a frame assertion.
        $runs += @{ attempt=$attempt; title=$title; port=$port; win32Style=$style; serviceExited=$true; counts=$counts }
        if ($attempt -eq 1) {
            & $pythonPath -B -c 'import os,sqlite3; c=sqlite3.connect(os.environ["MAIL_GROUP_TEST_DB"]); c.execute("insert into recipients (email,note,enabled,created_at) values (?,?,?,?)",("persist@example.com","release smoke",0,"2026-10-06T12:00:00")); c.commit(); c.close()'
            if ($LASTEXITCODE -ne 0) { throw 'Cannot seed restart persistence check' }
        }
    }
    @{ passed=$true; artifact=$Artifact; developerRuntimesOnChildPath=$false; realSmtpCalls=0; isolatedProfile=$profilePath; runs=$runs } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outputPath 'binary-smoke.json') -Encoding utf8
    Get-Content -LiteralPath (Join-Path $outputPath 'binary-smoke.json')
} finally {
    $env:PATH = $savedPath
    Remove-Item Env:MAIL_GROUP_TEST_DB -ErrorAction SilentlyContinue
    # Close only the application's own isolated profile if an assertion failed.
    Get-CimInstance Win32_Process -Filter "Name='邮件群发助手.exe'" | Where-Object { $_.CommandLine -like "*$profilePath*" -and $_.CommandLine -notlike '*--type=*' } | ForEach-Object {
        $remaining = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        if ($remaining -and $remaining.MainWindowHandle -ne 0) { [void][MailGroupWindowCheck]::PostMessage($remaining.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    }
}

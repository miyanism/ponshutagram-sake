# refresh_index.ps1 - rebuild caption_index.json, commit and push if it changed.
#
# Purpose: keep the post index fresh so the IG auto-reply bot / TikTok reply bot
#          can match the latest videos. The cloud side cannot rebuild the index
#          (it has no access to the local video folders), so this runs on this PC.
#
# Intended to run from Windows Task Scheduler (daily 22:00). The git remote has an
# embedded PAT, so push works non-interactively.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 (powershell.exe) reads a
#       BOM-less script as the system ANSI codepage (cp932), which corrupts any
#       non-ASCII characters and breaks parsing. ASCII avoids that entirely.

$ErrorActionPreference = "Stop"
$repo = "C:\Users\user\claude.honten"
$app  = Join-Path $repo "railway-app"
$log  = Join-Path $app "refresh_index.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$ts  $msg" -Encoding UTF8
}

try {
    Set-Location $app

    # 1) rebuild the index (append-type: keeps past posts no longer on disk)
    #    Do not log python's stdout: it contains Japanese and PS 5.1 would mojibake it.
    $env:PYTHONIOENCODING = "utf-8"
    & python build_caption_index.py 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "rebuild FAILED (python exit $LASTEXITCODE)"
        exit 1
    }
    Write-Log "rebuild ok"

    # 2) nothing changed -> stop (avoid empty commits)
    Set-Location $repo
    & git diff --quiet -- railway-app/caption_index.json
    if ($LASTEXITCODE -eq 0) {
        Write-Log "no change -> skip commit"
        exit 0
    }

    # 3) changed -> commit and push only this file
    & git pull --rebase --quiet 2>&1 | Out-Null
    & git add railway-app/caption_index.json
    & git commit -q -m "chore(index): auto rebuild caption_index (scheduled)" 2>&1 | Out-Null
    $push = & git push 2>&1
    Write-Log ("pushed: " + ($push -join " | "))
    exit 0
}
catch {
    Write-Log ("ERROR: " + $_.Exception.Message)
    exit 1
}

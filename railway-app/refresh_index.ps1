# refresh_index.ps1 - rebuild caption_index.json, commit and push if it changed.
#
# Purpose: keep the post index fresh so the IG auto-reply bot / TikTok reply bot
#          can match the latest videos. The cloud side cannot rebuild the index
#          (it has no access to the local video folders), so this runs on this PC.
#
# Runs from Windows Task Scheduler (daily 22:00). The git remote has an embedded
# PAT, so push works non-interactively.
#
# NOTES (learned the hard way):
#  - Keep this file ASCII-only. Windows PowerShell 5.1 (powershell.exe) reads a
#    BOM-less script as the system ANSI codepage (cp932) and breaks parsing otherwise.
#  - Do NOT use $ErrorActionPreference='Stop'. Native git prints harmless warnings
#    (e.g. LF/CRLF) to stderr; under Stop those become terminating errors. Instead
#    check $LASTEXITCODE explicitly after each git call.
#  - Commit the index FIRST, then 'git pull --rebase', then push. pull --rebase
#    refuses to run with unstaged changes; committing first keeps the tree clean.
#    --autostash tolerates any stray unstaged change (e.g. this script itself).

$ErrorActionPreference = "Continue"
$repo = "C:\Users\user\claude.honten"
$app  = Join-Path $repo "railway-app"
$log  = Join-Path $app "refresh_index.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$ts  $msg" -Encoding UTF8
}

# 1) rebuild the index (append-type: keeps past posts no longer on disk)
Set-Location $app
$env:PYTHONIOENCODING = "utf-8"
& python build_caption_index.py 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Log "rebuild FAILED (python exit $LASTEXITCODE)"; exit 1 }
Write-Log "rebuild ok"

# 2) nothing changed -> stop (avoid empty commits)
Set-Location $repo
& git diff --quiet -- railway-app/caption_index.json
if ($LASTEXITCODE -eq 0) { Write-Log "no change -> skip commit"; exit 0 }

# 3) changed -> commit this file first, rebase onto remote, then push
& git add railway-app/caption_index.json 2>&1 | Out-Null
& git commit -q -m "chore(index): auto rebuild caption_index (scheduled)" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Log "commit FAILED (git exit $LASTEXITCODE)"; exit 1 }
& git pull --rebase --autostash 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Log "pull --rebase FAILED (git exit $LASTEXITCODE)"; exit 1 }
& git push 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Log "push FAILED (git exit $LASTEXITCODE)"; exit 1 }
Write-Log "pushed ok"
exit 0

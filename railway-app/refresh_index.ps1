# refresh_index.ps1 — caption_index.json を再生成し、変化があればcommit&pushする
#
# 目的: 新しい動画（claude.scenario/output・claude.short/scenarios・claude.movie/input）が
#       増えたら索引を最新化し、IG自動返信bot / TikTok返信案ボットが「未照合」にならないようにする。
#       クラウド側は動画フォルダを見られないため、材料のあるこのPCで定期実行する。
#
# Windowsタスクスケジューラから定期実行する想定（例: 数時間おき）。
# gitのremoteにはPATが埋め込まれているので非対話でpushできる。

$ErrorActionPreference = "Stop"
$repo = "C:\Users\user\claude.honten"
$app  = Join-Path $repo "railway-app"
$log  = Join-Path $app "refresh_index.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$ts  $msg" -Encoding UTF8
}

try {
    Set-Location $app

    # 1) 索引を再生成（追記型: ローカルから消えた過去投稿も保持）
    $env:PYTHONIOENCODING = "utf-8"
    $out = & python build_caption_index.py 2>&1
    Log ("rebuild: " + ($out -join " | "))

    # 2) 変化が無ければ何もしない（空コミット防止）
    Set-Location $repo
    & git diff --quiet -- railway-app/caption_index.json
    if ($LASTEXITCODE -eq 0) {
        Log "no change -> skip commit"
        exit 0
    }

    # 3) 変化があればこのファイルだけcommit&push
    & git pull --rebase --quiet 2>&1 | Out-Null
    & git add railway-app/caption_index.json
    & git commit -q -m "chore(index): caption_index自動再生成（定期タスク）" 2>&1 | Out-Null
    $push = & git push 2>&1
    Log ("pushed: " + ($push -join " | "))
    exit 0
}
catch {
    Log ("ERROR: " + $_.Exception.Message)
    exit 1
}

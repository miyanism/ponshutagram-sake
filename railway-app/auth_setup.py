# -*- coding: utf-8 -*-
"""
【初回のみ実行】Google OAuth2のリフレッシュトークンと
GMBのアカウントID・ロケーションIDを取得するスクリプト

使い方:
  pip install google-auth-oauthlib requests
  python auth_setup.py
"""
import json
import requests
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

print("=" * 60)
print("Google認証セットアップ")
print("=" * 60)
print()
print("【準備】Google Cloud Consoleで以下を完了してください:")
print("  1. https://console.cloud.google.com/ にアクセス")
print("  2. 新規プロジェクト作成（または既存を選択）")
print("  3. 「APIとサービス」→「ライブラリ」→")
print("     「My Business Business Information API」を有効化")
print("  4. 「APIとサービス」→「認証情報」→")
print("     「認証情報を作成」→「OAuthクライアントID」")
print("  5. アプリの種類:「デスクトップアプリ」で作成")
print("  6. JSONをダウンロードして client_secret.json として保存")
print()
input("client_secret.json を同じフォルダに置いたら Enter を押してください...")

# OAuth2 フロー実行
flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

print()
print("=" * 60)
print("✅ 認証成功！以下をRailwayの環境変数に設定してください")
print("=" * 60)
print(f"GOOGLE_CLIENT_ID     = {creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET = {creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
print()

# GMB アカウント一覧取得
token = creds.token
headers = {"Authorization": f"Bearer {token}"}

print("GMBアカウント一覧を取得中...")
r = requests.get(
    "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
    headers=headers,
)
accounts = r.json().get("accounts", [])
print()
for acc in accounts:
    print(f"  アカウント名: {acc['name']}  ({acc.get('accountName', '')})")

print()
account_name = input("使用するアカウント名を入力してください (例: accounts/123456789): ").strip()

# ロケーション一覧取得
print()
print("ロケーション（店舗）一覧を取得中...")
r = requests.get(
    f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_name}/locations"
    "?readMask=name,title",
    headers=headers,
)
locations = r.json().get("locations", [])
for loc in locations:
    print(f"  ロケーション名: {loc['name']}  ({loc.get('title', '')})")

print()
print("=" * 60)
print("以下を Railway の環境変数 GMB_LOCATION に設定してください:")
print()
if locations:
    # v4形式に変換: accounts/xxx/locations/yyy
    for loc in locations:
        name = loc["name"]  # 例: locations/12345678901234567
        # account付きのフルパスに変換
        loc_id = name.split("/")[-1]
        acc_id = account_name.split("/")[-1]
        print(f"  GMB_LOCATION = accounts/{acc_id}/locations/{loc_id}")
print("=" * 60)

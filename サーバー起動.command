#!/bin/bash
cd "$(dirname "$0")"
echo "================================================"
echo "  競艇サインマイナー サーバー"
echo "================================================"

# 依存チェック
if ! python3 -c "import requests, bs4" 2>/dev/null; then
    echo "[INFO] 必要なライブラリをインストールします..."
    python3 -m pip install --user -q -r requirements.txt
fi

echo ""
echo "サーバーを起動します..."
echo "ブラウザで http://localhost:8772/ にアクセスしてください"
echo ""
python3 server.py

#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 server.py
else
  echo "找不到 Python 3。請先到 https://www.python.org/downloads/ 下載安裝，裝完再雙擊這個檔案。"
fi
echo
read -n 1 -s -r -p "按任意鍵關閉視窗…"

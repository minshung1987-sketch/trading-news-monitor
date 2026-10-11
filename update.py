# -*- coding: utf-8 -*-
"""一鍵更新：從 GitHub 抓這個專案的最新版本，覆蓋到這個資料夾裡的程式碼檔案。

不會動到你的設定（config.json）跟新聞資料庫（news.db / news.db-wal / news.db-shm）——
不管下載回來的新版裡有沒有這些檔案，一律保留你本機現有的版本，不覆蓋、不刪除。

只會新增／更新檔案，不會刪除你本機多出來的檔案（例如你自己加的來源腳本）；如果以後哪個
檔案在新版裡被整個移除，舊的那份會留在你的資料夾裡，不受影響，但也不會自動幫你清掉。

用法：雙擊 update.command（Mac）或 update.bat（Windows），或直接執行「python update.py」。
更新完之後，重新啟動一次程式（關掉視窗再雙擊 start.command / start.bat）才會套用新版本。
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

REPO = "minshung1987-sketch/trading-news-monitor"
ZIP_URL = "https://github.com/%s/archive/refs/heads/main.zip" % REPO
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 這幾個檔案一律保留你本機現有的版本，不管新版裡有沒有、內容是什麼。
PROTECTED = {"config.json", "news.db", "news.db-wal", "news.db-shm"}


def log(msg=""):
    print(msg, flush=True)


def main():
    log("正在從 GitHub 下載最新版本…")
    try:
        with urllib.request.urlopen(ZIP_URL, timeout=30) as r:
            data = r.read()
    except Exception as e:
        log("下載失敗：%s" % e)
        log("請檢查網路連線是否正常；如果一直失敗，到 https://github.com/%s 確認這個 repo 還存在。" % REPO)
        return 1

    tmp_dir = tempfile.mkdtemp(prefix="tnm_update_")
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile:
            log("下載回來的內容不是有效的壓縮檔，可能是 GitHub 那邊暫時有問題，晚點再試一次。")
            return 1

        entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
        if len(entries) != 1:
            log("下載回來的內容格式跟預期不同，沒有更新任何檔案，晚點再試一次。")
            return 1
        src_root = os.path.join(tmp_dir, entries[0])

        updated, added, skipped = [], [], []
        for root, dirs, files in os.walk(src_root):
            dirs[:] = [d for d in dirs if d != ".git"]
            rel_dir = os.path.relpath(root, src_root)
            for fname in files:
                rel_path = fname if rel_dir == "." else os.path.normpath(os.path.join(rel_dir, fname))
                if rel_path.replace("\\", "/") in PROTECTED or fname == ".gitignore":
                    continue
                src_path = os.path.join(root, fname)
                dst_path = os.path.join(BASE_DIR, rel_path)
                with open(src_path, "rb") as f:
                    new_content = f.read()
                existed = os.path.exists(dst_path)
                old_content = None
                if existed:
                    with open(dst_path, "rb") as f:
                        old_content = f.read()
                if new_content == old_content:
                    continue
                os.makedirs(os.path.dirname(dst_path) or BASE_DIR, exist_ok=True)
                with open(dst_path, "wb") as f:
                    f.write(new_content)
                (updated if existed else added).append(rel_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log()
    if added or updated:
        for f in sorted(added):
            log("  新增：%s" % f)
        for f in sorted(updated):
            log("  更新：%s" % f)
        log()
        log("已更新 %d 個檔案。你的設定（config.json）跟新聞資料庫（news.db）沒有被動到。" % (len(added) + len(updated)))
        log("重新啟動程式（關掉這個視窗，再重新雙擊 start.command / start.bat）即可套用新版本。")
    else:
        log("已經是最新版本，沒有東西需要更新。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)

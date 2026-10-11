# -*- coding: utf-8 -*-
"""英文標題 → 繁體中文（機器翻譯，僅供參考）。

用的是 Google 翻譯網頁版背後的非官方接口：免費、不用申請金鑰，
但它不是官方提供的服務，可能被限流，也可能哪天改版失效。
失效時只會少了中文那一行，其他功能不受影響。

只在使用者實際看到某則英文新聞時才翻譯，翻過的存進資料庫，不會重複翻。
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse

from fetcher import describe_error, http_get
from monitor import log

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LAT = re.compile(r"[A-Za-z]")

MAX_IDS_PER_CALL = 60
MAX_NEW_PER_CALL = 25
BUDGET_SECONDS = 20
PAUSE_BETWEEN = 0.25
COOLDOWN_SECONDS = 180
RETRY_WAIT = 1.0            # 單一標題失敗後，等幾秒再試一次
MAX_FAILS_PER_TITLE = 3     # 同一則連續失敗幾次就先放棄（重新啟動程式後會再試）
MAX_STREAK = 3              # 連續幾則都失敗，就判斷整個服務有問題


def needs_translation(title):
    """標題是否是英文（不含中文字、英文字母夠多）。中文標題不需要翻。"""
    title = title or ""
    if len(_CJK.findall(title)) >= 2:
        return False
    letters = len(_LAT.findall(title))
    visible = len(re.sub(r"\s", "", title))
    return letters >= 8 and letters / max(1, visible) >= 0.5


def parse_gtx(data):
    """解析接口回傳：[[["譯文","原文",...], ...], ...] → 把每一段譯文接起來。"""
    try:
        segs = json.loads(data.decode("utf-8"))[0]
        text = "".join(s[0] for s in segs if isinstance(s, list) and s and isinstance(s[0], str)).strip()
    except (KeyError, IndexError, TypeError):
        raise ValueError("翻譯服務回傳的格式跟預期不同（可能已改版）")
    if not text:
        raise ValueError("翻譯服務回傳空白")
    return text


class Translator:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store
        self.lock = threading.Lock()
        self.cooldown_until = 0
        self.last_error = ""
        self.fail_counts = {}

    def _one(self, title, st):
        url = st["translate_endpoint"] + "?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "en", "tl": "zh-TW", "dt": "t", "q": title})
        return parse_gtx(http_get(url, st["user_agent"], 10, st.get("verify_ssl", True)))

    def _friendly(self, e):
        if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
            return "翻譯服務暫時拒絕請求（可能太頻繁）"
        if isinstance(e, urllib.error.HTTPError):
            return "翻譯服務回應錯誤（HTTP %d）" % e.code
        if type(e) is ValueError:
            return str(e)
        return describe_error(e)

    def _one_with_retry(self, title, st):
        """單一標題：遇到偶發的伺服器錯誤（5xx、連線中斷）先等一下再試一次。
        429/403 是被限流，重試只會更糟，直接丟出去。"""
        try:
            return self._one(title, st)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
                raise
            time.sleep(RETRY_WAIT)
            return self._one(title, st)

    def run(self, ids):
        """回傳 {"zh": {id: 譯文}, "done": [已處理完的 id], "error": 錯誤訊息或空字串,
                 "retry_after": 建議幾秒後再問（0 = 可以立刻繼續）}。
        done 包含：已翻好的、不需要翻的（中文標題）、放棄的（同一則連續失敗太多次）；
        不在 done 裡的表示還沒處理，稍後可再問。"""
        st = self.monitor.cfg["settings"]
        ids = [int(i) for i in ids][:MAX_IDS_PER_CALL]
        if not st.get("translate", True):
            return {"zh": {}, "done": ids, "error": "disabled", "retry_after": 0}

        order = {i: n for n, i in enumerate(ids)}        # 依畫面順序（新的在前）處理
        rows = sorted(self.store.get_titles(ids), key=lambda r: order.get(r["id"], 0))
        zh, done, todo = {}, [], []
        for r in rows:
            if r["title_zh"] is not None:
                done.append(r["id"])
                if r["title_zh"]:
                    zh[r["id"]] = r["title_zh"]
            elif not needs_translation(r["title"]):
                done.append(r["id"])
            elif self.fail_counts.get(r["id"], 0) >= MAX_FAILS_PER_TITLE:
                done.append(r["id"])                       # 這則一直失敗，這次執行期間不再試
            else:
                todo.append(r)
        if not todo:
            return {"zh": zh, "done": done, "error": "", "retry_after": 0}

        if time.time() < self.cooldown_until:
            return {"zh": zh, "done": done, "error": self.last_error or "翻譯服務暫時無法使用",
                    "retry_after": int(self.cooldown_until - time.time()) + 1}
        if not self.lock.acquire(timeout=0.2):            # 另一批正在處理，稍後再問即可
            return {"zh": zh, "done": done, "error": "", "retry_after": 2}
        try:
            start, n, error, failed, streak = time.time(), 0, "", 0, 0
            for r in todo:
                if n >= MAX_NEW_PER_CALL or time.time() - start > BUDGET_SECONDS:
                    break
                try:
                    text = self._one_with_retry(r["title"], st)
                except Exception as e:
                    error = self._friendly(e)
                    self.last_error = error
                    self.fail_counts[r["id"]] = self.fail_counts.get(r["id"], 0) + 1
                    failed += 1
                    streak += 1
                    log("中文翻譯失敗：%s（技術細節：%s）標題：%s" % (error, repr(e)[:160], r["title"][:50]))
                    rate_limited = isinstance(e, urllib.error.HTTPError) and e.code in (403, 429)
                    if rate_limited or streak >= MAX_STREAK:   # 被限流，或連續好幾則都失敗 → 整個服務先休息
                        self.cooldown_until = time.time() + COOLDOWN_SECONDS
                        log("中文翻譯暫停 %d 秒後自動重試" % COOLDOWN_SECONDS)
                        return {"zh": zh, "done": done, "error": error, "retry_after": COOLDOWN_SECONDS}
                    time.sleep(PAUSE_BETWEEN)
                    continue                                   # 只是這一則失敗 → 跳過，繼續翻下一則
                streak = 0
                n += 1
                if text.strip().lower() == r["title"].strip().lower():
                    text = ""                       # 譯文與原文相同（例如全是專有名詞），不重複顯示
                self.store.set_translation(r["id"], text)
                done.append(r["id"])
                if text:
                    zh[r["id"]] = text
                time.sleep(PAUSE_BETWEEN)
            # 有幾則單獨失敗但整體還在運作：不當成錯誤，但請前端過一會兒再問，不要立刻連續重打
            return {"zh": zh, "done": done, "error": "", "retry_after": 20 if failed else 0}
        finally:
            self.lock.release()

# -*- coding: utf-8 -*-
"""替新聞補上「表格／圖表預覽」與摘要：抓文章頁 → 挑圖 → 存進資料庫。

跟翻譯一樣，只在你實際看到某則新聞時才處理（由網頁要求），處理過的不會再抓。
只記錄圖片網址，不下載圖片檔；圖片由你的瀏覽器直接從新聞網站載入。
"""
import json
import threading
import time
import urllib.error
import urllib.parse

import gnews
from fetcher import describe_error, http_get
from monitor import log
from pageinfo import decode_html, parse_page, pick_images, snippet

MAX_PER_CALL = 8
BUDGET_SECONDS = 22
PAUSE_BETWEEN = 0.35
COOLDOWN_LIMITED = 300        # 被限流（429/403）時暫停幾秒
COOLDOWN_BROKEN = 600         # 解 Google 連結連續失敗時暫停幾秒
MAX_FAILS_PER_ARTICLE = 2
MAX_GOOGLE_STREAK = 3
MAX_NET_STREAK = 4             # 連續幾次連線類錯誤，就當成自己沒網路
COOLDOWN_NET = 120

SKIP_HOSTS = {
    "www.ptt.cc", "ptt.cc", "twitter.com", "x.com", "www.youtube.com", "youtube.com", "youtu.be",
    "www.facebook.com", "facebook.com", "www.reddit.com", "reddit.com", "www.sec.gov", "mops.twse.com.tw",
    "www.twse.com.tw", "www.tpex.org.tw",
}


class _Step(Exception):
    """包住例外並標明是哪一步出的錯：google（解連結）或 page（抓新聞網站）。"""
    def __init__(self, step, cause, host=""):
        super().__init__(str(cause))
        self.step, self.cause, self.host = step, cause, host


class Enricher:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store
        self.lock = threading.Lock()
        self.cooldown_until = 0
        self.last_error = ""
        self.fail_counts = {}
        self.google_streak = 0
        self.net_streak = 0
        self.host_cooldown = {}

    def _friendly(self, e):
        if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
            return "對方網站暫時拒絕請求（HTTP %d，可能太頻繁）" % e.code
        if isinstance(e, gnews.DecodeError):
            return str(e)
        return describe_error(e)

    def _process(self, row, st):
        """處理一則：回傳 (real_url 或 None, 圖片清單, 摘要)。出錯直接丟例外。"""
        ua = st["page_user_agent"]
        timeout = st["request_timeout"]
        verify = st.get("verify_ssl", True)
        url = row["real_url"] or row["url"]
        via_google = False
        if not row["real_url"] and gnews.is_google_news(url):
            via_google = True
            try:
                url = gnews.resolve(url, ua, timeout, verify)
            except Exception as e:
                raise _Step("google", e)
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        real = url if url != row["url"] else None
        if host in SKIP_HOSTS or not url.lower().startswith("http"):
            return real, [], "", via_google
        if self.host_cooldown.get(host, 0) > time.time():
            raise _Step("hostwait", RuntimeError("網站冷卻中"), host)
        try:
            data, headers = http_get(url, ua, timeout, verify, with_headers=True,
                                     headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                                              "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
        except Exception as e:
            raise _Step("page", e, host)
        if "html" not in (headers.get("Content-Type") or "text/html").lower():
            return real, [], "", via_google
        page = parse_page(decode_html(data, headers), url)
        imgs = pick_images(page, st.get("images_mode", "tables"))
        return real, imgs, (snippet(page) if not row["summary"] else ""), via_google

    def run(self, ids):
        """回傳 {"items": {id: {"images": [...], "summary": "..."}}, "done": [id], "error": "", "retry_after": 秒}。"""
        st = self.monitor.cfg["settings"]
        ids = [int(i) for i in ids][:60]
        if not st.get("images", True):
            return {"items": {}, "done": ids, "error": "disabled", "retry_after": 0}
        order = {i: n for n, i in enumerate(ids)}
        rows = sorted(self.store.get_for_enrich(ids), key=lambda r: order.get(r["id"], 0))
        items, done, todo = {}, [], []
        for r in rows:
            if r["images"] is not None:
                done.append(r["id"])
            elif self.fail_counts.get(r["id"], 0) >= MAX_FAILS_PER_ARTICLE:
                done.append(r["id"])
            else:
                todo.append(r)
        if not todo:
            return {"items": items, "done": done, "error": "", "retry_after": 0}
        if time.time() < self.cooldown_until:
            return {"items": items, "done": done, "error": self.last_error or "圖片預覽暫時無法使用",
                    "retry_after": int(self.cooldown_until - time.time()) + 1}
        if not self.lock.acquire(timeout=0.2):
            return {"items": items, "done": done, "error": "", "retry_after": 2}
        try:
            start, n, failed = time.time(), 0, 0
            for r in todo:
                if n >= MAX_PER_CALL or time.time() - start > BUDGET_SECONDS:
                    break
                try:
                    real, imgs, snip, via_google = self._process(r, st)
                except Exception as exc:
                    st_err = exc if isinstance(exc, _Step) else _Step("page", exc, "")   # 沒料到的例外也只算這一則的問題
                    e, step, host = st_err.cause, st_err.step, st_err.host
                    if step == "hostwait":                       # 這個網站剛被限流，先跳過，不算失敗
                        continue
                    error = self._friendly(e)
                    self.last_error = error
                    log("圖片預覽失敗（%s）：%s（技術細節：%s）標題：%s" % (
                        "解 Google 連結" if step == "google" else "抓 " + host, error, repr(e)[:140], r["title"][:36]))
                    limited = isinstance(e, urllib.error.HTTPError) and e.code in (403, 429)
                    if step == "google":
                        self.fail_counts[r["id"]] = self.fail_counts.get(r["id"], 0) + 1
                        failed += 1
                        self.google_streak += 1
                        if limited or self.google_streak >= MAX_GOOGLE_STREAK:
                            secs = COOLDOWN_LIMITED if limited else COOLDOWN_BROKEN
                            self.cooldown_until = time.time() + secs
                            self.last_error = "無法解開 Google 新聞連結（" + error + "）。這個功能用的是 Google 未公開的接口，可能被限制或已改版"
                            log("圖片預覽暫停 %d 秒後自動重試" % secs)
                            return {"items": items, "done": done, "error": self.last_error, "retry_after": secs}
                    else:
                        # 單一網站的問題只記在那則（或那個網站）身上，不影響其他文章
                        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                            self.host_cooldown[host] = time.time() + COOLDOWN_LIMITED
                        elif not isinstance(e, urllib.error.HTTPError):
                            self.net_streak += 1                 # 連線類錯誤連續發生 → 可能是自己沒網路
                        self.fail_counts[r["id"]] = self.fail_counts.get(r["id"], 0) + 1
                        failed += 1
                        if self.fail_counts[r["id"]] >= MAX_FAILS_PER_ARTICLE:
                            self.store.set_enrichment(r["id"], None, "[]", "")     # 一直打不開：標成沒有圖，以後不再試
                            items[r["id"]] = {"images": [], "summary": ""}
                            done.append(r["id"])
                        if self.net_streak >= MAX_NET_STREAK:
                            self.cooldown_until = time.time() + COOLDOWN_NET
                            self.last_error = "連不上新聞網站（" + error + "），請確認網路連線"
                            return {"items": items, "done": done, "error": self.last_error, "retry_after": COOLDOWN_NET}
                    time.sleep(PAUSE_BETWEEN)
                    continue
                if via_google:
                    self.google_streak = 0
                self.net_streak = 0
                n += 1
                self.store.set_enrichment(r["id"], real, json.dumps(imgs, ensure_ascii=False), snip)
                items[r["id"]] = {"images": imgs, "summary": snip}
                done.append(r["id"])
                time.sleep(PAUSE_BETWEEN)
            return {"items": items, "done": done, "error": "", "retry_after": 20 if failed else 0}
        finally:
            self.lock.release()

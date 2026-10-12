# -*- coding: utf-8 -*-
"""交易新聞監測：啟動這個檔案即可（python server.py）。

只監聽本機 127.0.0.1，資料存在同資料夾的 news.db，設定存在 config.json。
"""
import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from config import ConfigError, market_list
from monitor import Monitor, log
from enrich import Enricher
from translate import Translator
import ai_digest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "static", "index.html")
MAX_BODY = 1024 * 1024
BUILD = "2026-10-12.1"   # 必須跟 static/index.html 裡的 BUILD 一致；網頁用它偵測「檔案只更新了一半」


def _one(qs, key, default=None):
    v = qs.get(key)
    return v[0] if v else default


def _int(v, default=0, lo=None, hi=None):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


SENT_VALUES = ("pos", "neg", "neutral")


def filters_from(qs):
    hours = _int(_one(qs, "hours"), 24, 0, 24 * 365)
    sent = _one(qs, "sent") or ""
    return {
        "market": _one(qs, "market") or None,
        "tag": _one(qs, "tag") or None,
        "q": (_one(qs, "q") or "").strip(),
        "min_imp": _int(_one(qs, "min_imp"), 1, 1, 5),
        "since": int(time.time()) - hours * 3600 if hours else 0,
        "unread": _one(qs, "unread") == "1",
        "starred": _one(qs, "starred") == "1",
        "source": _one(qs, "source") or None,
        "show_dups": _one(qs, "show_dups") == "1",
        "sent": sent if sent in SENT_VALUES else None,
        "important": _one(qs, "important") == "1",
        "sources": [s for s in (qs.get("sources") or []) if s] or None,
    }


def make_handler(monitor):
    store = monitor.store
    translator = Translator(monitor)
    enricher = Enricher(monitor)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingNewsMonitor/1.0"

        def log_message(self, *args):  # 不要在終端機洗版
            pass

        # -------------------------------------------------- 共用
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _err(self, code, msg):
            self._json({"error": msg}, code)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        # -------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return self._err(403, "forbidden")
            u = urlsplit(self.path)
            qs = parse_qs(u.query)
            path = u.path
            try:
                if path in ("/", "/index.html"):
                    with open(INDEX_PATH, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if path == "/api/articles":
                    res = store.query(filters_from(qs), sort=_one(qs, "sort", "time"),
                                      limit=_int(_one(qs, "limit"), 100, 1, 300),
                                      offset=_int(_one(qs, "offset"), 0, 0))
                    return self._json(res)
                if path == "/api/stats":
                    return self._json(self._stats(qs))
                if path == "/api/digest":
                    hours = _int(_one(qs, "hours"), 12, 1, 24 * 7)
                    since = int(time.time()) - hours * 3600
                    return self._json(store.digest(since))
                if path == "/api/focus_stocks":
                    hours = _int(_one(qs, "hours"), 18, 1, 24 * 7)
                    since = int(time.time()) - hours * 3600
                    cfg = monitor.cfg
                    wl_markets = {it["name"]: it["market"] for it in cfg["watchlist"]}
                    cands = store.focus_candidates(since, wl_markets, cfg["settings"]["focus_weights"])
                    return self._json({"candidates": cands})
                if path == "/api/config":
                    return self._json(monitor.cfg)
                self._err(404, "not found")
            except Exception as e:
                log("API 錯誤 %s：%s" % (path, e))
                self._err(500, str(e))

        def _stats(self, qs):
            hours = _int(_one(qs, "hours"), 24, 0, 24 * 365)   # 0 = 全部保留的新聞
            since = int(time.time()) - hours * 3600 if hours else 0
            cfg = monitor.cfg
            counts = store.counts(since)
            out = {
                "now": int(time.time()),
                "max_id": store.max_id(),
                "unread": counts["total"],
                "markets": [{"name": m, "unread": counts["markets"].get(m, 0)} for m in market_list(cfg)],
                "watchlist": [{"name": it["name"], "market": it["market"], "unread": counts["tags"].get(it["name"], 0)}
                              for it in cfg["watchlist"]],
                "sent_counts": counts["sent"],
                "important_count": counts["important"],
                "important_sources": store.important_source_counts(since),
                "sources": store.statuses(),
                "source_names": store.sources_in_use(since),
                "monitor": dict(monitor.state),
                "sentiment_hint": cfg["settings"]["sentiment_hint"],
                "build": BUILD,
            }
            after = _int(_one(qs, "after_id"), -1)
            if after >= 0:
                out["new"] = store.new_since(after, since)
            return out

        # -------------------------------------------------- POST
        def do_POST(self):
            if not self._host_ok():
                return self._err(403, "forbidden")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc.rsplit(":", 1)[0].strip("[]").lower() not in ("127.0.0.1", "localhost", "::1"):
                return self._err(403, "bad origin")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._err(415, "content-type must be application/json")
            n = _int(self.headers.get("Content-Length"), 0, 0)
            if n > MAX_BODY:
                return self._err(413, "too large")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._err(400, "invalid json")
            path = urlsplit(self.path).path
            try:
                if path == "/api/action":
                    return self._json(self._action(body))
                if path == "/api/translate":
                    ids = [i for i in (body.get("ids") or []) if isinstance(i, int)]
                    return self._json(translator.run(ids))
                if path == "/api/enrich":
                    ids = [i for i in (body.get("ids") or []) if isinstance(i, int)]
                    return self._json(enricher.run(ids))
                if path == "/api/refresh":
                    monitor.refresh_now()
                    return self._json({"ok": True})
                if path == "/api/ai_digest":
                    hours = _int(body.get("hours"), 12, 1, 24 * 7)
                    since = int(time.time()) - hours * 3600
                    d = store.digest(since)
                    ai_cfg = monitor.cfg["settings"]["ai_digest"]
                    try:
                        text = ai_digest.generate(d, hours, ai_cfg["api_key"], ai_cfg["model"])
                    except ai_digest.AiDigestError as e:
                        return self._err(400, str(e))
                    return self._json({"text": text})
                if path == "/api/config":
                    cfg = monitor.apply_config(body)
                    return self._json(cfg)
                self._err(404, "not found")
            except ConfigError as e:
                self._err(400, str(e))
            except Exception as e:
                log("API 錯誤 %s：%s" % (path, e))
                self._err(500, str(e))

        def _action(self, body):
            act = body.get("action")
            if act in ("read", "unread", "star", "unstar"):
                field = "starred" if act in ("star", "unstar") else "read"
                value = act in ("read", "star")
                return {"changed": store.set_flag(body.get("ids") or [], field, value)}
            if act == "read_all":
                f = body.get("filter") or {}
                sent = f.get("sent") or ""
                params = {
                    "market": f.get("market") or None, "tag": f.get("tag") or None,
                    "q": str(f.get("q") or ""), "min_imp": _int(f.get("min_imp"), 1, 1, 5),
                    "since": (int(time.time()) - _int(f.get("hours"), 0, 0, 24 * 365) * 3600) if f.get("hours") else 0,
                    "starred": bool(f.get("starred")), "source": f.get("source") or None,
                    "sent": sent if sent in SENT_VALUES else None,
                    "important": bool(f.get("important")),
                    "sources": [s for s in (f.get("sources") or []) if isinstance(s, str) and s] or None,
                }
                return {"changed": store.mark_all_read(params)}
            raise ValueError("unknown action")

    return Handler


def main():
    try:
        # line_buffering=True：有些 Windows 終端機會把輸出整批緩衝住，不到一定份量或程式結束不會顯示，
        # 導致視窗看起來像卡住，其實只是還沒印出來。開這個讓每行印出來就立刻顯示。
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="交易新聞監測")
    ap.add_argument("--no-browser", action="store_true", help="啟動時不要自動開啟瀏覽器")
    ap.add_argument("--port", type=int, help="覆寫連接埠")
    args = ap.parse_args()

    if sys.version_info < (3, 8):
        print("需要 Python 3.8 或更新版本（目前是 %s）。" % sys.version.split()[0], flush=True)
        sys.exit(1)

    print("正在啟動，請稍候…", flush=True)
    try:
        monitor = Monitor(BASE_DIR)
    except ConfigError as e:
        print("\n設定檔讀取失敗：\n%s\n" % e, flush=True)
        sys.exit(1)

    settings = monitor.cfg["settings"]
    port = args.port or settings["port"]
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(monitor))
    except OSError:
        print("\n連接埠 %d 已被佔用：可能已經開了另一個「交易新聞監測」視窗，請先關掉它；"
              "或用 python server.py --port 8800 換一個連接埠。\n" % port, flush=True)
        sys.exit(1)

    url = "http://127.0.0.1:%d/" % port
    print("=" * 56, flush=True)
    print("  交易新聞監測 已啟動", flush=True)
    print("  儀表板：%s" % url, flush=True)
    print("  關閉方式：關掉這個視窗，或按 Ctrl+C", flush=True)
    print("=" * 56, flush=True)
    monitor.start()
    if settings["open_browser"] and not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

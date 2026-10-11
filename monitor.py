# -*- coding: utf-8 -*-
"""背景排程：定時抓取所有來源 → 比對關注清單 → 評分 → 去重 → 存入資料庫。"""
import hashlib
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import ConfigError, flatten_noise, load_config, normalize, save_config
from fetcher import build_sources, describe_error, fetch_source, http_get, parse_source
from rules import Matcher, norm_title, validate_rules, extract_stock_codes
from store import Store, agg_sentiment, merge_signals, sj


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Monitor:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.cfg_path = os.path.join(base_dir, "config.json")
        self.cfg = load_config(self.cfg_path)
        self.matcher = Matcher(self.cfg)
        self.store = Store(os.path.join(base_dir, "news.db"))
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.state = {"running": False, "last_start": 0, "last_end": 0, "next_run": 0,
                      "last_summary": "", "cycles": 0, "rescoring": False}
        self._first_failures_logged = False

    # ------------------------------------------------------------ 設定

    def apply_config(self, raw_cfg):
        cfg = normalize(raw_cfg)
        bad = validate_rules(cfg["sector_rules"])
        if bad:
            raise ConfigError("「板塊影響規則」有 %d 行格式不對，沒有儲存：\n%s" % (
                len(bad), "\n".join("第 %d 行：%s\n    %s" % (i, why, text[:80]) for i, text, why in bad[:5])))
        save_config(self.cfg_path, cfg)
        with self.lock:
            self.cfg = cfg
            self.matcher = Matcher(cfg)
        self.start_rescore()
        self.refresh_now()
        return cfg

    # ------------------------------------------------------------ 重新評估既有新聞的多空標示

    def start_rescore(self):
        """在背景重算最近這段時間所有新聞的多空標示（改了規則或關鍵字後，舊新聞也會跟著更新）。"""
        threading.Thread(target=self._rescore_safe, name="rescore", daemon=True).start()

    def _rescore_safe(self):
        self.state["rescoring"] = True
        try:
            self.rescore()
        except Exception:
            log("重新評估多空標示時發生錯誤：\n" + traceback.format_exc())
        finally:
            self.state["rescoring"] = False

    @staticmethod
    def match_signature(cfg):
        """影響「新聞比對到哪些標的」的設定的指紋。變了就要把資料庫裡最近的新聞重新比對。"""
        data = {
            "wl": [(w["name"], w["keywords"], w.get("commodity"), w.get("direction"), w.get("market")) for w in cfg["watchlist"]],
            "noise": flatten_noise(cfg.get("noise_phrases")), "ctx": cfg.get("commodity_context"),
            "rules": cfg.get("sector_rules"), "pos": cfg.get("positive_words"), "neg": cfg.get("negative_words"),
            "neu": cfg.get("neutral_words"), "hint": cfg["settings"].get("sentiment_hint"),
        }
        return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def rescore(self):
        """重新比對最近保留期間內所有新聞的標的、市場、多空標示。

        - 現在已經比對不到任何標的、但原本是靠標的才收進來的新聞（例如標題只有「黃金周」的雜訊）會被刪掉。
          保護：有加星號的、來自「收錄全部」來源的、以及原本的標的已被你從清單刪除的，一律不刪，只更新標籤。
        - 標題符合「整篇文章直接排除」設定的（例如盤中速報）：不論來源設定，一律刪除；只有加星號才保留。
        - 同一事件的各則報導視為一組，一起判斷。"""
        with self.lock:
            cfg, matcher = self.cfg, self.matcher
        since = int(time.time()) - cfg["settings"]["keep_days"] * 86400
        rows = self.store.rows_for_rescore(since)
        names = {w["name"] for w in cfg["watchlist"]}
        market_of = {w["name"]: w.get("market", "") for w in cfg["watchlist"]}
        keep_all = {f["name"] for f in cfg["feeds"] if f.get("keep_all")}
        new_tags, sig, neu, excl, kwp, kwn, imp = {}, {}, {}, {}, {}, {}, {}
        for r in rows:
            excl[r["id"]] = matcher.excluded(r["title"])
            matched = [] if excl[r["id"]] else matcher.match(r["title"] + " " + (r["summary"] or ""))
            new_tags[r["id"]] = [it["name"] for it in matched]
            _b, _s, n = matcher.score(r["title"], r["summary"] or "")
            neu[r["id"]] = bool(n)
            sig[r["id"]] = matcher.signals(r["title"], new_tags[r["id"]])
            # 多方／空方新聞蒐集用的純文字比對，跟上面 signals（單則新聞的利多／利空標籤）是分開算的。
            p_words, n_words = matcher.pos_neg_words(r["title"])
            kwp[r["id"]], kwn[r["id"]] = bool(p_words), bool(n_words)
            # 重要消息來源：純粹看來源名稱，跟標題內容、關鍵字比對都無關，所以改了「重要消息來源」
            # 清單後，既有新聞也能跟著重新標記（不用等下一次抓取）。
            imp[r["id"]] = matcher.source_important(r["source"])
        groups = {}
        for r in rows:
            groups.setdefault(r["dup_of"] or r["id"], []).append(r)
        updates, delete_ids = [], []
        for root, mem in groups.items():
            old_union = []
            for r in mem:
                for t in json.loads(r["tags"] or "[]"):
                    if t not in old_union:
                        old_union.append(t)
            new_union = []
            for r in mem:
                for t in new_tags[r["id"]]:
                    if t not in new_union:
                        new_union.append(t)
            protected = any(r["starred"] or r["source"] in keep_all or matcher.source_important(r["source"])
                            for r in mem)
            # 盤中速報這類「整篇排除」的雜訊：只要有加星號就保留，不看來源是否「收錄全部」
            # （跟下面「找不到標的」的清理不同——那類是標的變了才清，這類是文章本身就是雜訊）。
            if any(excl[r["id"]] for r in mem) and not any(r["starred"] for r in mem):
                delete_ids += [r["id"] for r in mem]
                continue
            if old_union and not new_union and all(t in names for t in old_union) and not protected:
                delete_ids += [r["id"] for r in mem]
                continue
            merged_sig, merged_neu, merged_kwp, merged_kwn, merged_imp = [], False, False, False, False
            for r in mem:
                merged_sig = merge_signals(merged_sig, sig[r["id"]])
                merged_neu = merged_neu or neu[r["id"]]
                merged_kwp = merged_kwp or kwp[r["id"]]
                merged_kwn = merged_kwn or kwn[r["id"]]
                merged_imp = merged_imp or imp[r["id"]]
            for r in mem:
                if r["id"] == root:                                # 代表那一則：顯示整組的聯集
                    tags = new_union
                    mk = []
                    for t in tags:
                        if market_of.get(t) and market_of[t] not in mk:
                            mk.append(market_of[t])
                    markets_json = json.dumps(mk, ensure_ascii=False) if mk else (r["markets"] or "[]")
                    updates.append((r["id"], json.dumps(tags, ensure_ascii=False), markets_json,
                                    agg_sentiment(merged_sig), merged_neu, sj(merged_sig), merged_kwp, merged_kwn, merged_imp))
                else:
                    updates.append((r["id"], json.dumps(new_tags[r["id"]], ensure_ascii=False), r["markets"] or "[]",
                                    agg_sentiment(sig[r["id"]]), neu[r["id"]], sj(sig[r["id"]]), kwp[r["id"]], kwn[r["id"]], imp[r["id"]]))
        self.store.apply_retag(updates, delete_ids)
        self.store.set_meta("match_sig", self.match_signature(cfg))
        log("已重新比對 %d 則新聞（清掉 %d 則不相關的雜訊）" % (len(rows), len(delete_ids)))

    # ------------------------------------------------------------ 排程

    def start(self):
        threading.Thread(target=self._loop, name="monitor", daemon=True).start()
        if self.store.has_unscored() or self.store.get_meta("match_sig") != self.match_signature(self.cfg):
            self.start_rescore()

    def refresh_now(self):
        self.wake.set()

    def _loop(self):
        while True:
            try:
                self.run_cycle()
            except Exception:
                log("更新週期發生未預期的錯誤：\n" + traceback.format_exc())
                self.state["running"] = False
            interval = self.cfg["settings"]["refresh_minutes"] * 60
            self.state["next_run"] = int(time.time()) + interval
            self.wake.wait(interval)
            self.wake.clear()

    # ------------------------------------------------------------ 一個更新週期

    def _fetch_one(self, src, settings):
        try:
            return src, fetch_source(src, settings), None
        except Exception as e:  # 單一來源失敗不影響其他來源
            return src, None, describe_error(e)

    def _fetch_one_cached(self, src, settings, url_cache):
        """跟 _fetch_one 一樣，但同一輪次裡，同一個網址（例如同一頁面設了兩種解析方式）
        只會真的發出一次請求；同時有別的來源在等同一個網址時會等它做完，不會各發各的。"""
        url = src["url"]
        with url_cache["lock"]:
            per_url_lock = url_cache["locks"].setdefault(url, threading.Lock())
        with per_url_lock:
            if url not in url_cache["raw"]:
                ua = src.get("user_agent") or settings["user_agent"]
                try:
                    data = http_get(url, ua, settings["request_timeout"], settings.get("verify_ssl", True))
                    url_cache["raw"][url] = (data, None)
                except Exception as e:
                    url_cache["raw"][url] = (None, e)
            data, err = url_cache["raw"][url]
        if err is not None:
            return src, None, describe_error(err)
        try:
            return src, parse_source(data, src), None
        except Exception as e:
            return src, None, describe_error(e)

    def _prepare(self, src, raw_items, cfg, matcher, now):
        st = cfg["settings"]
        cutoff = now - st["keep_days"] * 86400
        out = []
        ranked = sorted(raw_items, key=lambda x: -x["published"])[: st["max_items_per_source"]]
        for r in ranked:
            published = r["published"] or now
            if published > now + 600:
                published = now
            if published < cutoff:
                continue
            if matcher.excluded(r["title"]):     # 盤中速報這類機械式雜訊：整篇不收，無論來源是否「收錄全部」
                continue
            if matcher.source_excluded(r["source"]):   # 娛樂／廣告業配這類來源：整篇不收，無論標題是否命中財經詞
                continue
            matched = matcher.match(r["title"] + " " + r["summary"])
            tags = [m["name"] for m in matched]
            # 板塊規則（例如被動元件）不需要先比對到關注標的也能成立，所以「算不算相關」要連規則
            # 命中的結果一起看，不能只看有沒有比對到標的——否則規則命中、但沒有對應標的的新聞，
            # 在沒開「收錄全部」的來源上會被提早濾掉，永遠沒機會被標示。
            signals = matcher.signals(r["title"], tags)
            # 重要消息來源（經濟日報／工商時報／華爾街見聞／東方財富網，設定 → 一般設定可以增減）：
            # 不管有沒有比對到關注標的、板塊規則，也不管主動搜尋的財經情境詞有沒有命中，一律收進來、
            # 並標記為 important，不受前面那套多方／空方／中性關鍵詞比對機制限制。用來源名稱判斷
            # （而不是來源設定裡的旗標），這樣既有安裝不用重新設定來源就能套用新的預設清單。
            important = matcher.source_important(r["source"])
            if not matched and not signals:
                if not src.get("keep_all") and not important:
                    continue
                # 主動搜尋來源：關鍵字本身可能太通用（例如「首度」），標題還要有財經相關的字
                # 或股票代碼才留下，不然會洗進體育、娛樂這類無關新聞。重要消息來源不受這層限制。
                if src.get("finance_context_required") and not important:
                    if not matcher.has_finance_context(r["title"]) and not extract_stock_codes(r["title"]):
                        continue
            markets = []
            for m in matched:
                if m["market"] and m["market"] not in markets:
                    markets.append(m["market"])
            if not markets and src.get("market"):
                # 重要消息來源裡本來沒開「收錄全部」的（例如東方財富焦點新聞，量大且多是跟關注標的
                # 無關的中國內需股新聞），沒比對到任何標的時不要掛上大範圍市場標籤（例如「總經」），
                # 不然會洗版到一般的市場瀏覽畫面——這篇還是會收進來，只是只出現在「重要消息來源」集錦。
                if not (important and not src.get("keep_all")):
                    markets = [src["market"]]
            boost = max([m["boost"] for m in matched] or [0])
            base, _generic, neutral = matcher.score(r["title"], r["summary"], bool(src.get("official")), boost)
            sent = agg_sentiment(signals)
            out.append({
                "uid": r["uid"], "title": r["title"], "norm": norm_title(r["title"]),
                "url": r["url"], "source": r["source"], "summary": r["summary"],
                "published": published, "tags": tags, "markets": markets,
                "base_imp": base, "sentiment": sent, "neutral": neutral, "signals": signals,
                "official": bool(src.get("official")), "important": important,
            })
        return out

    def run_cycle(self):
        with self.lock:
            cfg, matcher = self.cfg, self.matcher
        sources = build_sources(cfg)
        self.state.update(running=True, last_start=int(time.time()))
        t0 = time.time()
        totals = {"new": 0, "dup": 0}
        failed = []

        url_cache = {"lock": threading.Lock(), "locks": {}, "raw": {}}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = [ex.submit(self._fetch_one_cached, s, cfg["settings"], url_cache) for s in sources]
            for fut in as_completed(futures):
                src, items, err = fut.result()
                if err:
                    failed.append((src["name"], err))
                    self.store.set_status(src["name"], src["url"], False, 0, err)
                    continue
                try:
                    prepared = self._prepare(src, items, cfg, matcher, int(time.time()))
                    res = self.store.ingest(prepared, cfg["settings"]["dedup_similarity"], matcher)
                except Exception as e:
                    err = "處理失敗：%s" % e
                    failed.append((src["name"], err))
                    self.store.set_status(src["name"], src["url"], False, 0, err)
                    continue
                totals["new"] += res["new"]
                totals["dup"] += res["dup"]
                self.store.set_status(src["name"], src["url"], True, res["new"] + res["dup"], "")

        self.store.prune_status({s["name"] for s in sources})
        self.store.purge(cfg["settings"]["keep_days"])

        summary = "%d 個來源，新增 %d 則（另有 %d 則與既有新聞重複），失敗 %d 個，耗時 %.1f 秒" % (
            len(sources), totals["new"], totals["dup"], len(failed), time.time() - t0)
        self.state.update(running=False, last_end=int(time.time()), last_summary=summary,
                          cycles=self.state["cycles"] + 1)
        log("更新完成：" + summary)
        if failed and not self._first_failures_logged:
            self._first_failures_logged = True
            log("以下來源抓取失敗（不影響其他來源；可在網頁「來源狀態」查看，或在設定中停用）：")
            for name, err in failed:
                log("  - %s：%s" % (name, err))

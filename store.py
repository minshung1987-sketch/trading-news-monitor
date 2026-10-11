# -*- coding: utf-8 -*-
"""SQLite 儲存：新聞、去重（相似標題歸為同一則）、查詢、來源狀態。"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from rules import bigrams, cluster_bonus, is_similar, number_set

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  norm_title TEXT NOT NULL,
  url TEXT NOT NULL,
  source TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  published INTEGER NOT NULL,
  fetched INTEGER NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  markets TEXT NOT NULL DEFAULT '[]',
  base_imp INTEGER NOT NULL DEFAULT 1,
  importance INTEGER NOT NULL DEFAULT 1,
  sentiment INTEGER NOT NULL DEFAULT 0,
  neutral INTEGER NOT NULL DEFAULT 0,
  kw_pos INTEGER NOT NULL DEFAULT 0,
  kw_neg INTEGER NOT NULL DEFAULT 0,
  important INTEGER NOT NULL DEFAULT 0,
  title_zh TEXT,
  real_url TEXT,
  images TEXT,
  signals TEXT,
  official INTEGER NOT NULL DEFAULT 0,
  dup_of INTEGER,
  read INTEGER NOT NULL DEFAULT 0,
  starred INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pub ON articles(published);
CREATE INDEX IF NOT EXISTS idx_dup ON articles(dup_of);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS source_status(
  name TEXT PRIMARY KEY,
  url TEXT,
  ok INTEGER,
  last_try INTEGER,
  last_ok INTEGER,
  error TEXT,
  last_count INTEGER,
  total INTEGER NOT NULL DEFAULT 0
);
"""


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def sj(signals):
    """多空標示存成精簡 JSON（篩選時用字串比對 "d":1 / "d":-1，所以格式要固定）。"""
    return json.dumps(signals or [], ensure_ascii=False, separators=(",", ":"))


def merge_signals(a, b):
    """兩組標示取聯集；同一對象一邊利多、一邊利空就抵銷掉。"""
    seen, out = set(), []
    for x in list(a or []) + list(b or []):
        k = (x["s"], x["d"])
        if k not in seen:
            seen.add(k)
            out.append(x)
    conflict = {x["s"] for x in out if any(y["s"] == x["s"] and y["d"] != x["d"] for y in out)}
    return [x for x in out if x["s"] not in conflict]


def agg_sentiment(signals):
    v = sum(x["d"] for x in signals or [])
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _union(a, b):
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self._tx() as c:
            c.executescript(SCHEMA)
        # 舊版資料庫沒有 neutral 欄位；CREATE TABLE IF NOT EXISTS 不會幫既有的表補欄位，
        # 所以既有安裝（已經有 news.db）要另外補上，新安裝則會被下面這行忽略。
        try:
            with self._tx() as c:
                c.execute("ALTER TABLE articles ADD COLUMN neutral INTEGER NOT NULL DEFAULT 0")
        except sqlite3.Error:
            pass
        try:
            with self._tx() as c:
                c.execute("ALTER TABLE articles ADD COLUMN title_zh TEXT")
        except sqlite3.Error:
            pass
        for col in ("real_url TEXT", "images TEXT", "signals TEXT",
                    "kw_pos INTEGER NOT NULL DEFAULT 0", "kw_neg INTEGER NOT NULL DEFAULT 0",
                    "important INTEGER NOT NULL DEFAULT 0"):
            try:
                with self._tx() as c:
                    c.execute("ALTER TABLE articles ADD COLUMN " + col)
            except sqlite3.Error:
                pass
        try:
            with self._tx() as c:
                c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass

    @contextmanager
    def _tx(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ------------------------------------------------------------ 寫入

    def ingest(self, items, threshold, matcher=None):
        """寫入一批新聞。每個 item 需含：
        uid, title, norm, url, source, summary, published, tags, markets, base_imp, sentiment, official
        matcher：目前的 Matcher，用來取得每則標題命中的多方／空方關鍵詞本身（去重判斷用，
        比整體加總的分數更準——加總容易被無關的詞稀釋打平）。沒給的話退回不比對這項（相容用）。
        回傳 {"new": 新增的獨立新聞數, "dup": 併入既有新聞的數量, "exists": 已存在數}
        """
        pn = matcher.pos_neg_words if matcher else (lambda t: (frozenset(), frozenset()))
        stats = {"new": 0, "dup": 0, "exists": 0}
        if not items:
            return stats
        now = int(time.time())
        with self.lock, self._tx() as c:
            recent = []
            for r in c.execute(
                    "SELECT id, title, norm_title, published, tags, markets, base_imp, sentiment, neutral, signals, kw_pos, kw_neg, important FROM articles "
                    "WHERE dup_of IS NULL AND published >= ?", (now - 5 * 86400,)):
                d = dict(r)
                d["tags"] = json.loads(d["tags"])
                d["markets"] = json.loads(d["markets"])
                d["signals"] = json.loads(d["signals"]) if d.get("signals") else []
                d["bg"] = bigrams(d["norm_title"])
                title = d.pop("title")
                d["nums"] = number_set(title)
                d["pos"], d["neg"] = pn(title)
                recent.append(d)

            for it in items:
                if c.execute("SELECT 1 FROM articles WHERE uid=?", (it["uid"],)).fetchone():
                    stats["exists"] += 1
                    continue
                bg = bigrams(it["norm"])
                nums = number_set(it["title"])
                ipos, ineg = pn(it["title"])
                primary = None
                for r in recent:
                    if abs(r["published"] - it["published"]) <= 3 * 86400 and \
                            is_similar(r["norm_title"], r["bg"], it["norm"], bg, threshold,
                               r["nums"], nums, r["pos"], r["neg"], ipos, ineg):
                        primary = r
                        break

                important = int(bool(it.get("important")))
                cur = c.execute(
                    "INSERT INTO articles(uid,title,norm_title,url,source,summary,published,fetched,"
                    "tags,markets,base_imp,importance,sentiment,neutral,kw_pos,kw_neg,important,official,dup_of,signals) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["uid"], it["title"], it["norm"], it["url"], it["source"], it["summary"],
                     it["published"], now,
                     json.dumps(it["tags"], ensure_ascii=False),
                     json.dumps(it["markets"], ensure_ascii=False),
                     it["base_imp"], it["base_imp"], it["sentiment"], int(it["neutral"]), int(bool(ipos)), int(bool(ineg)),
                     important,
                     int(it["official"]),
                     primary["id"] if primary else None, sj(it["signals"]) if it.get("signals") is not None else None))
                new_id = cur.lastrowid

                if primary is None:
                    recent.append({"id": new_id, "norm_title": it["norm"], "published": it["published"],
                                   "tags": list(it["tags"]), "markets": list(it["markets"]),
                                   "base_imp": it["base_imp"], "bg": bg, "nums": nums,
                                   "pos": ipos, "neg": ineg,
                                   "sentiment": it["sentiment"], "neutral": int(it["neutral"]),
                                   "kw_pos": int(bool(ipos)), "kw_neg": int(bool(ineg)),
                                   "important": important,
                                   "signals": list(it.get("signals") or [])})
                    stats["new"] += 1
                else:
                    stats["dup"] += 1
                    if it["published"] < primary["published"]:
                        # 這則報導比原本的代表更早 → 讓最早的成為代表，並沿用已讀／星號狀態
                        old_id = primary["id"]
                        flags = c.execute("SELECT read, starred FROM articles WHERE id=?", (old_id,)).fetchone()
                        c.execute("UPDATE articles SET dup_of=? WHERE dup_of=? OR id=?", (new_id, old_id, old_id))
                        c.execute("UPDATE articles SET dup_of=NULL, read=?, starred=? WHERE id=?",
                                  (flags["read"], flags["starred"], new_id))
                        primary.update(id=new_id, norm_title=it["norm"], published=it["published"], bg=bg,
                                       nums=nums, pos=ipos, neg=ineg, sentiment=it["sentiment"])
                        # kw_pos／kw_neg 是純文字比對，跟哪一則是代表無關，叢集裡任何一則命中就算命中，
                        # 換代表也不用重算，下面的 OR 聚合已經涵蓋。
                    primary["tags"] = _union(primary["tags"], it["tags"])
                    primary["markets"] = _union(primary["markets"], it["markets"])
                    primary["base_imp"] = max(primary["base_imp"], it["base_imp"])
                    # 中性標籤、純文字多空比對都獨立於哪個是代表新聞：叢集裡只要有一則命中，就算命中。
                    primary["neutral"] = 1 if (primary.get("neutral") or it["neutral"]) else 0
                    primary["kw_pos"] = 1 if (primary.get("kw_pos") or ipos) else 0
                    primary["kw_neg"] = 1 if (primary.get("kw_neg") or ineg) else 0
                    # 重要消息來源：叢集裡只要有一則來自重要消息來源，整個叢集（代表這則）就算重要——
                    # 跟 kw_pos／kw_neg／neutral 同一種「只要有一則命中就算命中」邏輯。
                    primary["important"] = 1 if (primary.get("important") or important) else 0
                    # 有對象的多空標示：叢集裡各則的標示取聯集（同一對象一利多一利空則抵銷）——
                    # 這是給「單則新聞的利多／利空」標籤用的，跟上面 kw_pos／kw_neg（新聞蒐集用）是兩件事。
                    primary["signals"] = merge_signals(primary.get("signals"), it.get("signals"))
                    if primary["signals"]:
                        primary["sentiment"] = agg_sentiment(primary["signals"])
                    c.execute("UPDATE articles SET neutral=?, signals=?, sentiment=?, kw_pos=?, kw_neg=?, important=? WHERE id=?",
                              (primary["neutral"], sj(primary["signals"]), primary["sentiment"],
                               primary["kw_pos"], primary["kw_neg"], primary["important"], primary["id"]))
                    n_src = c.execute(
                        "SELECT COUNT(DISTINCT source) FROM articles WHERE id=? OR dup_of=?",
                        (primary["id"], primary["id"])).fetchone()[0]
                    imp = min(5, primary["base_imp"] + cluster_bonus(n_src))
                    c.execute("UPDATE articles SET tags=?, markets=?, base_imp=?, importance=? WHERE id=?",
                              (json.dumps(primary["tags"], ensure_ascii=False),
                               json.dumps(primary["markets"], ensure_ascii=False),
                               primary["base_imp"], imp, primary["id"]))
        return stats

    def purge(self, keep_days):
        cutoff = int(time.time()) - keep_days * 86400
        with self.lock, self._tx() as c:
            c.execute("DELETE FROM articles WHERE published < ? AND starred = 0", (cutoff,))
            c.execute("DELETE FROM articles WHERE dup_of IS NOT NULL "
                      "AND dup_of NOT IN (SELECT id FROM articles)")

    # ------------------------------------------------------------ 來源狀態

    def set_status(self, name, url, ok, count, error):
        now = int(time.time())
        with self.lock, self._tx() as c:
            row = c.execute("SELECT total FROM source_status WHERE name=?", (name,)).fetchone()
            total = (row["total"] if row else 0) + (count if ok else 0)
            if row is None:
                c.execute("INSERT INTO source_status(name,url,ok,last_try,last_ok,error,last_count,total) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (name, url, int(ok), now, now if ok else 0, error or "", count, total))
            elif ok:
                c.execute("UPDATE source_status SET url=?, ok=1, last_try=?, last_ok=?, error='', "
                          "last_count=?, total=? WHERE name=?", (url, now, now, count, total, name))
            else:
                c.execute("UPDATE source_status SET url=?, ok=0, last_try=?, error=? WHERE name=?",
                          (url, now, error or "", name))

    def prune_status(self, valid_names):
        with self.lock, self._tx() as c:
            rows = [r["name"] for r in c.execute("SELECT name FROM source_status")]
            for n in rows:
                if n not in valid_names:
                    c.execute("DELETE FROM source_status WHERE name=?", (n,))

    def statuses(self):
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT name, url, ok, last_try, last_ok, error, last_count, total "
                "FROM source_status ORDER BY ok ASC, name ASC")]

    # ------------------------------------------------------------ 查詢

    @staticmethod
    def _where(p):
        w, a = [], []
        if not p.get("show_dups"):
            w.append("dup_of IS NULL")
        if p.get("market"):
            w.append("instr(markets, ?) > 0")
            a.append(json.dumps(p["market"], ensure_ascii=False))
        if p.get("tag"):
            w.append("instr(tags, ?) > 0")
            a.append(json.dumps(p["tag"], ensure_ascii=False))
        if p.get("source"):
            w.append("source = ?")
            a.append(p["source"])
        if p.get("sources"):
            marks = ",".join("?" * len(p["sources"]))
            w.append("source IN (%s)" % marks)
            a += list(p["sources"])
        if p.get("min_imp", 1) > 1:
            w.append("importance >= ?")
            a.append(p["min_imp"])
        if p.get("since"):
            w.append("published >= ?")
            a.append(p["since"])
        if p.get("unread"):
            w.append("read = 0")
        if p.get("starred"):
            w.append("starred = 1")
        if p.get("important"):
            w.append("important = 1")
        # 多方／空方／中性新聞蒐集：純粹比對標題有沒有出現多方／空方／中性關鍵詞（kw_pos/kw_neg/neutral），
        # 跟單則新聞的「XX利多／XX利空」標籤（signals／sentiment，走關注標的與板塊規則）是兩回事，
        # 不互相影響——一則新聞可能標題有多方詞、但因為板塊規則的推論被標成別的方向的利空，反之亦然。
        if p.get("sent") == "pos":
            w.append("kw_pos = 1")
        elif p.get("sent") == "neg":
            w.append("kw_neg = 1")
        elif p.get("sent") == "neutral":
            w.append("neutral = 1")
        for term in (p.get("q") or "").split():
            like = _like(term)
            w.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\')")
            a += [like, like, like]
        return (" AND ".join(w) or "1=1"), a

    @staticmethod
    def _row(r):
        d = dict(r)
        d.pop("norm_title", None)
        d["tags"] = json.loads(d["tags"])
        d["markets"] = json.loads(d["markets"])
        d["read"] = bool(d["read"])
        d["starred"] = bool(d["starred"])
        d["official"] = bool(d["official"])
        d["neutral"] = bool(d["neutral"])
        d["important"] = bool(d.get("important"))
        d["images"] = json.loads(d["images"]) if d.get("images") is not None else None
        d["signals"] = json.loads(d["signals"]) if d.get("signals") is not None else None   # None = 還沒評估過
        return d

    def query(self, params, sort="time", limit=100, offset=0):
        where, args = self._where(params)
        order = "importance DESC, published DESC, id DESC" if sort == "importance" else "published DESC, id DESC"
        with self._tx() as c:
            total = c.execute("SELECT COUNT(*) FROM articles WHERE " + where, args).fetchone()[0]
            rows = [self._row(r) for r in c.execute(
                "SELECT id,title,title_zh,url,source,summary,published,tags,markets,importance,sentiment,neutral,important,images,signals,"
                "official,read,starred,dup_of FROM articles WHERE " + where +
                " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [limit, offset])]
            dups = {}
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                for d in c.execute(
                        "SELECT id, dup_of, title, url, source, published FROM articles "
                        "WHERE dup_of IN (%s) ORDER BY published ASC" % marks, ids):
                    dups.setdefault(d["dup_of"], []).append(
                        {"id": d["id"], "title": d["title"], "url": d["url"],
                         "source": d["source"], "published": d["published"]})
            for r in rows:
                r["dups"] = dups.get(r["id"], [])
        return {"items": rows, "total": total}

    def counts(self, since):
        """各市場、各標的的未讀數，以及多方／空方／中性新聞集錦、重要消息來源的未讀數。"""
        markets, tags = {}, {}
        total = 0
        sent = {"pos": 0, "neg": 0, "neutral": 0}
        important = 0
        with self._tx() as c:
            for r in c.execute("SELECT markets, tags, kw_pos, kw_neg, neutral, important FROM articles "
                               "WHERE dup_of IS NULL AND read = 0 AND published >= ?", (since,)):
                total += 1
                for m in json.loads(r["markets"]):
                    markets[m] = markets.get(m, 0) + 1
                for t in json.loads(r["tags"]):
                    tags[t] = tags.get(t, 0) + 1
                if r["kw_pos"]:
                    sent["pos"] += 1
                if r["kw_neg"]:
                    sent["neg"] += 1
                if r["neutral"]:
                    sent["neutral"] += 1
                if r["important"]:
                    important += 1
        return {"total": total, "markets": markets, "tags": tags, "sent": sent, "important": important}

    def digest(self, since, top_n=15):
        """規則式摘要：重要度最高的幾則、按市場分組、多家來源都在報的事件。
        不是 AI 生成的文字摘要，是從既有資料篩選、分組後的精簡清單。"""
        with self._tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, title, source, url, published, markets, tags, importance, "
                "neutral, kw_pos, kw_neg, signals FROM articles "
                "WHERE dup_of IS NULL AND published >= ? ORDER BY importance DESC, published DESC",
                (since,))]
            for r in rows:
                r["markets"] = json.loads(r["markets"])
                r["tags"] = json.loads(r["tags"])
                r["signals"] = json.loads(r["signals"]) if r["signals"] else []
                n_src = c.execute("SELECT COUNT(DISTINCT source) FROM articles WHERE id=? OR dup_of=?",
                                  (r["id"], r["id"])).fetchone()[0]
                r["source_count"] = n_src

        stats = {"total": len(rows),
                 "pos": sum(1 for r in rows if r["kw_pos"]),
                 "neg": sum(1 for r in rows if r["kw_neg"]),
                 "neutral": sum(1 for r in rows if r["neutral"])}

        top_important = [r for r in rows if r["importance"] >= 3][:top_n]

        by_market = {}
        for r in rows:
            for m in r["markets"]:
                by_market.setdefault(m, []).append(r)
        for m in by_market:
            by_market[m] = sorted(by_market[m], key=lambda r: (-r["importance"], -r["published"]))[:8]

        cross_source = sorted([r for r in rows if r["source_count"] >= 2],
                              key=lambda r: (-r["source_count"], -r["importance"]))[:top_n]

        def strip(r):
            return {"id": r["id"], "title": r["title"], "source": r["source"], "url": r["url"],
                    "published": r["published"], "markets": r["markets"], "tags": r["tags"],
                    "importance": r["importance"], "signals": r["signals"], "source_count": r["source_count"]}

        return {"stats": stats,
                "top_important": [strip(r) for r in top_important],
                "by_market": {m: [strip(r) for r in lst] for m, lst in by_market.items()},
                "cross_source": [strip(r) for r in cross_source]}

    def focus_candidates(self, since, watchlist_markets, weights, limit=20):
        """盤前焦點股候選名單：不是預測，是把新聞關注度最集中的股票排出來給你自己判斷。
        watchlist_markets：{標的名稱: 市場}，用來排除政經人物、法人報告這類不是「股票」的標的。
        weights：{"source","importance","institution","net_signal"} 四個分數的權重。
        同時涵蓋：(1) 關注清單裡市場是台股／美股／加密貨幣的標的；(2) 標題裡用「名稱(4位數代碼)」
        格式寫出來、但不在關注清單裡的股票（見 rules.extract_stock_codes，規則比對，不是精確查證）。
        """
        from rules import extract_stock_codes
        stock_markets = ("台股", "美股", "加密貨幣")
        with self._tx() as c:
            # 故意不排除 dup_of（被合併的重複報導）：這裡要算的是「幾個不同來源在講同一檔股票」，
            # 被去重機制合併成一則的那幾篇，自己的 source／tags／kw_pos 等欄位還是完整保留的，
            # 拿掉 dup_of IS NULL 這個條件才不會漏算那些被合併掉的來源。
            rows = [dict(r) for r in c.execute(
                "SELECT title, source, importance, markets, tags, kw_pos, kw_neg FROM articles "
                "WHERE published >= ?", (since,))]
        subjects = {}
        for r in rows:
            tags = json.loads(r["tags"])
            markets = json.loads(r["markets"])
            is_institution_piece = "法人報告" in markets
            keys = [("code:" + code, name) for name, code in extract_stock_codes(r["title"])]
            keys += [("tag:" + t, t) for t in tags if watchlist_markets.get(t) in stock_markets]
            seen_here = set()
            for key, name in keys:
                if key in seen_here:   # 同一則新聞裡同一個標的只算一次
                    continue
                seen_here.add(key)
                d = subjects.setdefault(key, {"name": name, "sources": set(), "max_imp": 0,
                                              "kw_pos": 0, "kw_neg": 0, "institution_bullish": False, "titles": []})
                d["sources"].add(r["source"])
                d["max_imp"] = max(d["max_imp"], r["importance"])
                if r["kw_pos"]:
                    d["kw_pos"] += 1
                if r["kw_neg"]:
                    d["kw_neg"] += 1
                if is_institution_piece and r["kw_pos"]:
                    d["institution_bullish"] = True
                if len(d["titles"]) < 5:
                    d["titles"].append(r["title"])

        out = []
        for key, d in subjects.items():
            net = max(0, d["kw_pos"] - d["kw_neg"])
            score = (len(d["sources"]) * weights.get("source", 2) + d["max_imp"] * weights.get("importance", 3) +
                    (weights.get("institution", 10) if d["institution_bullish"] else 0) + net * weights.get("net_signal", 2))
            out.append({"key": key, "name": d["name"], "source_count": len(d["sources"]), "sources": sorted(d["sources"]),
                       "max_importance": d["max_imp"], "institution_bullish": d["institution_bullish"],
                       "kw_pos": d["kw_pos"], "kw_neg": d["kw_neg"], "titles": d["titles"], "score": score,
                       "tracked": key.startswith("tag:")})
        out.sort(key=lambda x: -x["score"])
        return out[:limit]

    def max_id(self):
        with self._tx() as c:
            return c.execute("SELECT COALESCE(MAX(id),0) FROM articles").fetchone()[0]

    def new_since(self, after_id, since):
        with self._tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, title, importance FROM articles WHERE dup_of IS NULL AND read = 0 AND id > ? AND published >= ? "
                "ORDER BY importance DESC, id DESC", (after_id, since))]
        return {
            "count": len(rows),
            "important": sum(1 for r in rows if r["importance"] >= 4),
            "top": [r for r in rows if r["importance"] >= 4][:3],
        }

    def sources_in_use(self, since):
        with self._tx() as c:
            return [r[0] for r in c.execute(
                "SELECT source FROM articles WHERE dup_of IS NULL AND published >= ? "
                "GROUP BY source ORDER BY COUNT(*) DESC LIMIT 60", (since,))]

    def important_source_counts(self, since):
        """重要消息來源集錦裡，各個實際來源名稱（例如「經濟日報（經 Google 新聞）」「東方財富（焦點新聞）」）
        各自的未讀數，用來做「只看這幾個來源」的子篩選（設定 → 一般設定的「重要消息來源」是用來比對
        關鍵字的粗略名稱，這裡回傳的是實際存在資料庫裡、可以直接拿來篩選的完整來源名稱）。"""
        with self._tx() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT source, COUNT(*) FROM articles WHERE dup_of IS NULL AND read = 0 "
                "AND important = 1 AND published >= ? GROUP BY source ORDER BY COUNT(*) DESC", (since,))}

    # ------------------------------------------------------------ 重新評估多空標示

    def has_unscored(self):
        with self._tx() as c:
            return c.execute("SELECT 1 FROM articles WHERE signals IS NULL LIMIT 1").fetchone() is not None

    def rows_for_rescore(self, since):
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, title, summary, tags, markets, dup_of, source, starred FROM articles WHERE published >= ?", (since,))]

    def get_meta(self, key):
        with self._tx() as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return r["v"] if r else None

    def set_meta(self, key, value):
        with self.lock, self._tx() as c:
            c.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    def apply_retag(self, updates, delete_ids):
        """updates: [(id, tags_json, markets_json, sentiment, neutral, signals_json, kw_pos, kw_neg, important)]；
        delete_ids: 要刪掉的文章。kw_pos／kw_neg 是新聞蒐集用的純文字比對，跟 sentiment／signals
        （單則新聞的利多／利空標籤）是分開的兩件事；important（重要消息來源）純粹看來源名稱，
        跟這兩者都無關。"""
        with self.lock, self._tx() as c:
            c.executemany("UPDATE articles SET tags=?, markets=?, sentiment=?, neutral=?, signals=?, kw_pos=?, kw_neg=?, important=? WHERE id=?",
                          [(t, m, sent, int(neu), sig, int(kwp), int(kwn), int(imp), i)
                           for i, t, m, sent, neu, sig, kwp, kwn, imp in updates])
            ids = [int(i) for i in delete_ids]
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                c.execute("DELETE FROM articles WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)
            c.execute("DELETE FROM articles WHERE dup_of IS NOT NULL AND dup_of NOT IN (SELECT id FROM articles)")

    def update_signals_batch(self, updates):
        """updates: [(id, sentiment, neutral, signals_json)]"""
        with self.lock, self._tx() as c:
            c.executemany("UPDATE articles SET sentiment=?, neutral=?, signals=? WHERE id=?",
                          [(sent, int(neu), sig, i) for i, sent, neu, sig in updates])

    # ------------------------------------------------------------ 翻譯

    def get_titles(self, ids):
        ids = [int(i) for i in ids][:200]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, title, title_zh FROM articles WHERE id IN (%s)" % marks, ids)]

    def set_translation(self, art_id, zh):
        with self.lock, self._tx() as c:
            c.execute("UPDATE articles SET title_zh=? WHERE id=?", (zh, int(art_id)))

    # ------------------------------------------------------------ 圖片預覽（抓文章頁）

    def get_for_enrich(self, ids):
        ids = [int(i) for i in ids][:200]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, title, url, source, summary, real_url, images FROM articles WHERE id IN (%s)" % marks, ids)]

    def set_enrichment(self, art_id, real_url, images_json, summary):
        """images_json 是 JSON 字串（空清單 "[]" 代表「處理過、沒有圖」）；摘要只在原本是空的時候才補。"""
        with self.lock, self._tx() as c:
            c.execute("UPDATE articles SET real_url=COALESCE(?, real_url), images=?, "
                      "summary=CASE WHEN summary='' THEN ? ELSE summary END WHERE id=?",
                      (real_url, images_json, summary or "", int(art_id)))

    # ------------------------------------------------------------ 使用者操作

    def set_flag(self, ids, field, value):
        if field not in ("read", "starred") or not ids:
            return 0
        ids = [int(i) for i in ids][:500]
        marks = ",".join("?" * len(ids))
        with self.lock, self._tx() as c:
            cur = c.execute("UPDATE articles SET %s=? WHERE id IN (%s)" % (field, marks), [int(value)] + ids)
            return cur.rowcount

    def mark_all_read(self, params):
        p = dict(params)
        p["unread"] = True
        where, args = self._where(p)
        with self.lock, self._tx() as c:
            cur = c.execute("UPDATE articles SET read=1 WHERE " + where, args)
            return cur.rowcount

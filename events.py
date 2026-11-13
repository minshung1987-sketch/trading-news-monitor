# -*- coding: utf-8 -*-
"""事件追蹤：把同一個事件的所有報導收成一張卡（時間軸、關注標的、筆記、AI 整理的重點）。

- 事件存在 news.db（不是 config.json），所以改設定、更新程式都不會動到它。
- 事件的「關鍵詞規則」每行一條、符合任何一條就收進事件；語法跟「重要度規則」一樣，只是沒有 => 權重。
- 收進事件的新聞不會被「保留天數」清掉（跟加星號的一樣），事件的時間軸才不會被截斷。
- 這個模組只管資料；抓取、比對、呼叫 AI 的串接在 monitor.py / server.py。
"""
import json
import re
import threading
import time
import urllib.parse

from fetcher import GN_EDITIONS
from rules import ARROW, _group_hit, _word_group, split_words
from zhnorm import to_simp

MAX_NAME = 60
MAX_RULE_LINES = 20
MAX_RULE_LEN = 240
MAX_NOTE = 4000
TIMELINE_CAP = 300          # 一個事件最多回傳幾組（相同報導合併後）新聞
AI_INPUT_CAP = 80           # 送給 AI 的新聞組數上限


class EventError(Exception):
    """給使用者看的白話錯誤訊息（網頁會直接顯示）。"""


# ---------------------------------------------------------------- 規則

def parse_event_line(line):
    """解析一行事件規則：甲組詞,甲組詞 + 乙組詞,乙組詞 ! 排除詞,排除詞
    空行與 # 開頭的註解回傳 None；格式不對丟 ValueError（白話訊息）。"""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if ARROW.search(line):
        raise ValueError("事件規則不需要「=>」，只要列出詞就好")
    excl = []
    left = line
    if "!" in left or "！" in left:
        left, ex = re.split(r"[!！]", left, maxsplit=1)
        excl = split_words(ex)
        if not excl:
            raise ValueError("「!」後面要接不想要的詞（或把「!」拿掉）")
    groups = [split_words(g) for g in re.split(r"[+＋]", left)]
    if len(groups) > 2 or not groups or not all(groups):
        raise ValueError("條件要寫成「甲組詞 + 乙組詞」（用 + 分開，各自用逗號列出詞）；只有一組詞就是出現任一個詞就成立")
    topics, triggers = (groups[0], groups[1]) if len(groups) == 2 else (groups[0], [])
    for w in topics + triggers + excl:
        if not w.startswith("@") and len(w) < 2:
            raise ValueError("「%s」太短，會收進一堆不相關的新聞，每個詞至少兩個字" % w)
    return {"topics": topics, "triggers": triggers, "excludes": excl}


def clean_rules(text):
    """整理使用者輸入的規則文字並驗證；回傳整理後的文字（保留註解行，去掉空行）。錯誤丟 EventError。"""
    lines = [ln.strip() for ln in str(text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) > MAX_RULE_LINES:
        raise EventError("規則最多 %d 行" % MAX_RULE_LINES)
    real = 0
    for i, ln in enumerate(lines, 1):
        if len(ln) > MAX_RULE_LEN:
            raise EventError("第 %d 行太長（上限 %d 字）" % (i, MAX_RULE_LEN))
        try:
            if parse_event_line(ln):
                real += 1
        except ValueError as e:
            raise EventError("第 %d 行：%s\n    %s" % (i, e, ln[:80]))
    if not real:
        raise EventError("至少要有一條規則（例如：黃仁勳,黃樂 + 調查,董事會）")
    return "\n".join(lines)


def compile_rules(text):
    out = []
    for ln in str(text or "").split("\n"):
        r = parse_event_line(ln)
        if not r:
            continue
        out.append({"topics": r["topics"], "triggers": r["triggers"], "excludes": r["excludes"],
                    "g_topics": _word_group(r["topics"]),
                    "g_triggers": _word_group(r["triggers"]) if r["triggers"] else None,
                    "g_excludes": _word_group(r["excludes"]) if r["excludes"] else None})
    return out


def rules_hit(rules, text, names=(), markets=()):
    """text 要先轉成簡體（to_simp）。標的／市場名稱讓 @標的 這種寫法可以用。"""
    for r in rules:
        if not _group_hit(r["g_topics"], text, names, markets):
            continue
        if r["g_triggers"] and not _group_hit(r["g_triggers"], text, names, markets):
            continue
        if r["g_excludes"] and _group_hit(r["g_excludes"], text, names, markets):
            continue
        return True
    return False


def _search_url(rule, edition, when):
    def grp(words):
        terms = ['"%s"' % w.replace('"', "") for w in words[:6] if not w.startswith("@")]
        return "(" + " OR ".join(terms) + ")" if terms else ""
    q = " ".join(g for g in (grp(rule["topics"]), grp(rule["triggers"])) if g)
    if not q:
        return None
    for ex in rule["excludes"][:6]:
        if not ex.startswith("@"):
            q += ' -"%s"' % ex.replace('"', "")
    q += " when:" + when
    hl, gl, ceid = GN_EDITIONS[edition]
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid})


# ---------------------------------------------------------------- AI 結果整理

_STATUS = ("confirmed", "unverified")


def clean_ai(obj, keep=None):
    """把 AI 回傳（或使用者編輯後送回來）的內容整理成固定格式；格式不對的項目直接丟掉。"""
    if not isinstance(obj, dict):
        raise EventError("AI 內容格式不對")

    def txt(v, n):
        return str(v or "").strip()[:n]
    points = []
    for p in (obj.get("points") or [])[:12]:
        if isinstance(p, dict) and txt(p.get("text"), 300):
            points.append({"status": "confirmed" if p.get("status") == "confirmed" else "unverified",
                           "text": txt(p.get("text"), 300)})
    timeline = []
    for t in (obj.get("timeline") or [])[:30]:
        if isinstance(t, dict) and txt(t.get("text"), 300):
            timeline.append({"date": txt(t.get("date"), 16), "text": txt(t.get("text"), 300)})
    out = {"summary": txt(obj.get("summary"), 300), "points": points, "timeline": timeline}
    for k in ("n_news", "model", "ts"):
        src = keep if keep is not None else obj
        if src.get(k) is not None:
            out[k] = src[k]
    return out


# ---------------------------------------------------------------- 主類別

def _chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class EventTracker:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self._active = {}
        self.reload()

    # ---------------- 目前啟用中的事件（給抓取流程用）

    def reload(self):
        with self.store._tx() as c:
            rows = c.execute("SELECT id, name, rules FROM events WHERE archived = 0").fetchall()
        active = {}
        for r in rows:
            try:
                active[r["id"]] = {"name": r["name"], "rules": compile_rules(r["rules"])}
            except ValueError:
                continue          # 存進去前都驗證過；萬一檔案被手動改壞，就當作這個事件暫停
        with self.lock:
            self._active = active

    def hit(self, title, summary="", tags=(), markets=()):
        """這則新聞符合哪些啟用中的事件；回傳事件 id 清單。"""
        with self.lock:
            active = dict(self._active)
        if not active:
            return []
        text = to_simp((title or "") + " " + (summary or ""))
        return [eid for eid, ev in active.items() if rules_hit(ev["rules"], text, tags, markets)]

    def search_sources(self, keep_days=14):
        """每個啟用中事件的每條規則，各產生一個 Google 新聞搜尋來源（含來源名稱 → 事件 id，抓回來後
        還會再用規則過濾一次，Google 回傳的模糊結果不會直接收進去）。"""
        with self.lock:
            active = dict(self._active)
        when = "%dd" % max(1, min(30, int(keep_days)))
        out, used = [], set()
        for eid, ev in active.items():
            for i, rule in enumerate(ev["rules"], 1):
                words = rule["topics"] + rule["triggers"]
                eds = ["tw"] + (["us"] if any(w.isascii() and any(ch.isalpha() for ch in w) and not w.startswith("@")
                                           for w in words) else [])
                for ed in eds:
                    url = _search_url(rule, ed, when)
                    if not url:
                        continue
                    name = "事件追蹤：%s（%d%s）" % (ev["name"], i, "" if ed == "tw" else "·US")
                    if name in used:                  # 兩個事件取了同名：加上 id 區分，來源狀態才不會混在一起
                        name = name[:-1] + " #%d）" % eid
                    used.add(name)
                    out.append({"name": name, "url": url, "type": "rss", "market": "", "keep_all": True,
                                "official": False, "google": True, "event_id": eid})
        return out

    # ---------------- 把資料庫裡的新聞歸進事件

    def sync(self, event_id=None):
        """把「上次掃描之後新進資料庫的新聞」比對一遍，符合的記進 event_articles。
        event_id 給定時只處理那個事件（新建或改規則後用）。"""
        with self.lock:
            targets = {eid: ev for eid, ev in self._active.items() if event_id in (None, eid)}
        if not targets:
            return 0
        added = 0
        for eid, ev in targets.items():
            with self.store._tx() as c:
                cur = c.execute("SELECT cursor FROM events WHERE id=?", (eid,)).fetchone()
                if cur is None:
                    continue
                cursor = cur["cursor"]
                top = c.execute("SELECT COALESCE(MAX(id),0) FROM articles").fetchone()[0]
                rows = c.execute("SELECT id, title, summary, tags, markets FROM articles WHERE id > ? AND id <= ?",
                                 (cursor, top)).fetchall()
            hits = []
            for r in rows:
                text = to_simp((r["title"] or "") + " " + (r["summary"] or ""))
                try:
                    names, mk = json.loads(r["tags"] or "[]"), json.loads(r["markets"] or "[]")
                except ValueError:
                    names, mk = [], []
                if rules_hit(ev["rules"], text, names, mk):
                    hits.append((eid, r["id"]))
            with self.store.lock, self.store._tx() as c:
                c.executemany("INSERT OR IGNORE INTO event_articles(event_id, article_id, excluded) VALUES (?,?,0)", hits)
                c.execute("UPDATE events SET cursor=? WHERE id=?", (top, eid))
            added += len(hits)
        return added

    # ---------------- 讀取（給網頁）

    def list_events(self, archived=False):
        with self.store._tx() as c:
            evs = c.execute("SELECT * FROM events WHERE archived=? ORDER BY updated DESC, id DESC",
                            (int(bool(archived)),)).fetchall()
            return [self._detail(c, e) for e in evs]

    def get_event(self, event_id):
        with self.store._tx() as c:
            e = c.execute("SELECT * FROM events WHERE id=?", (int(event_id),)).fetchone()
            return self._detail(c, e) if e else None

    @staticmethod
    def _art(r):
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["markets"] = json.loads(d["markets"] or "[]")
        for k in ("read", "starred", "official", "important"):
            d[k] = bool(d.get(k))
        return d

    def _clusters(self, c, event_id):
        """回傳 (組清單, 總報導則數)。每組 = 一個代表新聞 + 併入它的相同報導；依發布時間由舊到新。"""
        rows = c.execute("SELECT article_id, excluded FROM event_articles WHERE event_id=?", (event_id,)).fetchall()
        excluded = {r["article_id"] for r in rows if r["excluded"]}
        ids = [r["article_id"] for r in rows if not r["excluded"]]
        cols = ("id,title,title_zh,url,source,summary,published,tags,markets,importance,important,official,"
                "starred,read,dup_of")
        matched = []
        for ch in _chunks(ids):
            matched += c.execute("SELECT %s FROM articles WHERE id IN (%s)" % (cols, ",".join("?" * len(ch))), ch).fetchall()
        root_ids = {r["dup_of"] or r["id"] for r in matched}
        roots, dups = {}, {}
        for ch in _chunks(root_ids):
            marks = ",".join("?" * len(ch))
            for r in c.execute("SELECT %s FROM articles WHERE id IN (%s)" % (cols, marks), ch):
                roots[r["id"]] = self._art(r)
            for r in c.execute("SELECT %s FROM articles WHERE dup_of IN (%s) ORDER BY published ASC" % (cols, marks), ch):
                dups.setdefault(r["dup_of"], []).append(r)
        out, total = [], 0
        for rid, a in roots.items():
            members = dups.get(rid, [])
            # 叢集裡只要有一則被使用者移出過，整組都不顯示（之後併進來的相同報導也一樣）
            if rid in excluded or any(m["id"] in excluded for m in members):
                continue
            a["dups"] = [{"id": m["id"], "title": m["title"], "url": m["url"], "source": m["source"],
                          "published": m["published"]} for m in members]
            total += 1 + len(members)
            out.append(a)
        out.sort(key=lambda a: (a["published"], a["id"]))
        return out, total

    def _detail(self, c, e):
        clusters, total = self._clusters(c, e["id"])
        tag_count, markets, sources = {}, [], set()
        for a in clusters:
            for t in a["tags"]:
                tag_count[t] = tag_count.get(t, 0) + 1
            for m in a["markets"]:
                if m not in markets:
                    markets.append(m)
            sources.add(a["source"])
            sources.update(d["source"] for d in a["dups"])
        notes = [dict(r) for r in c.execute(
            "SELECT id, ts, text FROM event_notes WHERE event_id=? ORDER BY ts DESC, id DESC", (e["id"],))]
        ai = None
        if e["ai"]:
            try:
                ai = json.loads(e["ai"])
            except ValueError:
                ai = None
        return {
            "id": e["id"], "name": e["name"], "rules": e["rules"], "created": e["created"], "updated": e["updated"],
            "archived": bool(e["archived"]), "ai": ai, "ai_ts": e["ai_ts"], "notes": notes,
            "stats": {
                "news": len(clusters), "reports": total, "sources": len(sources),
                "tags": [t for t, _n in sorted(tag_count.items(), key=lambda kv: -kv[1])],
                "markets": markets,
                "max_importance": max([a["importance"] for a in clusters], default=0),
                "first": clusters[0]["published"] if clusters else 0,
                "last": clusters[-1]["published"] if clusters else 0,
                "truncated": max(0, len(clusters) - TIMELINE_CAP),
            },
            "timeline": clusters[-TIMELINE_CAP:],
        }

    # ---------------- 寫入（使用者操作）

    def _name(self, name):
        name = " ".join(str(name or "").split())
        if not name:
            raise EventError("請幫事件取個名字")
        if len(name) > MAX_NAME:
            raise EventError("名字太長（上限 %d 字）" % MAX_NAME)
        return name

    def create(self, name, rules):
        name, rules = self._name(name), clean_rules(rules)
        now = int(time.time())
        with self.store.lock, self.store._tx() as c:
            cur = c.execute("INSERT INTO events(name, rules, created, updated) VALUES (?,?,?,?)",
                            (name, rules, now, now))
            eid = cur.lastrowid
        self.reload()
        self.sync(eid)
        return eid

    def update(self, event_id, name, rules):
        name, rules = self._name(name), clean_rules(rules)
        with self.store._tx() as c:
            old = c.execute("SELECT rules FROM events WHERE id=?", (int(event_id),)).fetchone()
        if old is None:
            raise EventError("找不到這個事件（可能已被刪除）")
        changed = old["rules"] != rules
        with self.store.lock, self.store._tx() as c:
            c.execute("UPDATE events SET name=?, rules=?, updated=? WHERE id=?", (name, rules, int(time.time()), int(event_id)))
            if changed:      # 規則改了：自動收進來的重算，使用者手動移出的保留
                c.execute("DELETE FROM event_articles WHERE event_id=? AND excluded=0", (int(event_id),))
                c.execute("UPDATE events SET cursor=0 WHERE id=?", (int(event_id),))
        self.reload()
        if changed:
            self.sync(int(event_id))
        return changed

    def set_archived(self, event_id, archived):
        with self.store.lock, self.store._tx() as c:
            c.execute("UPDATE events SET archived=?, updated=? WHERE id=?",
                      (int(bool(archived)), int(time.time()), int(event_id)))
        self.reload()
        if not archived:
            self.sync(int(event_id))

    def delete(self, event_id):
        eid = int(event_id)
        with self.store.lock, self.store._tx() as c:
            c.execute("DELETE FROM event_articles WHERE event_id=?", (eid,))
            c.execute("DELETE FROM event_notes WHERE event_id=?", (eid,))
            c.execute("DELETE FROM events WHERE id=?", (eid,))
        self.reload()

    def add_note(self, event_id, text):
        text = str(text or "").strip()
        if not text:
            raise EventError("筆記不能是空的")
        if len(text) > MAX_NOTE:
            raise EventError("筆記太長（上限 %d 字）" % MAX_NOTE)
        with self.store.lock, self.store._tx() as c:
            if c.execute("SELECT 1 FROM events WHERE id=?", (int(event_id),)).fetchone() is None:
                raise EventError("找不到這個事件（可能已被刪除）")
            c.execute("INSERT INTO event_notes(event_id, ts, text) VALUES (?,?,?)", (int(event_id), int(time.time()), text))

    def delete_note(self, note_id):
        with self.store.lock, self.store._tx() as c:
            c.execute("DELETE FROM event_notes WHERE id=?", (int(note_id),))

    def exclude(self, event_id, article_id):
        """把某則新聞（連同併在一起的相同報導）移出事件。"""
        eid, aid = int(event_id), int(article_id)
        with self.store.lock, self.store._tx() as c:
            r = c.execute("SELECT id, dup_of FROM articles WHERE id=?", (aid,)).fetchone()
            if r is None:
                return
            root = r["dup_of"] or r["id"]
            ids = [root] + [x[0] for x in c.execute("SELECT id FROM articles WHERE dup_of=?", (root,))]
            c.executemany("INSERT OR REPLACE INTO event_articles(event_id, article_id, excluded) VALUES (?,?,1)",
                          [(eid, i) for i in ids])

    def save_ai(self, event_id, ai):
        with self.store._tx() as c:
            row = c.execute("SELECT ai FROM events WHERE id=?", (int(event_id),)).fetchone()
        if row is None:
            raise EventError("找不到這個事件（可能已被刪除）")
        keep = json.loads(row["ai"]) if row["ai"] else {}
        cleaned = clean_ai(ai, keep=keep)
        with self.store.lock, self.store._tx() as c:
            c.execute("UPDATE events SET ai=?, ai_ts=COALESCE(ai_ts, ?) WHERE id=?",
                      (json.dumps(cleaned, ensure_ascii=False), int(time.time()), int(event_id)))
        return cleaned

    def store_ai_result(self, event_id, ai, n_news, model):
        cleaned = clean_ai(ai)
        now = int(time.time())
        cleaned.update(n_news=n_news, model=model, ts=now)
        with self.store.lock, self.store._tx() as c:
            c.execute("UPDATE events SET ai=?, ai_ts=? WHERE id=?",
                      (json.dumps(cleaned, ensure_ascii=False), now, int(event_id)))
        return cleaned

    def ai_input(self, event_id):
        """給 AI 的原料：(事件名稱, [新聞組])；新聞太多時保留重要度高的，再依時間由舊到新排好。"""
        ev = self.get_event(event_id)
        if ev is None:
            raise EventError("找不到這個事件（可能已被刪除）")
        items = ev["timeline"]
        if not items:
            raise EventError("這個事件還沒有收到任何新聞，沒有東西可以整理。")
        if len(items) > AI_INPUT_CAP:
            items = sorted(items, key=lambda a: (-a["importance"], a["published"]))[:AI_INPUT_CAP]
            items.sort(key=lambda a: (a["published"], a["id"]))
        return ev["name"], items

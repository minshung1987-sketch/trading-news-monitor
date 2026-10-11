# -*- coding: utf-8 -*-
"""關鍵字比對、重要度評分、標題相似度（去重用）。"""
import difflib
import re
import unicodedata

from config import flatten_noise, item_keywords
from zhnorm import to_simp

# 港股代碼是 5 位數字（台股是 4 位數、美股不會用括號包數字），標題裡出現這個格式，
# 八成是在講某檔港股，用來輔助「法人報告」排除陸股／港股相關內容，不用使用者手動列出詞組。
_HK_CODE = re.compile(r"[（(]\d{5}[）)]")

# 台股代碼：公司名稱緊接著括號包 4 位數字（例如「台積電(2330)」），用來輔助「盤前焦點股」
# 辨認「不在關注清單裡」的股票。「2026年」這種年份寫法容易被誤判成代碼，額外濾掉。
_TW_CODE = re.compile(r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9&\-]{1,10})[（(](\d{4})[）)]")
_YEAR_SUFFIX = re.compile(r"(今年|去年|明年|全年|上年|本年|上季|本季|下季|去季|[季年月日度Q])$")


def extract_stock_codes(title):
    """從標題抓出「名稱(4位數代碼)」，回傳 [(名稱, 代碼), ...]。是輔助辨識用的簡單規則，
    不是真的查證過代碼存不存在，公司名稱部分也可能抓得不夠精準，僅供參考。"""
    out = []
    for name, code in _TW_CODE.findall(title):
        if code[:2] == "20" and _YEAR_SUFFIX.search(name):
            continue
        out.append((name, code))
    return out


def compile_kw(kw):
    """把關鍵字編成 regex。

    - 中文：直接子字串比對。
    - 英文／數字：要求前後不是英數字（避免 "ETH" 誤中 "method"、"2330" 誤中 "23301"）。
    - 全大寫的英文（BTC、CPI、NVDA）區分大小寫，其餘不分大小寫。
    """
    kw = to_simp((kw or "").strip())      # 簡繁統一：關鍵字與被比對的文字都轉成簡體字形再比
    if not kw:
        return None
    if kw.isascii():
        flags = 0 if (kw.upper() == kw and any(c.isalpha() for c in kw)) else re.I
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", flags)
    return re.compile(re.escape(kw), re.I)


_SUFFIX = re.compile(r"\s+[-–—|｜]\s+[^-–—|｜]{1,30}$")


def norm_title(title):
    """去掉媒體名稱後綴、標點與空白，用來比較標題是否相同。"""
    t = unicodedata.normalize("NFKC", title or "")
    t = _SUFFIX.sub("", t).lower()
    return re.sub(r"[\W_]+", "", t)


def bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)(\s?%)?")


def number_set(title):
    """標題中的數字（兩位數以上，或後面接 % 的個位數），用來避免把「數字不同的同型標題」誤合併。"""
    t = unicodedata.normalize("NFKC", title or "")
    out = set()
    for m in _NUM.finditer(t):
        n = m.group(1).replace(",", "")
        if len(n.replace(".", "")) >= 2 or m.group(2):
            out.add(n)
    return out


def is_similar(a, ba, b, bb, threshold, na=frozenset(), nb=frozenset(),
              posa=frozenset(), nega=frozenset(), posb=frozenset(), negb=frozenset()):
    """兩個已正規化的標題是否為同一則新聞。

    ba/bb：預先算好的 bigram 集合。na/nb：標題中的數字。
    posa/nega、posb/negb：兩則標題各自命中的多方／空方關鍵詞（詞的集合，不是加總分數——
    加總分數容易被無關的詞稀釋打平，例如兩則都提到「上漲」時，光看總分會看不出「買超」跟「賣超」
    這種具體差異）。寧可少合併也不要誤合併：漏看一則不同的新聞，比多看到一則重複的代價高。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 8:          # 太短的標題只接受完全相同
        return False
    # 一則獨有的多方詞、另一則獨有的空方詞（或反過來）→ 兩則在講不同方向的事，不是同一件
    if (posa - posb) and (negb - nega):
        return False
    if (nega - negb) and (posb - posa):
        return False
    if (na - nb) and (nb - na):          # 各自有對方沒有的數字（3000 張 vs 1200 張）→ 不是同一件事
        return False
    inter = len(ba & bb)
    if not inter:
        return False
    jac = inter / (len(ba) + len(bb) - inter)
    if jac >= 0.6:
        return True
    if jac < 0.3:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= threshold


def cluster_bonus(n_sources):
    """同一事件被越多家媒體報導，越可能重要。"""
    if n_sources >= 6:
        return 2
    if n_sources >= 3:
        return 1
    return 0


# ---------------------------------------------------------------- 板塊影響規則

_SPLIT = re.compile(r"[,，、]")
ARROW = re.compile(r"\s*(?:=>|->|→|＝>)\s*")


def split_words(text):
    return [w.strip() for w in _SPLIT.split(text or "") if w.strip()]


def parse_rule_line(line):
    """解析一行規則。格式：

        主題詞,主題詞 + 觸發詞,觸發詞 ! 排除詞,排除詞 => 能源股利多, 高科技利空

    - 標題要同時出現「任一主題詞」和「任一觸發詞」才成立；只寫一組詞（沒有 +）就是「出現任一個詞就成立」。
    - ! 後面的排除詞是選填：標題只要有其中一個，這條規則就不成立（用來排除「暫緩降息」這類否定說法）。
    - => 後面是影響：「對象」加上「利多」或「利空」，用逗號分隔。
    格式不對會丟出 ValueError（訊息是白話）。空行與 # 開頭的註解回傳 None。"""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = ARROW.split(line, maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("缺少「=>」與後面的影響（例如 => 能源股利多, 高科技利空）")
    left, right = parts
    excl = []
    if "!" in left or "！" in left:
        left, ex = re.split(r"[!！]", left, maxsplit=1)
        excl = split_words(ex)
    groups = [split_words(g) for g in re.split(r"[+＋]", left)]
    if len(groups) > 2 or not groups or not all(groups):
        raise ValueError("條件要寫成「主題詞 + 觸發詞」（用 + 分開，各自用逗號列出詞）")
    topics, triggers = (groups[0], groups[1]) if len(groups) == 2 else (groups[0], [])
    effects = []
    for e in split_words(right):
        m = re.match(r"^(.+?)\s*(利多|利空)$", e)
        if not m:
            raise ValueError("「%s」要以「利多」或「利空」結尾，例如 能源股利多" % e)
        effects.append((m.group(1).strip(), 1 if m.group(2) == "利多" else -1))
    if not effects:
        raise ValueError("沒有寫任何影響")
    return {"topics": topics, "triggers": triggers, "excludes": excl, "effects": effects}


def validate_rules(lines):
    """回傳 [(行號, 原文, 錯誤說明)]；全部正確就是空清單。"""
    bad = []
    for i, line in enumerate(lines or [], 1):
        try:
            parse_rule_line(line)
        except ValueError as e:
            bad.append((i, line, str(e)))
    return bad


def _compiled(words):
    return [p for p in (compile_kw(w) for w in words) if p]


class Matcher:
    def __init__(self, cfg):
        self.items = []
        for it in cfg.get("watchlist", []):
            pats = [p for p in (compile_kw(k) for k in item_keywords(it)) if p]
            if pats:
                self.items.append({
                    "name": it["name"],
                    "market": it.get("market", ""),
                    "boost": int(it.get("boost", 0) or 0),
                    "dir": bool(it.get("direction", True)),      # 是否用漲跌用字替這個標的標示利多／利空
                    "commodity": bool(it.get("commodity", False)),   # 商品類：標題要有行情用詞才算相關
                    "institution": bool(it.get("institution", False)),   # 法人報告類：標題要有報告用詞才算相關
                    "person": bool(it.get("person", False)),   # 容易撞名的政經人物：標題要有 Fed／貨幣政策相關詞才算相關
                    "pats": pats,
                })
        self.by_name = {it["name"]: it for it in self.items}
        self.noise = _compiled(flatten_noise(cfg.get("noise_phrases")))
        self.ctx = _compiled(cfg.get("commodity_context") or [])
        self.ictx = _compiled(cfg.get("institution_context") or [])
        self.iexcl = _compiled(cfg.get("institution_exclude") or [])
        self.pctx = _compiled(cfg.get("person_context") or [])
        self.fctx = _compiled(cfg.get("finance_context") or [])
        self.title_excl = _compiled(flatten_noise(cfg.get("title_exclude")))
        self.source_excl = _compiled(flatten_noise(cfg.get("source_exclude")))
        self.important_srcs = _compiled(cfg.get("important_sources") or [])
        self.imp = []
        for word, weight in (cfg.get("importance_words") or {}).items():
            p = compile_kw(word)
            if p:
                self.imp.append((p, int(weight)))
        self.sentiment = bool(cfg.get("settings", {}).get("sentiment_hint", True))
        self.pos = [p for p in (compile_kw(w) for w in cfg.get("positive_words", [])) if p]
        self.neg = [p for p in (compile_kw(w) for w in cfg.get("negative_words", [])) if p]
        # 保留「詞本身」（不只是編譯好的樣式），去重判斷要用：整體加總的分數容易被無關的詞稀釋成平手，
        # 但「這則有『買超』、那則有『賣超』」這種具體的詞不一樣，才是真正代表兩則在講不同的事。
        self.pos_words = [(w, p) for w, p in ((w, compile_kw(w)) for w in cfg.get("positive_words", [])) if p]
        self.neg_words = [(w, p) for w, p in ((w, compile_kw(w)) for w in cfg.get("negative_words", [])) if p]
        self.neu = [p for p in (compile_kw(w) for w in cfg.get("neutral_words", [])) if p]

        def _triggers(words):
            """板塊影響規則的觸發詞：「多方詞」「空方詞」是共用多方／空方關鍵詞的特殊寫法，
            會展開成目前的 self.pos／self.neg（跟著設定一起變動），其餘照字面編譯。"""
            out = []
            for w in words:
                if w in ("多方詞", "多方關鍵詞"):
                    out += self.pos
                elif w in ("空方詞", "空方關鍵詞"):
                    out += self.neg
                else:
                    p = compile_kw(w)
                    if p:
                        out.append(p)
            return out

        self.rules = []
        for line in cfg.get("sector_rules", []) or []:
            try:
                r = parse_rule_line(line)
            except ValueError:
                continue                                            # 壞掉的行略過（存檔時會擋下並提示）
            if r:
                self.rules.append({"topics": _compiled(r["topics"]), "triggers": _triggers(r["triggers"]),
                                   "excludes": _compiled(r["excludes"]), "effects": r["effects"]})

    def clean(self, text):
        """簡繁統一，並把「非商品用法」的詞組（黃金周、黃金交叉…）遮掉，之後所有比對都用這個結果。"""
        t = to_simp(text)
        for p in self.noise:
            t = p.sub(" ", t)
        return t

    def excluded(self, title):
        """整篇排除：標題符合任何一條「整篇文章直接排除」的寫法（例如盤中速報），不看內容直接丟掉。"""
        t = to_simp(title or "")
        return any(p.search(t) for p in self.title_excl)

    def source_excluded(self, source):
        """整篇排除：來源（媒體／出處名稱）符合「來源排除」清單裡任何一條，不看標題內容直接丟掉。
        用來擋掉娛樂、廣告／業配這類媒體——它們的標題常常剛好用到也被鏡射進財經情境詞的通用詞
        （「缺貨」「供不應求」…），光看標題擋不掉，只能從來源本身排除。"""
        t = to_simp(source or "")
        return any(p.search(t) for p in self.source_excl)

    def source_important(self, source):
        """來源（媒體／出處名稱）符合「重要消息來源」清單裡任何一條：這則新聞不管有沒有比對到
        關注標的、板塊規則，也不管多方／空方／中性關鍵詞有沒有命中，都要收進來，並標記為重要。"""
        t = to_simp(source or "")
        return any(p.search(t) for p in self.important_srcs)

    def match(self, text):
        t = self.clean(text)
        out = []
        for it in self.items:
            if not any(p.search(t) for p in it["pats"]):
                continue
            if it["commodity"] and self.ctx and not any(p.search(t) for p in self.ctx):
                continue                          # 商品類標的：沒有行情用詞就當作是別的意思
            if it["institution"] and self.ictx and not any(p.search(t) for p in self.ictx):
                continue                          # 法人報告類標的：沒有目標價／評等／報告這類詞，八成是持股揭露或機構自己的消息，不是評論
            if it["institution"] and (any(p.search(t) for p in self.iexcl) or _HK_CODE.search(t)):
                continue                          # 陸股／港股相關的評論（含港股 5 位數代碼），使用者不想要
            if it["person"] and self.pctx and not any(p.search(t) for p in self.pctx):
                continue                          # 容易跟運動員、演藝人員同名撞在一起的姓氏，要有 Fed／貨幣政策相關詞才算數
            out.append(it)
        return out

    def signals(self, title, tags=()):
        """回傳有對象的多空標示：[{"s": "能源股", "d": 1}, {"s": "高科技", "d": -1}, …]。

        來源有兩個：
          1. 板塊影響規則（sector_rules）：標題符合條件就給出規則裡寫的所有「對象＋方向」。
          2. 標題比對到的關注標的（如台積電）：標題的多方／空方關鍵字比較，多的那邊勝出，
             標在該標的上（只用在有開啟「多空標示」的標的）。
        沒有對象就不標，不會再出現沒有主詞的「利多」。同一對象被兩邊拉扯（一利多一利空）會抵銷。"""
        if not self.sentiment:
            return []
        t = self.clean(title)
        votes = {}
        for r in self.rules:
            if not any(p.search(t) for p in r["topics"]):
                continue
            if r["triggers"] and not any(p.search(t) for p in r["triggers"]):
                continue
            if any(p.search(t) for p in r["excludes"]):
                continue
            for subj, d in r["effects"]:
                votes[subj] = votes.get(subj, 0) + d
        pos = sum(1 for p in self.pos if p.search(t))
        neg = sum(1 for p in self.neg if p.search(t))
        d = 1 if pos > neg else (-1 if neg > pos else 0)
        if d:
            for name in list(tags)[:3]:
                it = self.by_name.get(name)
                if it and it["dir"]:
                    votes[name] = votes.get(name, 0) + d
        return [{"s": s_, "d": 1 if v > 0 else -1} for s_, v in votes.items() if v != 0][:6]

    def has_finance_context(self, title):
        """標題有沒有財經相關的字。給「主動搜尋」用：關鍵字本身可能太通用（例如「首度」），
        抓回來的文章要額外確認真的跟財經有關，才不會洗進體育、娛樂新聞。"""
        t = self.clean(title)
        return any(p.search(t) for p in self.fctx)

    def pos_neg_words(self, title):
        """標題命中的多方／空方關鍵詞本身（詞的集合，不是分數）。給去重判斷用。"""
        t = self.clean(title)
        pos = frozenset(w for w, p in self.pos_words if p.search(t))
        neg = frozenset(w for w, p in self.neg_words if p.search(t))
        return pos, neg

    def score(self, title, summary, official=False, item_boost=0):
        """回傳 (基礎重要度 1~5, 情緒 -1/0/1, 是否命中中性關鍵字)。
        中性關鍵字（如「資本支出」「擴產」）只是額外標示話題性質，
        獨立於多方／空方關鍵字的比較，不會互相抵銷或覆蓋。
        同事件被多家報導的重要度加成在資料庫端計算。"""
        title, summary = self.clean(title), self.clean(summary)
        text = title + " " + summary
        hit = sum(w for p, w in self.imp if p.search(text))
        base = 1 + min(3, hit) + (1 if official else 0) + item_boost
        base = max(1, min(5, base))
        sent, neutral = 0, False
        if self.sentiment:
            pos = sum(1 for p in self.pos if p.search(title))
            neg = sum(1 for p in self.neg if p.search(title))
            sent = 1 if pos > neg else (-1 if neg > pos else 0)
            neutral = any(p.search(title) for p in self.neu)
        return base, sent, neutral

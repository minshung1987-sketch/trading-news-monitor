# -*- coding: utf-8 -*-
"""抓取與解析：RSS / Atom / 證交所 JSON，以及自動產生 Google 新聞搜尋來源。"""
import gzip
import hashlib
import html
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from pageinfo import decode_html

from config import flatten_noise, item_keywords
from zhnorm import to_simp

GN_EDITIONS = {
    "tw": ("zh-TW", "TW", "TW:zh-Hant"),
    "us": ("en-US", "US", "US:en"),
}
DEFAULT_EDITIONS = {
    "台股": ["tw"],
    "美股": ["us"],
    "加密貨幣": ["us"],
    "期貨/外匯": ["us", "tw"],
    "總經": ["us", "tw"],
}
MAX_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------- 來源清單

def google_news_url(keywords, edition, when="2d", exclude=()):
    terms = ['"%s"' % k.replace('"', "") for k in keywords[:6] if k.strip()]
    q = "(" + " OR ".join(terms) + ")"
    for ex in list(exclude)[:8]:                      # 在搜尋時就排除已知的雜訊詞組（Google 的 -"詞組"）
        q += ' -"%s"' % ex.replace('"', "")
    q += " when:" + when
    hl, gl, ceid = GN_EDITIONS[edition]
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": hl, "gl": gl, "ceid": ceid})


def _noise_for(it, noise):
    """雜訊詞組中，包含這個標的任一關鍵字的那些（例如黃金 → 黃金周、黃金交叉），拿去在 Google 搜尋時排除。"""
    kws = [to_simp(k).lower() for k in item_keywords(it)]
    return [n for n in noise if any(k and k in to_simp(n).lower() for k in kws)]


def build_sources(cfg):
    """設定檔中的 feeds，加上依關注清單自動產生的 Google 新聞搜尋，
    以及（如果開啟）依中性關鍵詞自動產生的 Google 新聞搜尋。"""
    out = [dict(f) for f in cfg.get("feeds", []) if f.get("enabled", True) and f.get("url")]
    noise = flatten_noise(cfg.get("noise_phrases"))
    for it in cfg.get("watchlist", []):
        g = it.get("google", "auto")
        if g is False:
            continue
        eds = DEFAULT_EDITIONS.get(it.get("market"), ["tw"]) if g == "auto" else g
        for ed in eds:
            out.append({
                "name": "Google 新聞：%s（%s）" % (it["name"], ed.upper()),
                "url": google_news_url(item_keywords(it), ed, exclude=_noise_for(it, noise)),
                "type": "rss",
                "market": it.get("market", ""),
                "keep_all": False,
                "official": False,
                "google": True,
            })
    settings = cfg.get("settings", {})
    for label, cfg_key, setting_key in (("多方", "positive_words", "positive_word_search"),
                                        ("空方", "negative_words", "negative_word_search"),
                                        ("中性", "neutral_words", "neutral_word_search")):
        if not settings.get(setting_key):
            continue
        words = [w for w in (cfg.get(cfg_key) or []) if w.strip()]
        batches = [words[i:i + 6] for i in range(0, len(words), 6)]   # 跟關注標的一樣，一次最多 6 個詞
        for i, batch in enumerate(batches, 1):
            out.append({
                "name": "%s關鍵字搜尋（%d/%d）" % (label, i, len(batches)),
                "url": google_news_url(batch, "tw"),
                "type": "rss",
                "market": "總經",
                "keep_all": True,          # 這是主動搜尋，不是分類：抓到的就收，不用再比對關注標的
                "official": False,
                "google": True,
                "finance_context_required": True,   # 關鍵字本身可能太通用（例如「首度」），
                                                     # 標題還要有財經相關的字或股票代碼才留下，避免洗進體育／娛樂新聞
            })
    return out


# ---------------------------------------------------------------- HTTP

class _RedirectAll308(urllib.request.HTTPRedirectHandler):
    """urllib 內建只會自動跟隨 301/302/303/307，不含 308（永久重新導向），
    但不少網站（例如 CoinDesk）搬家後就是回 308，所以要自己補上。"""
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, code, msg, headers)


def http_get(url, user_agent, timeout, verify_ssl=True, data=None, headers=None, with_headers=False):
    """抓取網址，回傳內容（bytes）。data 不為 None 時改用 POST；headers 可覆寫預設標頭；
    with_headers=True 時回傳 (內容, 回應標頭)。"""
    h = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    ctx = None if verify_ssl else ssl._create_unverified_context()
    opener = urllib.request.build_opener(_RedirectAll308, urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=timeout) as r:
        body = r.read(MAX_BYTES + 1)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        resp_headers = r.headers
    if len(body) > MAX_BYTES:
        raise ValueError("回應內容過大")
    if enc == "gzip":
        body = gzip.decompress(body)
    elif enc == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    return (body, resp_headers) if with_headers else body


# ---------------------------------------------------------------- 小工具

_TRACK = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_)", re.I)


def norm_url(u):
    p = urllib.parse.urlsplit((u or "").strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not _TRACK.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                                    urllib.parse.urlencode(q), ""))


def make_uid(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def clean_text(s, limit=300):
    if not s:
        return ""
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_date(s):
    """回傳 epoch 秒；解析不了就回傳 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    m = _ISO.match(s)
    if m:
        y, mo, d, hh, mi, ss, tz = m.groups()
        if not tz or tz == "Z":
            off = timezone.utc
        else:
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            off = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        try:
            return int(datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), tzinfo=off).timestamp())
        except ValueError:
            return 0
    return 0


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def _safe_url(u):
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else ""


# ---------------------------------------------------------------- RSS / Atom

def _parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        return ET.fromstring(text)


def _rss_item(el):
    link = ""
    orig = _child(el, "origLink")
    if orig is not None and _text(orig):
        link = _text(orig)
    if not link:
        link = _text(_child(el, "link"))
    if not link:
        guid = _text(_child(el, "guid"))
        if guid.startswith("http"):
            link = guid
    src_el = _child(el, "source")
    return {
        "title": _text(_child(el, "title")),
        "url": link,
        "summary": _text(_child(el, "description", "encoded")),
        "date": _text(_child(el, "pubDate", "date", "published", "updated")),
        "media": _text(src_el),
    }


def _atom_entry(el):
    link = ""
    for c in el:
        if _local(c.tag) == "link":
            rel = c.attrib.get("rel", "alternate")
            if rel == "alternate" and c.attrib.get("href"):
                link = c.attrib["href"]
                break
            if not link and c.attrib.get("href"):
                link = c.attrib["href"]
    return {
        "title": _text(_child(el, "title")),
        "url": link,
        "summary": _text(_child(el, "summary", "content")),
        "date": _text(_child(el, "published", "updated")),
        "media": "",
    }


def parse_feed(data, default_source):
    root = _parse_xml(data)
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "item":
            raw = _rss_item(el)
        elif name == "entry":
            raw = _atom_entry(el)
        else:
            continue
        title = clean_text(raw["title"], 400)
        url = _safe_url(raw["url"])
        if not title or not url:
            continue
        media = clean_text(raw["media"], 60)
        if media and title.endswith(" - " + media):
            title = title[: -len(" - " + media)].rstrip()
        # Google 新聞的 description 只是重複標題與連結，沒有資訊量
        summary = "" if "news.google.com" in url else clean_text(raw["summary"], 300)
        out.append({
            "title": title,
            "url": url,
            "summary": summary,
            "published": parse_date(raw["date"]),
            "source": media or default_source,
            "uid": make_uid(norm_url(url)),
        })
    return out


# ---------------------------------------------------------------- 證交所／櫃買 重大訊息（JSON）

def _roc_datetime(d, t):
    d = re.sub(r"\D", "", d or "")
    t = re.sub(r"\D", "", t or "")
    try:
        if len(d) == 7:
            y, m, dd = int(d[:3]) + 1911, int(d[3:5]), int(d[5:7])
        elif len(d) == 8:
            y, m, dd = int(d[:4]), int(d[4:6]), int(d[6:8])
        else:
            return 0
        t = t.zfill(6)[:6]
        dt = datetime(y, m, dd, int(t[:2]), int(t[2:4]), int(t[4:6]), tzinfo=timezone(timedelta(hours=8)))
        return int(dt.timestamp())
    except ValueError:
        return 0


def parse_twse_json(data, default_source):
    rows = json.loads(data.decode("utf-8-sig"))
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("Data") or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items()}
        code = r.get("公司代號") or r.get("SecuritiesCompanyCode") or r.get("Code") or ""
        name = r.get("公司名稱") or r.get("CompanyName") or r.get("Name") or ""
        subject = r.get("主旨") or r.get("Subject") or ""
        if not subject:
            continue
        d = r.get("發言日期") or r.get("Date") or ""
        t = r.get("發言時間") or r.get("Time") or ""
        head = " ".join(x for x in (code, name) if x)
        out.append({
            "title": ("%s：%s" % (head, subject)) if head else subject,
            "url": "https://mops.twse.com.tw/mops/web/t05sr01_1",
            "summary": clean_text(r.get("說明") or r.get("Description") or "", 300),
            "published": _roc_datetime(d, t),
            "source": default_source,
            "uid": make_uid("twse", code, d, t, subject),
        })
    return out


# ---------------------------------------------------------------- 華爾街見聞（自家 JSON 接口）

def parse_wscn_json(data, default_source):
    """華爾街見聞網站自己用的資料接口（RSSHub 也是用它）。
    格式：{"code":20000,"data":{"items":[{"resource_type":"article","resource":{title, uri, display_time, content_short}}, ...]}}
    快訊（resource_type = live）沒有標題與網址，略過。"""
    obj = json.loads(data.decode("utf-8-sig"))
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict) or "items" not in obj["data"]:
        raise ValueError("華爾街見聞接口的格式跟預期不同（網址可能有誤，或接口已改版）")
    if obj.get("code") not in (None, 20000):
        raise ValueError("華爾街見聞接口回傳錯誤：%s" % obj.get("message"))
    out = []
    for it in obj["data"]["items"] or []:
        if not isinstance(it, dict) or it.get("resource_type") != "article":
            continue
        r = it.get("resource") or {}
        title = clean_text(r.get("title"), 400)
        url = _safe_url(r.get("uri"))
        if not title or not url:
            continue
        ts = r.get("display_time")
        out.append({
            "title": title,
            "url": url,
            "summary": clean_text(r.get("content_short"), 300),
            "published": int(ts) if isinstance(ts, (int, float)) and ts > 0 else 0,
            "source": default_source,
            "uid": make_uid(norm_url(url)),
        })
    return out


# ---------------------------------------------------------------- 富途牛牛「要聞」（自家 JSON 接口）

def parse_futunn_headlines(data, default_source):
    """富途牛牛新聞頁「要聞」清單自己用的資料接口。
    格式：{"code":0,"data":{"list":[{title,url,timestamp,source,abstract,...}], "honeyPotList":[...]}}

    honeyPotList 是反爬蟲的誘餌：裡面放一筆跟 list[0] 很像、但 id 和網址都是假的項目
    （網址裡會混進一段可疑字樣，正常使用者畫面上根本看不到這則），程式如果抓了它、甚至真的
    去 fetch 那個網址，就會暴露自己是程式在抓，不是瀏覽器——這裡完全不讀 honeyPotList，
    只處理 list。

    「要聞」其實是富途牛牛從多家原始媒體（金十數據、財聯社、智通財經APP、中信證券研究……）
    轉載彙整的清單，不是它自己寫的稿，所以每則新聞的來源用接口裡附的原始出處（item 的
    source 欄位），沒有的話才退回用這個設定的名稱——這樣如果裡面剛好混到「華爾街見聞」的
    稿子，會算在原本的「華爾街見聞」底下，不會因為多了「富途牛牛」這層外殼而被重要消息
    來源漏掉。timestamp 是字串形式的 epoch 秒數（不是毫秒）。標題是簡體字。"""
    obj = json.loads(data.decode("utf-8-sig"))
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict) or "list" not in obj["data"]:
        raise ValueError("富途牛牛要聞接口的格式跟預期不同（網址可能有誤，或接口已改版）")
    if obj.get("code") not in (None, 0):
        raise ValueError("富途牛牛要聞接口回傳錯誤：%s" % obj.get("message"))
    out = []
    for it in obj["data"]["list"] or []:
        if not isinstance(it, dict):
            continue
        title = clean_text(it.get("title"), 400)
        url = _safe_url(it.get("url"))
        if not title or not url:
            continue
        try:
            ts = int(str(it.get("timestamp") or "0").strip())
        except ValueError:
            ts = 0
        source = clean_text(it.get("source"), 60) or default_source
        out.append({
            "title": title,
            "url": url,
            "summary": clean_text(it.get("abstract"), 300),
            "published": ts,
            "source": source,
            "uid": make_uid(norm_url(url)),
        })
    return out


# ---------------------------------------------------------------- 東方財富首頁「快訊」小工具

class _EastmoneyKX(HTMLParser):
    """東方財富首頁裡 id="news_kx_c" 那個快訊小工具：一個 <div> 裡塞了好幾組
    <a href="...">標題</a>&nbsp;<cite>HH:MM</cite>，用 div 巢狀深度找出這個區塊的正確範圍，
    不是整頁隨便抓，避免抓到頁面其他地方剛好也有的 <a>／<cite>。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_block = False
        self.depth = 0
        self.items = []
        self.cur = None
        self.in_a = False
        self.in_cite = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and a.get("id") == "news_kx_c":
            self.in_block, self.depth = True, 0
            return
        if not self.in_block:
            return
        if tag == "div":
            self.depth += 1
        elif tag == "a" and a.get("href", "").startswith("http"):
            self.in_a = True
            self.cur = {"title": "", "url": a["href"], "time": ""}
        elif tag == "cite":
            self.in_cite = True

    def handle_endtag(self, tag):
        if not self.in_block:
            return
        if tag == "div":
            if self.depth == 0:
                self.in_block = False
            else:
                self.depth -= 1
        elif tag == "a" and self.in_a:
            self.in_a = False
            if self.cur and self.cur["title"].strip():
                self.items.append(self.cur)
            self.cur = None
        elif tag == "cite":
            self.in_cite = False

    def handle_data(self, data):
        if self.in_a and self.cur is not None:
            self.cur["title"] += data
        elif self.in_cite and self.items:
            self.items[-1]["time"] += data


def _bounded_div(text, start_marker, max_steps=20000):
    """找出某個開頭標記所在的 <div ...> 到它對應的 </div> 之間的內容（正確處理巢狀 div）。
    找不到開頭標記、標籤沒有正常配對，或版面比預期複雜很多（真實網站可能跟測試時的樣本不一樣，
    巢狀多到要找很久），都回傳 None，讓呼叫端當成這塊不存在、跳過，不要讓抓取卡住不動。"""
    i = text.find(start_marker)
    if i < 0:
        return None
    j = text.find(">", i)
    if j < 0:
        return None
    j += 1
    depth, k, steps = 1, j, 0
    while depth > 0:
        steps += 1
        if steps > max_steps:
            return None
        nxt_open = text.find("<div", k)
        nxt_close = text.find("</div>", k)
        if nxt_close < 0:
            return None
        if 0 <= nxt_open < nxt_close:
            depth += 1
            k = nxt_open + 4
        else:
            depth -= 1
            k = nxt_close + 6
    return text[j:k - 6]


class _EastmoneyItems(HTMLParser):
    """從一段 HTML 片段裡抓 <li>…</li> 清單：每個 <li> 只取第一個連向
    finance.eastmoney.com/a/ 文章的 <a>（後面若還有「全球快訊」「新股申購表」這類附加連結一律略過），
    以及緊接在前面的 <span class="ico_wico1">分類</span>（若有）。"""
    ART_RE = re.compile(r"^https?://finance\.eastmoney\.com/a/\d{10,}\.html")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.cur_cat = ""
        self.pending_cat = False
        self.in_li = False
        self.li_done = False        # 這個 <li> 已經取過連結，後面的連結不要
        self.in_a = False
        self.a_href = ""
        self.a_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            self.in_li, self.li_done = True, False
        elif tag == "span" and self.in_li and "ico_wico1" in (a.get("class") or ""):
            self.pending_cat = True
        elif tag == "a" and self.in_li and not self.li_done and self.ART_RE.match(a.get("href", "")):
            self.in_a, self.a_href, self.a_text = True, a["href"], ""

    def handle_endtag(self, tag):
        if tag == "span" and self.pending_cat:
            self.pending_cat = False
        elif tag == "a" and self.in_a:
            self.in_a = False
            title = self.a_text.strip()
            if title:
                self.items.append((self.cur_cat, title, self.a_href))
            self.li_done = True
            self.cur_cat = ""
        elif tag == "li":
            self.in_li = False
            self.cur_cat = ""

    def handle_data(self, data):
        if self.in_a:
            self.a_text += data
        elif self.pending_cat:
            self.cur_cat += data


def _parse_eastmoney_items(frag, default_source):
    p = _EastmoneyItems()
    try:
        p.feed(frag)
        p.close()
    except Exception:
        pass
    out, seen = [], set()
    for _cat, title, url in p.items:      # 東方財富自己的分類（大盤／題材／個股…）跟使用者的關注清單對不上，先不使用
        title, url = clean_text(title, 400), _safe_url(url)
        if not title or not url or url in seen:
            continue
        seen.add(url)
        # 沒有精確發布時間，只知道是首頁目前精選出來的內容：當成「現在」，讓 _prepare() 用抓取時間頂替。
        out.append({"title": title, "url": url, "summary": "", "published": 0,
                    "source": default_source, "uid": make_uid(norm_url(url))})
    return out


def parse_eastmoney_kx(data, default_source):
    """解析東方財富首頁三塊內容：
      1. id="news_kx_c" 的「快訊」小工具：約 10 則，有精確到分鐘的時間。
      2. 「股市焦點」清單：焦點股市新聞，有分類（大盤／題材／個股），沒有精確時間。
      3. 「財經導讀」清單：分成 4 個子區塊（畫面上看起來像分頁，其實同一次載入就有），
         有分類（宏觀／行業／公司等），沒有精確時間。
    只要「快訊」小工具找不到就視為改版失效（整個來源失敗）；股市焦點、財經導讀是額外加碼，
    找不到就跳過那一塊，不影響其他部分。"""
    text = decode_html(data)
    kx = _EastmoneyKX()
    try:
        kx.feed(text)
        kx.close()
    except Exception:
        pass
    if not kx.items:
        raise ValueError("首頁裡找不到「快訊」區塊（id=news_kx_c），網站版面可能已經改版")

    now = datetime.now(timezone(timedelta(hours=8)))
    out = []
    for it in kx.items:
        title = clean_text(it["title"], 400)
        url = _safe_url(it["url"])
        m = re.search(r"(\d{1,2}):(\d{2})", it["time"])
        if not title or not url or not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        try:
            ts = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            continue
        if ts.timestamp() > now.timestamp() + 300:
            ts = ts - timedelta(days=1)
        out.append({"title": title, "url": url, "summary": "", "published": int(ts.timestamp()),
                    "source": default_source, "uid": make_uid(norm_url(url))})

    # 「股市焦點」是「股市焦點」文字之後第一個 class="nlist"，不是全頁第一個（頁面裡有十幾個同名區塊）
    idx = text.find("股市焦点")
    stock_block = _bounded_div(text[idx:], 'class="nlist"') if idx >= 0 else None
    if stock_block:
        out += _parse_eastmoney_items(stock_block, default_source)

    for n in range(1, 9):                          # 抓到抓不到為止，不寫死一定是 4 個
        frag = _bounded_div(text, 'tracker-eventcode="dfcfwsy_sp_cjdd%d_bgl"' % n)
        if frag is None:
            break
        out += _parse_eastmoney_items(frag, default_source)

    seen, uniq = set(), []
    for it in out:
        if it["uid"] in seen:
            continue
        seen.add(it["uid"])
        uniq.append(it)
    return uniq


_EM_ARTICLE = re.compile(
    r'<a href="(https://finance\.eastmoney\.com/a/\d{15,}\.html)"[^>]*>\s*(?:<strong>)?\s*([^<]{2,120}?)\s*(?:</strong>)?\s*</a>')


def parse_eastmoney_articles(data, default_source):
    """東方財富首頁上所有真正的文章連結（不分區塊，整頁一起抓）。

    判斷「是不是真的文章」靠網址規律：這個網站的廣告、開戶、基金銷售、股吧留言都是別的網域或別的
    路徑，只有真正的文章會長這個樣子：finance.eastmoney.com/a/一串數字.html。這樣不用去猜頁面上
    哪一塊 <div> 的邊界（改版就可能失效），對這個網址規律的依賴也小一點。

    這裡沒有每篇文章各自的發布時間（只有小工具「快訊」區塊才有，見 parse_eastmoney_kx），
    所以 published 一律填 0，交給 _prepare() 退回抓取當下的時間——代表新聞出現的時間是「我們
    看到它」的時間，不是它實際發布的時間，兩者可能差到幾小時。"""
    text = decode_html(data)
    seen, out = set(), []
    for url, title in _EM_ARTICLE.findall(text):
        if url in seen:
            continue
        seen.add(url)
        title = clean_text(title, 400)
        if not title:
            continue
        out.append({"title": title, "url": url, "summary": "", "published": 0,
                    "source": default_source, "uid": make_uid(norm_url(url))})
    if not out:
        raise ValueError("首頁裡找不到任何文章連結，網站版面可能已經改版")
    return out


# ---------------------------------------------------------------- MoneyDJ 理財網（手機版，伺服器端直接輸出）

_MONEYDJ_ROW = re.compile(
    r'<li class="newsRow">\s*'
    r'<div class="newsListTitle"><a class="newsLink"[^>]*href="([^"]+)">([^<]+)</a></div>\s*'
    r'<div class="newsListDateTime[^"]*">([^<]+)</div>')


_TPE = timezone(timedelta(hours=8))


def _resolve_ampm_sequence(entries):
    """entries：[(year, month, day, hour_raw 1~12, minute), ...]，由新到舊排列。
    理財網的時間是 12 小時制但沒有標示上午／下午（「01:16」有可能是凌晨 1 點、也有可能是下午 1 點）。
    唯一能確定的是整份清單由新到舊排列，所以依序往回推：每一筆的時間，兩種可能（上午／下午）裡，
    選「不晚於前一筆」、而且離前一筆最近的那個——這樣整份清單一定會是嚴格遞減。"""
    out, prev = [], None
    for y, mo, d, hr, mi in entries:
        try:
            am = datetime(y, mo, d, 0 if hr == 12 else hr, mi, tzinfo=_TPE)
            pm = datetime(y, mo, d, 12 if hr == 12 else hr + 12, mi, tzinfo=_TPE)
        except ValueError:
            out.append(None)
            continue
        if prev is None:
            now = datetime.now(_TPE)
            cands = [c for c in (am, pm) if c <= now + timedelta(minutes=5)]
            chosen = max(cands) if cands else min(am, pm)
        else:
            cands = [c for c in (am, pm) if c <= prev]
            chosen = max(cands) if cands else min(am, pm)
        out.append(chosen)
        prev = chosen
    return out


def parse_moneydj(data, default_source):
    """MoneyDJ 理財網手機版的新聞清單（例如 m.moneydj.com/newsList.aspx?a=MB010000）。
    跟桌面版不同，手機版是伺服器端直接輸出整份清單，不需要瀏覽器執行 JavaScript，可以直接抓取。
    日期時間格式是「YYYY/MM/DD HH:MM」（12 小時制、無上午下午標示），台北時區，見 _resolve_ampm_sequence。"""
    text = decode_html(data)
    rows = _MONEYDJ_ROW.findall(text)
    if not rows:
        raise ValueError("理財網頁面裡找不到新聞清單（class=newsRow），網站版面可能已經改版")
    parsed, entries = [], []
    for href, title, dt in rows:
        title = clean_text(html.unescape(title), 400)
        url = _safe_url(urllib.parse.urljoin("https://m.moneydj.com/", href))
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", dt.strip())
        if not title or not url or not m:
            continue
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        parsed.append((title, url))
        entries.append((y, mo, d, hh, mm))
    out = []
    for (title, url), ts in zip(parsed, _resolve_ampm_sequence(entries)):
        if ts is None:
            continue
        out.append({"title": title, "url": url, "summary": "", "published": int(ts.timestamp()),
                    "source": default_source, "uid": make_uid(norm_url(url))})
    return out


# ---------------------------------------------------------------- 對外介面

def parse_source(data, src):
    """把已經抓到的原始內容，依這個來源設定的類型解析成新聞清單。"""
    if src.get("type") == "twse_json":
        return parse_twse_json(data, src["name"])
    if src.get("type") == "wscn_json":
        return parse_wscn_json(data, src["name"])
    if src.get("type") == "futunn_headlines":
        return parse_futunn_headlines(data, src["name"])
    if src.get("type") == "eastmoney_kx":
        return parse_eastmoney_kx(data, src["name"])
    if src.get("type") == "eastmoney_articles":
        return parse_eastmoney_articles(data, src["name"])
    if src.get("type") == "moneydj":
        return parse_moneydj(data, src["name"])
    return parse_feed(data, src["name"])


def fetch_source(src, settings):
    """抓取並解析一個來源，回傳項目清單；出錯時直接丟出例外由呼叫端記錄。"""
    ua = src.get("user_agent") or settings["user_agent"]
    data = http_get(src["url"], ua, settings["request_timeout"], settings.get("verify_ssl", True))
    return parse_source(data, src)


def describe_error(e):
    """把例外轉成使用者看得懂的一句話。"""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        hint = "（網站拒絕存取，可能需要自訂 user_agent 或該來源已停用）" if e.code in (401, 403, 429) else ""
        return "HTTP %d %s%s" % (e.code, e.reason, hint)
    if isinstance(e, urllib.error.URLError):
        msg = str(e.reason)
        if isinstance(e.reason, socket.gaierror):
            return "找不到這個網站（請檢查網路連線與網址）：" + msg
        if isinstance(e.reason, ConnectionRefusedError):
            return "對方拒絕連線：" + msg
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg += "（Mac 使用者請到「應用程式/Python 3.x」資料夾，執行 Install Certificates.command）"
        return msg
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "連線逾時"
    if isinstance(e, ET.ParseError):
        return "內容不是有效的 RSS/XML（網址可能有誤，或被網站擋下）"
    if isinstance(e, (json.JSONDecodeError, UnicodeDecodeError)):
        return "內容不是有效的 JSON"
    if type(e) is ValueError:
        return str(e)
    return "%s: %s" % (type(e).__name__, e)

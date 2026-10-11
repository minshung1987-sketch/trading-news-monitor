# -*- coding: utf-8 -*-
"""解析新聞文章頁面：摘要文字，以及「表格／圖表」類的圖片。

圖片判斷規則（預設模式 tables，寧可漏掉也不要把一般照片當成圖表）：
  1. 圖說、替代文字、標題裡有攝影來源標註（美聯社、路透、圖／本報資料照片…）→ 照片，排除
  2. 檔名或圖說有表格／圖表特徵（Table、製表、資料來源、統計…）→ 收
  3. 是文章首圖（跟 og:image 同一張）→ 排除
  4. 放在 <figure> 裡、圖說完全空白 → 收（新聞網站的資訊圖表常常沒有圖說，照片幾乎都有）
  5. 其餘有圖說的 → 排除
另外一律排除：網站 logo／圖示／廣告／相關新聞縮圖（太小或位在導覽、側欄、頁尾）。

用標準函式庫的 HTMLParser，能處理一般的 <img> 與 AMP 的 <amp-img>。
"""
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
IMG_TAGS = {"img", "amp-img"}
CHROME_TAGS = {"nav", "header", "footer", "aside", "form"}
CHROME_WORDS = re.compile(
    r"(related|recommend|hot|rank|sidebar|side-|footer|header|navbar|nav-|menu|advert|\bad[-_ ]|ad-box|"
    r"banner|comment|share|widget|story-side|further-reading|story_lists|popular|breadcrumb|promo)", re.I)

PHOTO_CREDIT = re.compile(
    r"(美聯社|路透|法新社|彭博|中央社|Getty|AFP|\bAP\b|\bAP\/|Reuters|Bloomberg|Shutterstock|"
    r"資料照|檔案照|本報資料|示意圖|翻攝|攝影|／攝|記者.{0,8}攝|照片|提供)", re.I)
TABLE_WORDS = re.compile(r"(製表|製圖|資料來源|整理|統計|圖表|表格|一覽|比較表|懶人包|盤點|排行)")
TABLE_URL = re.compile(r"(table|chart|graph|infograph|tbl|圖表|統計)", re.I)
PHOTO_URL = re.compile(r"(PictureItem|photo_?item|\bstock[-_]?photo)", re.I)
JUNK_URL = re.compile(r"(logo|icon|avatar|sprite|banner|advert|pixel|blank|placeholder|noimg|tracking|"
                      r"spacer|loading|favicon|scorecardresearch|doubleclick)", re.I)
BAD_EXT = re.compile(r"\.(svg|gif|ico|webm|mp4)(\?|$)", re.I)


def _int(v):
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else 0


def _attrs(attrs):
    return {k.lower(): (v if v is not None else "") for k, v in attrs}


def _srcset_best(srcset):
    best, best_w = "", -1
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)(w|x)", bits[1])
            if m:
                w = int(m.group(1)) * (1 if m.group(2) == "w" else 1000)
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def image_key(url):
    """用來判斷「兩個網址是不是同一張圖」：去掉查詢參數；經濟日報的圖片代理網址把原圖放在 u= 參數裡。"""
    try:
        sp = urlsplit(url)
        q = parse_qs(sp.query)
        if "u" in q and q["u"]:
            inner = q["u"][0]
            return inner.split("&")[0].split("?")[0]
        return sp.scheme + "://" + sp.netloc + sp.path
    except Exception:
        return url


class PageParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.stack = []            # (tag, is_chrome)
        self.chrome_depth = 0
        self.skip_depth = 0        # script / style 內
        self.meta = {}
        self.og_images = []
        self.images = []           # 已收集的圖片（dict）
        self.paragraphs = []       # (文字, 是否在 <article> 內)
        self.article_depth = 0
        self._fig = None           # 目前所在的 <figure>：{"imgs": [...], "caption": [...]}
        self._in_caption = 0
        self._p_buf = None
        self._a_href = []          # <a> 堆疊（圖片外層連結 = 大圖）
        self._order = 0

    # ---------------------------------------------------------- 標籤
    def handle_starttag(self, tag, attrs):
        a = _attrs(attrs)
        tag = tag.lower()
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            content = a.get("content", "")
            if key and content:
                if key in ("og:image", "twitter:image", "twitter:image:src"):
                    self.og_images.append(urljoin(self.base, content))
                self.meta.setdefault(key, content)
            return
        if tag in ("script", "style"):
            self.skip_depth += 1
        if tag not in VOID:
            is_chrome = tag in CHROME_TAGS or bool(CHROME_WORDS.search((a.get("class", "") + " " + a.get("id", ""))))
            self.stack.append((tag, is_chrome))
            if is_chrome:
                self.chrome_depth += 1
            if tag == "article":
                self.article_depth += 1
        if tag == "a":
            self._a_href.append(a.get("href", ""))
        if tag == "figure":
            self._fig = {"imgs": [], "caption": []}
        if tag == "figcaption":
            self._in_caption += 1
        if tag == "p" and self.skip_depth == 0:
            self._p_buf = []
        if tag in IMG_TAGS:
            self._image(tag, a)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style") and self.skip_depth:
            self.skip_depth -= 1
        if tag == "a" and self._a_href:
            self._a_href.pop()
        if tag == "figcaption" and self._in_caption:
            self._in_caption -= 1
        if tag == "figure" and self._fig is not None:
            cap = re.sub(r"\s+", " ", " ".join(self._fig["caption"])).strip()
            for im in self._fig["imgs"]:
                im["caption"] = cap
            self._fig = None
        if tag == "p" and self._p_buf is not None:
            text = re.sub(r"\s+", " ", "".join(self._p_buf)).strip()
            if text:
                self.paragraphs.append((text, self.chrome_depth > 0, self.article_depth > 0))
            self._p_buf = None
        # 彈出堆疊
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _t, ch in self.stack[i:]:
                    if ch:
                        self.chrome_depth -= 1
                for t, _ch in self.stack[i:]:
                    if t == "article":
                        self.article_depth -= 1
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self._in_caption and self._fig is not None:
            self._fig["caption"].append(data)
        if self._p_buf is not None:
            self._p_buf.append(data)

    # ---------------------------------------------------------- 圖片
    def _image(self, tag, a):
        src = (a.get("src") or a.get("data-src") or a.get("data-original") or a.get("data-lazy-src")
               or _srcset_best(a.get("srcset") or a.get("data-srcset")) or "")
        if not src or src.startswith("data:"):
            return
        thumb = urljoin(self.base, src.strip())
        full = thumb
        if self._a_href:                       # 外層連結指向圖片檔 → 那是大圖
            href = self._a_href[-1]
            if href and re.search(r"\.(jpe?g|png|webp)(\?|$)|photo\.php", href, re.I):
                full = urljoin(self.base, href)
        self._order += 1
        im = {
            "src": thumb, "full": full,
            "w": _int(a.get("width")), "h": _int(a.get("height")),
            "alt": a.get("alt", "").strip(), "title": a.get("title", "").strip(),
            "caption": "", "in_figure": self._fig is not None,
            "chrome": self.chrome_depth > 0, "order": self._order,
        }
        self.images.append(im)
        if self._fig is not None:
            self._fig["imgs"].append(im)


def parse_page(html_text, base_url):
    p = PageParser(base_url)
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass                                    # 壞掉的 HTML：能收多少算多少
    return p


def decode_html(data, headers=None):
    """依 Content-Type 或 <meta charset> 解碼；失敗就用 utf-8 並取代壞字元。"""
    charset = None
    if headers is not None:
        m = re.search(r"charset=([\w-]+)", headers.get("Content-Type", "") or "", re.I)
        if m:
            charset = m.group(1)
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', data[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for cs in (charset, "utf-8"):
        if cs:
            try:
                return data.decode(cs)
            except (LookupError, UnicodeDecodeError):
                continue
    return data.decode("utf-8", "replace")


def _caption_of(im):
    return (im["caption"] or im["title"] or im["alt"] or "").strip()


def classify(im, lead_keys):
    """回傳 (是否收, 原因)。"""
    url = im["src"]
    text = " ".join(x for x in (im["caption"], im["title"], im["alt"]) if x)
    if PHOTO_CREDIT.search(text) or PHOTO_URL.search(url):
        return False, "照片（有攝影來源標註）"
    if TABLE_URL.search(url) or TABLE_WORDS.search(text):
        return True, "表格／圖表特徵"
    if image_key(url) in lead_keys or image_key(im["full"]) in lead_keys:
        return False, "文章首圖"
    if im["in_figure"] and not _caption_of(im):
        return True, "無圖說的內文圖"
    return False, "有圖說的一般圖片"


def candidates(page):
    out, seen = [], set()
    for im in page.images:
        key = image_key(im["src"])
        if key in seen:
            continue
        if im["chrome"] or BAD_EXT.search(im["src"]) or JUNK_URL.search(im["src"]):
            continue
        w, h = im["w"], im["h"]
        if w and w < 400 or h and h < 150:
            continue
        if w and h and (w / h > 4 or h / w > 4):
            continue
        seen.add(key)
        out.append(im)
    return out


def pick_images(page, mode="tables", limit=3):
    """從頁面挑出要顯示的圖。mode: tables（只要表格圖表）或 all（全部內文圖）。"""
    lead_keys = {image_key(u) for u in page.og_images}
    picked = []
    for im in candidates(page):
        if mode == "all":
            ok = True
        else:
            ok, _why = classify(im, lead_keys)
        if ok:
            picked.append({"src": im["src"], "full": im["full"], "w": im["w"], "h": im["h"],
                           "cap": _caption_of(im)[:120]})
        if len(picked) >= limit:
            break
    return picked


def snippet(page, limit=180):
    """文章開頭的小段文字，當作摘要。優先用 <article> 裡的段落，再退回 og:description。"""
    paras = [t for t, chrome, in_art in page.paragraphs if not chrome and len(t) >= 25]
    in_article = [t for t, chrome, in_art in page.paragraphs if not chrome and in_art and len(t) >= 25]
    use = in_article or paras
    text = " ".join(use[:2]).strip()
    if not text:
        text = html.unescape(page.meta.get("og:description") or page.meta.get("description") or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit] + ("…" if len(text) > limit else "")

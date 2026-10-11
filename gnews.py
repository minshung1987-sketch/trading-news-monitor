# -*- coding: utf-8 -*-
"""把 Google 新聞的加密連結（news.google.com/rss/articles/CBMi…）還原成新聞網站的真實網址。

做法照公開的開源專案（googlenewsdecoder 等）：
  1. 舊格式的代碼：直接把 base64 解開就有網址（不用連網）。
  2. 新格式的代碼：先抓 Google 新聞的文章頁，取得簽章與時間戳（data-n-a-sg / data-n-a-ts），
     再呼叫 Google 內部的 batchexecute 接口，回傳裡面有真實網址。

注意：第 2 步用的是 Google 未公開的內部接口，可能被限流、可能哪天改版失效。
失效時只會拿不到圖片預覽，其他功能不受影響。
"""
import base64
import json
import re
import urllib.parse

from fetcher import http_get

GN_HOST = "news.google.com"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
PAGE_BASES = ("https://news.google.com/articles/", "https://news.google.com/rss/articles/")


class DecodeError(Exception):
    pass


def is_google_news(url):
    try:
        sp = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return sp.hostname == GN_HOST and bool(re.search(r"/(rss/)?(articles|read)/[^/]+", sp.path))


def token_of(url):
    return urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]


def decode_offline(token):
    """舊格式代碼：base64 → protobuf，網址就在裡面。新格式（AU_yqL…）回傳 None。"""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    prefix, suffix = b"\x08\x13\x22", b"\xd2\x01\x00"
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    if raw.endswith(suffix):
        raw = raw[:-len(suffix)]
    if not raw:
        return None
    length = raw[0]
    if length >= 0x80:                 # 長度超過 127 時用兩個位元組（varint）
        length = (raw[0] & 0x7F) | (raw[1] << 7)
        body = raw[2:2 + length]
    else:
        body = raw[1:1 + length]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("AU_yqL"):      # 新格式，必須連網解
        return None
    return text if re.match(r"^https?://", text) else None


def _params(token, ua, timeout, verify):
    """抓 Google 新聞的文章頁，取出解碼需要的簽章與時間戳。"""
    last = None
    for base in PAGE_BASES:
        try:
            data = http_get(base + token, ua, timeout, verify,
                            headers={"Accept": "text/html,*/*", "Cookie": "CONSENT=YES+cb"})
        except Exception as e:
            last = e
            continue
        text = data.decode("utf-8", "replace")
        sg = re.search(r'data-n-a-sg="([^"]+)"', text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', text)
        if sg and ts:
            return sg.group(1), ts.group(1)
        last = DecodeError("Google 新聞頁面裡找不到解碼參數（頁面格式可能已改版，或被要求同意 Cookie）")
    raise last or DecodeError("無法取得 Google 新聞頁面")


def _parse_response(text):
    """從 batchexecute 回傳裡取出真實網址。先用結構解析，失敗再用字串標記。"""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            arr = json.loads(line)
        except ValueError:
            continue
        for item in arr:
            if isinstance(item, list) and len(item) > 2 and item[1] == "Fbv4je" and isinstance(item[2], str):
                inner = json.loads(item[2])
                if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], str):
                    return inner[1]
    m = re.search(r'garturlres\\+"\s*,\s*\\+"(.*?)\\+"\s*,', text)
    if m:
        u = m.group(1)
        for _ in range(2):
            try:
                u = json.loads('"%s"' % u)
            except ValueError:
                break
        return u
    return None


def decode_online(token, ua, timeout=12, verify=True):
    sig, ts = _params(token, ua, timeout, verify)
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]' % (token, ts, sig))
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    data = http_get(BATCH_URL, ua, timeout, verify, data=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "Accept": "*/*"})
    url = _parse_response(data.decode("utf-8", "replace"))
    if not url or not re.match(r"^https?://", url):
        raise DecodeError("Google 新聞接口的回傳格式跟預期不同（可能已改版）")
    return url


def resolve(url, ua, timeout=12, verify=True):
    """Google 新聞連結 → 真實網址；不是 Google 新聞連結就原樣回傳。"""
    if not is_google_news(url):
        return url
    token = token_of(url)
    real = decode_offline(token)
    if real:
        return real
    return decode_online(token, ua, timeout, verify)

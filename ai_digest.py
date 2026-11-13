# -*- coding: utf-8 -*-
"""AI 文字摘要：把規則式摘要（digest()）整理成的精簡清單送給 Claude API，換回一段真正的文字摘要；
另外也用來整理「事件追蹤」的重點與時間軸。只在使用者按下按鈕時呼叫一次，不會自動排程、不會背景執行、不會自己重試。"""
import json
import time
import urllib.error

from fetcher import http_get

API_URL = "https://api.anthropic.com/v1/messages"


class AiDigestError(Exception):
    pass


def _fmt_items(items, limit=12):
    lines = []
    for r in items[:limit]:
        tags = "、".join(r.get("tags") or [])
        sig = "；".join("%s%s" % (g["s"], "利多" if g["d"] > 0 else "利空") for g in (r.get("signals") or []))
        bits = [r["title"]]
        if tags:
            bits.append("[標的:%s]" % tags)
        if sig:
            bits.append("[%s]" % sig)
        if r.get("source_count", 1) > 1:
            bits.append("[%d個來源]" % r["source_count"])
        lines.append("- " + " ".join(bits))
    return "\n".join(lines) if lines else "（無）"


def build_prompt(digest, hours):
    s = digest["stats"]
    parts = [
        "你是幫一位台灣交易者整理新聞的助理。以下是過去 %d 小時、程式用關鍵字規則篩選出來的新聞清單" % hours,
        "（標題後面 [標的:...] 是比對到的關注標的，[XX利多/利空] 是規則判斷的方向僅供參考，[N個來源] 代表有 N 家不同媒體報導同一事件）。",
        "請用繁體中文寫一段簡短的晨報，抓重點、講脈絡，不要逐條複述條列。大約 300~500 字，分成「總體情勢」「個股/標的焦點」「值得留意」三小段即可，不用加開場白或結語客套話。",
        "",
        "=== 整體統計 ===",
        "新聞 %d 則；多方 %d、空方 %d、中性 %d（僅供參考，不是投資建議）" % (s["total"], s["pos"], s["neg"], s["neutral"]),
        "",
        "=== 重要度最高 ===",
        _fmt_items(digest["top_important"]),
        "",
        "=== 多家來源都在報 ===",
        _fmt_items(digest["cross_source"]),
    ]
    for market, items in digest.get("by_market", {}).items():
        parts += ["", "=== %s 焦點 ===" % market, _fmt_items(items, limit=8)]
    return "\n".join(parts)


def call_claude(prompt, api_key, model, max_tokens=1200, timeout=30):
    """送一個提示給 Claude API，回傳文字。錯誤一律轉成 AiDigestError（訊息是白話）。"""
    if not api_key:
        raise AiDigestError("還沒有設定 API key。到「設定 → 一般設定」的「AI 文字摘要」填入，"
                            "key 可以到 console.anthropic.com 申請。")
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        raw = http_get(API_URL, "TradingNewsMonitor/1.0", timeout, verify_ssl=True, data=body, headers=headers)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            detail = (err_body.get("error") or {}).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise AiDigestError("API key 無效或已失效，請到「設定」重新確認（%s）" % (detail or "401"))
        if e.code == 429:
            raise AiDigestError("額度或頻率超過限制，稍後再試（%s）" % (detail or "429"))
        raise AiDigestError("呼叫 AI 失敗（HTTP %d）：%s" % (e.code, detail or str(e)))
    except Exception as e:
        raise AiDigestError("呼叫 AI 失敗：%s" % e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise AiDigestError("AI 回應格式異常")
    if "error" in data:
        msg = (data["error"] or {}).get("message", "未知錯誤")
        raise AiDigestError("AI 服務回應錯誤：%s" % msg)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise AiDigestError("AI 沒有回傳文字內容")
    return text


def generate(digest, hours, api_key, model, timeout=30):
    return call_claude(build_prompt(digest, hours), api_key, model, max_tokens=1200, timeout=timeout)


# ---------------------------------------------------------------- 事件追蹤：整理重點與時間軸

def build_event_prompt(name, items, today):
    lines = []
    for a in items:
        n_src = len({a["source"]} | {d["source"] for d in a.get("dups", [])})
        bits = ["%s %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(a["published"])), a["source"])]
        if n_src > 1:
            bits.append("[%d個來源]" % n_src)
        s = (a.get("summary") or "").replace("\n", " ").strip()
        lines.append("- %s｜%s%s" % ("｜".join(bits), a["title"], ("｜摘要：" + s[:120]) if s else ""))
    return "\n".join([
        "你是幫一位台灣交易者整理新聞事件的助理。使用者正在追蹤的事件是「%s」。" % name,
        "下面是程式依關鍵字蒐集到、跟這個事件有關的新聞（依時間由舊到新；[N個來源] 代表有 N 家不同媒體報導同一件事）。今天是 %s。" % today,
        "請「只根據這些新聞的標題與摘要」整理，只輸出一個 JSON 物件，不要有任何說明文字，也不要加 markdown 程式碼圍欄。格式：",
        '{"summary": "一到兩句話的事件概述",',
        ' "points": [{"status": "confirmed", "text": "重點"}, {"status": "unverified", "text": "重點"}],',
        ' "timeline": [{"date": "YYYY-MM-DD", "text": "當天發生的事"}]}',
        "",
        "規則：",
        "- points 最多 8 條。status 為 confirmed：官方公告、當事人或公司親口證實，或多家獨立媒體一致報導的事實；"
        "status 為 unverified：只有單一來源、「據報導／消息人士／傳聞」、各家說法不一致或還在調查中的內容。拿不準的一律標 unverified。",
        "- timeline 依日期由舊到新，同一天的事合併成一條，最多 15 條；date 必須是下面新聞顯示的日期。",
        "- 不可以補充新聞裡沒有的背景、歷史、數字或推測；不要寫投資建議，也不要判斷對股價的影響。",
        "- 使用繁體中文，簡潔。",
        "",
        "=== 新聞（%d 組）===" % len(items),
        "\n".join(lines),
    ])


def _extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise AiDigestError("AI 回傳的內容看不出是整理好的格式，請再按一次試試")
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        raise AiDigestError("AI 回傳的格式無法解析（可能內容被截斷），請再按一次試試")


def generate_event(name, items, api_key, model, timeout=90):
    """回傳 dict：summary / points / timeline（還沒整理過格式，交給 events.clean_ai）。"""
    prompt = build_event_prompt(name, items, time.strftime("%Y-%m-%d"))
    return _extract_json(call_claude(prompt, api_key, model, max_tokens=2500, timeout=timeout))

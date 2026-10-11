# -*- coding: utf-8 -*-
"""AI 文字摘要：把規則式摘要（digest()）整理成的精簡清單送給 Claude API，換回一段真正的文字摘要。
只在使用者按下按鈕時呼叫一次，不會自動排程、不會背景執行、不會自己重試。"""
import json
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


def generate(digest, hours, api_key, model, timeout=30):
    if not api_key:
        raise AiDigestError("還沒有設定 API key。到「設定 → 一般設定」的「AI 文字摘要」填入，"
                            "key 可以到 console.anthropic.com 申請。")
    prompt = build_prompt(digest, hours)
    body = json.dumps({
        "model": model,
        "max_tokens": 1200,
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

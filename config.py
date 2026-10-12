# -*- coding: utf-8 -*-
"""設定檔的預設值、驗證與讀寫。

使用者平常透過網頁上的「設定」修改；進階使用者也可以直接編輯 config.json。
"""
import copy
import json
import os
import re
import urllib.parse


def site_news_url(domain, when="2d"):
    """限定某個網站的 Google 新聞 RSS 搜尋（該網站自己沒有公開 RSS 時的替代方案）。"""
    q = "site:%s when:%s" % (domain, when)
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})


# 既有使用者的 config.json 已經存了一份完整的 feeds 清單（開著 app 跑過一次就會存檔），
# 之後這裡再怎麼改 DEFAULT_SOURCES 裡的網址，normalize() 都只會照抄 config.json 裡已經存的
# 那份、不會自動套用新網址（這是 feeds 這種「清單型」設定項目的已知限制，跟新增一個全新的
# 設定鍵不同、新鍵才會自動出現在舊使用者身上）。
# 下面這個表只做「網址一字不差」的一次性代換：舊網址換成新網址，讓 2026-10 這次調整
# （Google 新聞搜尋視窗從 2 天拉長到 4 天、華爾街見聞 limit 從 25 提高到 50，為了減少
# 「文章其實有抓，但因為視窗/數量太小被漏掉」的情況）也能套用到已經在用的使用者身上，
# 不用請他們自己去改 config.json。只比對網址字串，使用者自己手動改過網址的話就不會被動到。
_FEED_URL_MIGRATIONS = {
    site_news_url("money.udn.com", "2d"): site_news_url("money.udn.com", "4d"),
    site_news_url("ctee.com.tw", "2d"): site_news_url("ctee.com.tw", "4d"),
    "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global&accept=article&limit=25":
        "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global&accept=article&limit=50",
}

MARKET_ORDER = ["台股", "美股", "加密貨幣", "期貨/外匯", "總經", "政經人物", "法人報告"]

DEFAULT_SETTINGS = {
    "port": 8765,                 # 網頁儀表板的連接埠
    "open_browser": True,         # 啟動時自動開啟瀏覽器
    "refresh_minutes": 5,         # 每幾分鐘更新一次
    "keep_days": 14,              # 新聞保留天數（有加星號的不會被刪）
    "request_timeout": 15,        # 抓取單一來源的逾時秒數
    "max_items_per_source": 60,   # 每個來源每次最多取幾則（取最新的）
    "dedup_similarity": 0.72,     # 標題相似度門檻，越低合併越積極
    "sentiment_hint": True,       # 是否依標題關鍵字標示利多／利空（僅供參考）
    "positive_word_search": True, # 多方關鍵詞是否也主動搜尋新聞（不限定關注標的），量可能較大，可以關閉
    "negative_word_search": True, # 空方關鍵詞是否也主動搜尋新聞（同上）
    "neutral_word_search": True,  # 中性關鍵詞是否也主動搜尋新聞（不限定關注標的），量可能較大，可以關閉
    "verify_ssl": True,           # 若遇到憑證錯誤且無法修復，才考慮改成 false
    "images": True,               # 是否抓文章頁，顯示表格／圖表預覽與摘要
    "images_mode": "tables",      # tables＝只要表格圖表（預設）；all＝所有內文圖片
    "page_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 TradingNewsMonitor/1.0",
    "translate": True,            # 英文標題是否提供中文翻譯（機器翻譯，僅供參考）
    "translate_endpoint": "https://translate.googleapis.com/translate_a/single",
    "user_agent": "Mozilla/5.0 (compatible; TradingNewsMonitor/1.0; personal use)",
    "ai_digest": {                # AI 文字摘要：按按鈕才會呼叫，不會自動排程、不會自動花錢
        "api_key": "",             # 到 console.anthropic.com 申請，貼在這裡（存在你自己電腦的設定檔裡，明碼）
        "model": "claude-haiku-4-5-20251001",
        "digest_hours": 12,
    },
    "focus_weights": {             # 盤前焦點股候選名單的評分權重，是觀察名單不是預測，可自行調整
        "source": 2,                # 每多一個不同來源報導，加幾分
        "importance": 3,             # 乘上該標的新聞的最高重要度（1~5）
        "institution": 10,           # 有法人報告正面提及，加幾分（固定值，不是乘數）
        "net_signal": 2,             # 乘上「多方則數 - 空方則數」（不會是負的）
    },
}

DEFAULT_IMPORTANCE_WORDS = {
    # 這份清單刻意只收「事件本身」的詞，不收「股價已經怎麼動」的詞——
    # 「大跌8%」「漲停」「crash」這類描述的是結果不是原因，看到新聞當下那個反應可能還沒走完，
    # 不該讓它比一則真正的事件（例如財測下修、執行長請辭）更重要，所以不放進這份清單。
    #
    # 權重 3：近乎必然引發劇烈反應的事件
    "停牌": 3, "下市": 3, "處置": 3, "減資": 3, "破產": 3, "倒閉": 3, "違約": 3,
    "熔斷": 3, "駭客": 3, "被駭": 3, "盜幣": 3, "跑路": 3, "財報造假": 3, "會計造假": 3,
    "財報重編": 3, "掏空": 3, "淘空": 3, "無預警請辭": 3, "閃辭": 3, "執行長辭職": 3,
    "財務長辭職": 3, "執行長無預警": 3, "假帳": 3,
    "bankruptcy": 3, "halted": 3, "trading halt": 3, "delisted": 3, "delisting": 3,
    "hacked": 3, "SEC charges": 3, "ceo resigns": 3, "cfo resigns": 3, "resigns abruptly": 3,
    "accounting fraud": 3, "restatement": 3, "going concern": 3, "fraud investigation": 3,
    # 權重 2：多數情況下會有明顯反應的公司行動、政策與總經事件
    "財報": 2, "法說": 2, "法說會": 2, "財測": 2, "EPS": 2, "下修": 2, "上修": 2,
    "財測不如預期": 2, "財測優於預期": 2, "財報不如預期": 2, "財報優於預期": 2,
    "獲利不如預期": 2, "獲利超乎預期": 2, "營收不如預期": 2, "營收超乎預期": 2,
    "調升目標價": 2, "調降目標價": 2, "併購": 2, "收購": 2, "收購價": 2, "溢價收購": 2,
    "併購破局": 2, "惡意收購": 2, "敵意併購": 2, "私有化": 2, "庫藏股": 2, "私募": 2,
    "裁員": 2, "召回": 2, "制裁": 2, "關稅": 2, "出口管制": 2, "禁令": 2,
    "降息": 2, "升息": 2, "利率決策": 2, "非農": 2, "就業報告": 2, "IPO": 2,
    "信評調降": 2, "信評調升": 2, "調降信評": 2, "調升信評": 2, "降評": 2, "升評": 2,
    "做空報告": 2, "放空報告": 2, "放空機構": 2, "遭做空": 2, "遭放空": 2,
    "維權投資人": 2, "砍單": 2, "急單": 2, "大單": 2, "出貨異常": 2,
    "良率不佳": 2, "供應鏈中斷": 2, "斷鏈": 2, "集體訴訟": 2, "反壟斷調查": 2,
    "巨額罰款": 2, "司法調查": 2, "專利訴訟": 2, "專利侵權": 2, "三期臨床": 2,
    "臨床未達標": 2, "藥證未過": 2,
    "earnings": 2, "guidance": 2, "downgrade": 2, "upgrade": 2, "acquisition": 2,
    "acquire": 2, "acquires": 2, "merger": 2, "buyback": 2, "layoffs": 2, "recall": 2,
    "sanctions": 2, "tariff": 2, "tariffs": 2, "export controls": 2, "rate cut": 2,
    "rate hike": 2, "FOMC": 2, "CPI": 2, "PCE": 2, "payrolls": 2, "nonfarm": 2,
    "jobs report": 2, "OPEC": 2, "ETF approval": 2, "antitrust": 2, "stock split": 2,
    "hack": 2, "exploit": 2, "credit downgrade": 2, "credit rating cut": 2,
    "short seller": 2, "short report": 2, "activist investor": 2, "hostile takeover": 2,
    "patent lawsuit": 2, "antitrust probe": 2, "clinical trial": 2, "fda approval": 2,
    "fda rejection": 2, "class action": 2, "going private": 2,
    # 權重 1：一般相關訊號
    "營收": 1, "獲利": 1, "目標價": 1, "評等": 1, "合併": 1, "股利": 1, "配息": 1,
    "除權": 1, "除息": 1, "增資": 1, "訴訟": 1, "央行": 1, "通膨": 1, "通脹": 1,
    "ETF": 1, "上市": 1, "核准": 1, "庫存過剩": 1, "內部人買超": 1, "內部人賣超": 1,
    "revenue": 1, "price target": 1, "dividend": 1, "lawsuit": 1, "inflation": 1,
    "approved": 1, "SEC": 1, "probe": 1, "investigation": 1,
}

DEFAULT_POSITIVE_WORDS = [
    "上修", "調升", "調高",
    "漲價", "調漲", "供不應求", "缺貨", "不可抗力", "吃緊", "趨緊", "預付款", "樂觀",
    "搶購", "超額認購", "緊缺", "賣方市場",
]

DEFAULT_NEGATIVE_WORDS = [
    "看壞", "下修", "調降", "調低",
    "檢調", "羈押", "增資", "發債", "保守", "產能過剩", "供過於求", "搶單", "gdr",
    "處置", "買方市場",
]

# 中性關鍵字：只標示話題性質（例如財報常提到的資本支出／擴產），
# 不代表多空方向，獨立於上面兩份清單，不會互相抵銷。
DEFAULT_NEUTRAL_WORDS = [
    "資本支出", "擴產", "擴廠", "超級循環", "結構性",
    "拐點", "供給", "需求", "首度", "首次", "瓶頸", "加速",
    "看好", "入股", "庫存",
    "董事長", "總經理", "法說", "自結", "電話會議",
]

# 非商品／非標的的用法：這些詞組在比對前會先被遮掉（例如「黃金周」不是金價）。
# 一行可以寫多個（用逗號分隔）；# 開頭是註解。全部比對（關注標的、板塊規則、利多利空）共用。
DEFAULT_NOISE_PHRASES = [
    "# 黃金：黃金周、技術指標、地段、時段…都不是金價",
    "黃金周,黃金週,黃金交叉,黃金地段,黃金時段,黃金時間,黃金檔,黃金比例,黃金分割,黃金屋,黃金年代,黃金時代",
    "黃金組合,黃金搭檔,黃金拍檔,黃金陣容,黃金階段,黃金會員,黃金卡,黃金海岸,黃金三角,黃金獵犬,黃金奇異果",
    "黃金球,黃金鞋,黃金聯賽,黃金救援,golden week,golden cross,golden globe,golden gate,gold coast,gold medal",
    "# 石油／油價：食用油、保養油、油電車…不是原油",
    "橄欖油,食用油,沙拉油,花生油,葵花油,茶油,苦茶油,香油,麻油,芝麻油,精油,髮油,護髮油,魚油,按摩油,嬰兒油,潤膚油",
    "油電混合,油電車,油漆,油畫,油炸,油條,油膩",
    "# Apple：水果、食物、紐約別稱，不是蘋果公司",
    "apple harvest,apple festival,apple pie,apple cider,apple orchard,apple picking,apple sauce,applesauce,apple juice,apple tree,apple crisp,apple butter,apple cake,big apple",
    "蘋果汁,蘋果派,蘋果園,蘋果醋,蘋果樹,蘋果肌,青蘋果,蘋果日報",
    "# 想排除別的雜訊，直接在下面加一行；例如各國零售金價的洗版文章可以取消下一行的註解：",
    "# gold rate today,gold rates today,gold price today in,gold price in india,gold price in vietnam,per tola",
]

# 「法人報告」類標的（大摩、小摩…）的情境詞：標題要有其中一個，才算真的是法人評論／報告，
# 用來擋掉「小摩減持XX股票」這種持股異動揭露、「凱基金控增資」這種機構自己的消息、
# 「元大金融論壇」這種公關活動——這些都會提到機構名稱，但不是法人對個股／產業／總經的評論。
DEFAULT_PERSON_CONTEXT = [
    # 有些政經人物的姓氏是常見的英文姓（Jefferson、Williams、Barr…），容易跟運動員、演藝人員同名撞在一起
    # （例如美式足球球員 Justin Jefferson）。標成「person」的標的，標題要有其中一個詞才算數。
    "Fed", "FOMC", "聯準會", "美聯儲", "央行", "理事", "主席", "副主席", "總裁", "議息", "利率", "降息", "升息",
    "貨幣政策", "鷹派", "鴿派", "通膨", "通脹", "經濟", "monetary", "policy", "rate", "inflation",
    "economy", "economic", "central bank", "governor", "federal reserve", "hawkish", "dovish", "chair",
]

DEFAULT_FINANCE_CONTEXT = [
    # 多方／空方／中性關鍵詞「主動搜尋新聞」抓回來的文章，標題要有其中一個才留下。
    # 「首度」「首次」「下修」「處置」這類字太通用，體育、政治、社會新聞也常用，
    # 沒有這層過濾會洗進一堆無關新聞——所以這份清單刻意不收「處置」「下修」這種本身就是
    # 多空觸發詞、但單獨看也很通用的字，不然等於自己確認自己，這層過濾就白做了。
    # 本土券商故意用「XX證券」，不是單獨「元大」「富邦」這種字——那些字同時也是球隊、
    # 其他業務的名稱（例如「富邦悍將」是職棒球隊），單獨放會造成誤判。
    "股價", "股市", "台股", "美股", "陸股", "港股", "盤中", "盤後", "開盤", "收盤",
    "營收", "財報", "財測", "法說", "自結", "EPS", "漲停", "跌停", "法人", "外資", "投信", "自營商",
    "ETF", "基金", "期貨", "選擇權", "匯率", "利率", "央行", "通膨", "殖利率", "本益比",
    "股利", "配息", "增資", "減資", "上市", "上櫃", "興櫃", "董事會", "股東會", "庫藏股",
    "公開收購", "借殼", "下市", "注意股票", "融資", "融券", "當沖", "成交量",
    "漲幅", "跌幅", "個股", "類股", "指數", "加權指數", "那斯達克", "道瓊", "標普",
    "目標價", "評等", "評級", "元大證券", "凱基證券", "富邦證券", "國泰證券", "統一證券", "群益證券", "兆豐證券",
    # 以下是從多方／空方關鍵詞清單裡挑出來、本身已經夠具體（不太會在財經以外的新聞出現）的詞，
    # 讓這些詞自己就能當財經新聞的憑證，不用額外找別的財經字——這是「強茂、台半迎漲價紅利」這類
    # 標題原本會被濾掉（標題沒有其他財經字，但「漲價」本身已經很明確）的修正。
    "漲價", "調漲", "喊漲", "喊跌", "供不應求", "缺貨", "不可抗力", "吃緊", "趨緊", "預付款",
    "搶購", "超額認購", "緊缺", "賣方市場", "買方市場", "產能過剩", "供過於求", "搶單",
    "董事長", "總經理", "半導體", "晶圓", "面板", "記憶體", "產能", "砍單", "急單",
]

DEFAULT_INSTITUTION_CONTEXT = [
    "目標價", "評等", "評級", "升評", "降評", "看好", "看淡", "看空", "看多",
    "喊買", "喊進", "喊賣", "報告", "研選", "研选", "策略", "展望", "預估", "預測", "財測",
    "分析師", "首席", "策略師", "經濟學家", "建議", "觀點", "評論", "點評",
    "上修", "下修", "調升", "調降", "EPS",
    "曝", "揭露", "估計", "警告", "示警", "受惠", "加碼", "減碼", "再漲", "再跌",
]

# 「法人報告」類標的：標題有這些詞，即使有上面的情境詞也不收——是陸股／港股相關的評論，
# 不是使用者要的台股／美股／總經觀點。可自由增減。
DEFAULT_INSTITUTION_EXCLUDE = [
    "陸股", "A股", "a股", "港股", "中國股市", "中概股", "滬深", "深港通", "滬港通",
    "恒生指數", "恒指", "H股", "红筹股", "紅籌股", "上證", "深證", "滬指", "深指",
    "北向資金", "南向資金", "北向资金", "南向资金", "沪深", "沪港通", "深沪",
]

# 「商品」類標的（黃金、原油…）的情境詞：標題或摘要至少要出現其中一個，這則新聞才算跟這個商品有關。
# 用途是擋掉沒列進上面清單的新雜訊（單獨出現「黃金」卻完全沒講行情的文章）。可自由增減。
DEFAULT_COMMODITY_CONTEXT = [
    "價", "行情", "報價", "走勢", "漲", "跌", "飆", "暴", "崩", "挫", "攀", "衝", "突破", "站上", "失守", "回落", "反彈", "震盪",
    "承壓", "走強", "走弱", "走高", "走低", "看多", "看空", "新高", "新低", "期貨", "現貨", "ETF", "庫存", "儲備", "供需",
    "供給", "需求", "減產", "增產", "產量", "出口", "進口", "禁運", "制裁", "央行", "避險", "美元", "日圓", "利率", "殖利率",
    "債息", "通膨", "通脹", "Fed", "聯準會", "美聯儲", "降息", "升息", "盎司", "每桶", "多頭", "空頭", "部位", "賣壓", "買盤",
    "壓盤", "支撐", "OPEC", "XAU", "COMEX", "WTI", "Brent",
    "price", "prices", "futures", "spot", "ounce", "barrel", "rally", "slump", "surge", "plunge", "jump", "fall", "falls",
    "drop", "drops", "rise", "rises", "gain", "gains", "slip", "slips", "climb", "climbs", "record high", "demand", "supply",
    "inventory", "inventories", "output", "yield", "yields", "dollar", "inflation", "hedge", "bullish", "bearish",
    "forecast", "outlook", "safe haven", "central bank",
]

# 整篇文章直接排除：標題只要出現這些詞組（或符合這些寫法），這則新聞完全不會收進來，
# 跟上面的「非商品用法」不同——那個只是遮住某個詞不比對，這個是整篇丟掉。用來擋掉盤中速報這類
# 機械式產生、對交易沒有意義的價格快報（它們幾乎都含有「漲停」「跌停」，還會拉高重要度、誤標多空）。
DEFAULT_TITLE_EXCLUDE = [
    "盤中速報", "盤後速報", "盤中零股", "盤後零股", "尾盤速報", "開盤速報", "個股速報", "个股速报",
    "股價拉至漲停", "股價拉至跌停", "股價殺至跌停", "拉至漲停", "殺至跌停",
]

# 整篇文章直接排除：來源（媒體／出處名稱）只要符合下面任一名稱，這則新聞完全不收，
# 不管標題有沒有命中財經情境詞或股票代碼。用來擋掉娛樂、廣告／業配這類媒體——它們的標題
# 常常剛好用到「缺貨」「供不應求」「搶購」這種同時也被當成財經多空關鍵字、又被鏡射進
# finance_context 的通用詞（例如明星周邊「缺貨」、土產大閘蟹廣告「供不應求」），
# 光靠「標題裡有沒有財經詞」擋不掉，因為那個詞本身既是觸發詞也是自己的財經情境證據
# （跟「處置」曾經犯過的同一種「自我證明」問題一樣），只能從來源本身排除。可自由增減。
DEFAULT_SOURCE_EXCLUDE = [
    "娛樂星聞", "三星傳媒",
]

# 重要消息來源：來源（媒體／出處名稱）符合下面任一名稱的新聞，不管標題有沒有比對到關注標的、
# 板塊規則，也不管多方／空方／中性關鍵詞有沒有命中，一律收進來、並額外標記為「重要」，
# 可以在左側「重要消息來源」集錦單獨看到這幾家媒體的所有新聞。可自由增減（例如想追加其他媒體，
# 直接在這裡加上它在來源欄位會出現的名稱即可，部分名稱即可，不用整個媒體全名）。
DEFAULT_IMPORTANT_SOURCES = [
    "經濟日報", "工商時報", "華爾街見聞", "東方財富",
]

# 板塊影響規則：一行一條，格式  主題詞 + 觸發詞 ! 排除詞 => 對象利多, 對象利空
# （標題要同時出現任一主題詞與任一觸發詞才成立；! 排除詞可省略；# 開頭是註解）
# 這是依一般市場邏輯預設的草稿，市場環境不同結論就會不同（例如通膨與利率的階段），請依你的判斷修改。
DEFAULT_SECTOR_RULES = [
    "# 「多方詞」「空方詞」是特殊寫法，代表目前的「多方關鍵詞」「空方關鍵詞」清單本身——",
    "# 改那份清單，用到多方詞／空方詞的規則會一起跟著變，不用兩邊各改一次。",
    "# 下面能源／黃金／被動元件這三條刻意沒有用多方詞／空方詞，是各自寫死自己的漲跌用詞，",
    "# 不會被多方／空方關鍵詞清單的增減影響——如果你之後想讓它們也跟著共用清單變動，",
    "# 把後面那串字改成「多方詞」或「空方詞」即可。",
    "# 能源價格 → 能源與原物料受惠；通膨與利率壓力讓高科技與利率敏感股承壓",
    "能源價格,油價,原油,天然氣,燃料,crude oil,oil prices,natural gas + 大漲,上漲,飆漲,暴漲,創高,創新高,新高,surge,soar,jump,rally,record high,漲價,供不應求,缺貨 => 能源股利多, 原物料期貨利多, 高科技利空, 利率敏感股利空",
    "能源價格,油價,原油,天然氣,燃料,crude oil,oil prices,natural gas + 大跌,下跌,暴跌,重挫,崩跌,新低,plunge,tumble,slump,crash,產能過剩,供過於求 => 能源股利空, 原物料期貨利空, 高科技利多, 利率敏感股利多",
    "# 殖利率飆升、升息 → 高科技與利率敏感股承壓；降息 → 相反（排除「暫緩降息」這類否定說法）",
    "# 這幾條刻意不用多方詞／空方詞，因為殖利率的方向用詞跟一般漲跌用詞不完全相同，共用容易誤判",
    "殖利率,美債,treasury yields,bond yields + 飆升,飆漲,攀升,走高,創高,大漲,上升,spike,surge,climb,jump ! 回落,下降,走低 => 高科技利空, 利率敏感股利空",
    "降息 ! 暫緩,不急,不會,不降息,難降息,推遲,延後,延遲,降溫,削弱,減弱,轉弱 => 高科技利多, 利率敏感股利多",
    "升息,加息 ! 暫緩,不會,不再,結束,停止,降溫,削弱,減弱 => 高科技利空, 利率敏感股利空",
    "# 黃金",
    "黃金,gold price,gold prices + 大漲,上漲,飆漲,暴漲,創高,創新高,新高,surge,soar,jump,rally,record high => 黃金利多",
    "黃金,gold price,gold prices + 大跌,下跌,暴跌,重挫,崩跌,新低,plunge,tumble,slump,crash => 黃金利空",
    "# 被動元件（MLCC、電阻、電容這類，缺貨、漲價的消息常常一次影響整個族群）",
    "被動元件,MLCC,積層陶瓷電容,晶片電阻,鉭質電容 + 大漲,上漲,飆漲,暴漲,創高,創新高,新高,surge,soar,jump,rally,record high,漲價,缺貨,供不應求 => 被動元件族群利多",
    "被動元件,MLCC,積層陶瓷電容,晶片電阻,鉭質電容 + 大跌,下跌,暴跌,重挫,崩跌,新低,plunge,tumble,slump,crash,產能過剩,供過於求 => 被動元件族群利空",
]

DEFAULT_CONFIG = {
    "settings": DEFAULT_SETTINGS,
    "watchlist": [
        {"name": "台積電", "market": "台股", "keywords": ["台積電", "2330", "TSMC"], "google": ["tw", "us"], "boost": 0},
        {"name": "聯發科", "market": "台股", "keywords": ["聯發科", "2454", "MediaTek"], "google": ["us", "tw"], "boost": 0},
        {"name": "鴻海", "market": "台股", "keywords": ["鴻海", "2317", "Foxconn", "富士康"], "google": ["us", "tw"], "boost": 0},
        {"name": "台指期", "market": "期貨/外匯", "keywords": ["台指期", "台指選擇權", "外資期貨", "期交所"], "google": ["us", "tw"], "boost": 0, "direction": False},
        {"name": "NVIDIA", "market": "美股", "keywords": ["NVIDIA", "NVDA", "輝達", "英偉達"], "google": ["us", "tw"], "boost": 0},
        {"name": "Tesla", "market": "美股", "keywords": ["Tesla", "TSLA", "特斯拉"], "google": ["us", "tw"], "boost": 0},
        {"name": "Apple", "market": "美股", "keywords": ["Apple", "AAPL", "蘋果公司"], "google": ["us", "tw"], "boost": 0},
        {"name": "Bitcoin", "market": "加密貨幣", "keywords": ["Bitcoin", "BTC", "比特幣"], "google": ["us", "tw"], "boost": 0},
        {"name": "Ethereum", "market": "加密貨幣", "keywords": ["Ethereum", "ETH", "以太幣", "以太坊"], "google": ["us", "tw"], "boost": 0},
        {"name": "美元/日圓", "market": "期貨/外匯", "keywords": ["USD/JPY", "美元日圓", "日圓", "日元", "日本央行", "BOJ"], "google": ["us", "tw"], "boost": 0, "direction": False},
        {"name": "黃金", "market": "期貨/外匯", "keywords": ["黃金", "gold price", "XAU"], "google": ["us", "tw"], "boost": 0, "commodity": True},
        {"name": "原油", "market": "期貨/外匯", "keywords": ["原油", "石油", "油價", "crude oil", "WTI", "Brent", "布蘭特", "OPEC"], "google": ["us", "tw"], "boost": 0, "commodity": True},
        {"name": "總經與央行", "market": "總經", "keywords": ["Fed", "FOMC", "聯準會", "美聯儲", "CPI", "非農", "降息", "升息", "鮑威爾", "Powell", "央行理監事會"], "google": ["us", "tw"], "boost": 0, "direction": False},

        # ---- 全球政經重要人物（查證時間：2026-09-28，之後任職異動需自行更新）----
        {"name": "美國總統", "market": "政經人物", "keywords": ["川普", "Trump", "特朗普"], "google": ["us", "tw"], "boost": 0},
        {"name": "美國副總統", "market": "政經人物", "keywords": ["范斯", "JD Vance", "凡斯"], "google": ["us", "tw"], "boost": 0},
        {"name": "美國財政部長", "market": "政經人物", "keywords": ["貝森特", "Bessent", "Scott Bessent"], "google": ["us", "tw"], "boost": 0},
        {"name": "聯準會主席", "market": "政經人物", "keywords": ["Warsh", "沃爾什", "聯準會主席", "Fed主席"], "google": ["us", "tw"], "boost": 0},
        {"name": "Fed官員", "market": "政經人物",
         "keywords": ["Jefferson", "傑弗遜", "Waller", "沃勒", "Bowman", "鮑曼", "Lisa Cook", "庫克",
                      "Barr", "巴爾", "Williams", "威廉斯", "Hammack", "哈馬克", "Logan", "洛根",
                      "Kashkari", "卡什卡里", "Paulson", "鮑爾森", "FOMC票委", "Fed官員", "Fed理事", "聯準會官員", "地區聯準銀行總裁"],
         "google": ["us", "tw"], "boost": 0, "person": True},
        {"name": "美國商務部長", "market": "政經人物", "keywords": ["盧特尼克", "Lutnick"], "google": ["us", "tw"], "boost": 0},
        {"name": "美國貿易代表", "market": "政經人物", "keywords": ["葛里爾", "Greer", "USTR"], "google": ["us", "tw"], "boost": 0},
        {"name": "白宮國家經濟委員會主席", "market": "政經人物", "keywords": ["哈塞特", "Hassett", "國家經濟委員會"], "google": ["us", "tw"], "boost": 0},
        {"name": "日本首相", "market": "政經人物", "keywords": ["高市早苗", "Takaichi"], "google": ["us", "tw"], "boost": 0},
        {"name": "日本財務大臣", "market": "政經人物", "keywords": ["片山さつき", "片山皋月", "Katayama"], "google": ["us", "tw"], "boost": 0},
        {"name": "日本央行總裁", "market": "政經人物", "keywords": ["植田和男", "Ueda", "日銀總裁", "日本央行總裁"], "google": ["us", "tw"], "boost": 0},
        {"name": "歐洲央行總裁", "market": "政經人物", "keywords": ["拉加德", "Lagarde", "歐洲央行總裁", "ECB總裁"], "google": ["us", "tw"], "boost": 0},
        {"name": "歐盟執委會主席", "market": "政經人物", "keywords": ["馮德萊恩", "von der Leyen"], "google": ["us", "tw"], "boost": 0},
        {"name": "中國國務院副總理", "market": "政經人物", "keywords": ["何立峰"], "google": ["us", "tw"], "boost": 0},
        {"name": "中國人行行長", "market": "政經人物", "keywords": ["潘功勝", "人行行長", "央行行長", "PBOC"], "google": ["us", "tw"], "boost": 0},
        {"name": "中國財政部長", "market": "政經人物", "keywords": ["藍佛安"], "google": ["us", "tw"], "boost": 0},
        {"name": "中國商務部長", "market": "政經人物", "keywords": ["王文濤"], "google": ["us", "tw"], "boost": 0},

        # ---- 法人報告：外資、本土券商對台股／美股的觀點與評等。這些不是「可以漲跌的標的」，
        # 是消息來源，所以不自動標多空（同一個道理跟政經人物一樣：「大摩上修財測」講的是別家公司的財測，
        # 不是大摩自己的股價，硬要標「大摩利多」反而會誤導）。本土券商的關鍵字刻意用「XX證券／XX投顧」，
        # 不用單獨「元大」「國泰」這種字，因為那些字本身也是金控、人壽等其他業務常用字，會抓進一堆無關新聞。
        {"name": "大摩", "market": "法人報告", "keywords": ["大摩", "摩根士丹利"], "google": ["us", "tw"], "boost": 0},
        {"name": "小摩", "market": "法人報告", "keywords": ["小摩", "摩根大通"], "google": ["us", "tw"], "boost": 0},
        {"name": "花旗", "market": "法人報告", "keywords": ["花旗"], "google": ["us", "tw"], "boost": 0},
        {"name": "高盛", "market": "法人報告", "keywords": ["高盛"], "google": ["us", "tw"], "boost": 0},
        {"name": "瑞銀", "market": "法人報告", "keywords": ["瑞銀"], "google": ["us", "tw"], "boost": 0},
        {"name": "美銀美林", "market": "法人報告", "keywords": ["美銀美林"], "google": ["us", "tw"], "boost": 0},
        {"name": "大和資本", "market": "法人報告", "keywords": ["大和資本", "大和證券"], "google": ["us", "tw"], "boost": 0},
        {"name": "野村", "market": "法人報告", "keywords": ["野村證券", "野村控股", "野村國際"], "google": ["us", "tw"], "boost": 0},
        {"name": "法巴", "market": "法人報告", "keywords": ["法巴", "法國巴黎銀行"], "google": ["us", "tw"], "boost": 0},
        {"name": "凱基", "market": "法人報告", "keywords": ["凱基證券", "凱基投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "元大", "market": "法人報告", "keywords": ["元大證券", "元大投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "富邦證券", "market": "法人報告", "keywords": ["富邦證券", "富邦投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "國泰證券", "market": "法人報告", "keywords": ["國泰證券", "國泰投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "統一證券", "market": "法人報告", "keywords": ["統一證券", "統一投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "群益證券", "market": "法人報告", "keywords": ["群益證券", "群益投顧"], "google": ["us", "tw"], "boost": 0},
        {"name": "兆豐證券", "market": "法人報告", "keywords": ["兆豐證券", "兆豐投顧"], "google": ["us", "tw"], "boost": 0},

        # ---- 七巨頭 CEO／財務長（查證時間：2026-09-28）；沒找到對應的「投資長」職位，先以 CEO／CFO 為主 ----
        # 「庫克」不放進來：聯準會理事 Lisa Cook 也叫這個中文姓，單獨兩個字會撞到，
        # 保留「提姆庫克」這種更完整的寫法就好。
        {"name": "Apple高層", "market": "美股", "keywords": ["John Ternus", "Ternus", "泰納斯", "Tim Cook", "提姆庫克", "Kevan Parekh", "Parekh"], "google": ["us", "tw"], "boost": 0},
        {"name": "Microsoft高層", "market": "美股", "keywords": ["Satya Nadella", "Nadella", "納德拉", "Amy Hood"], "google": ["us", "tw"], "boost": 0},
        {"name": "Alphabet高層", "market": "美股", "keywords": ["Sundar Pichai", "Pichai", "皮查伊", "Anat Ashkenazi", "Ashkenazi"], "google": ["us", "tw"], "boost": 0},
        {"name": "Amazon高層", "market": "美股", "keywords": ["Andy Jassy", "Jassy", "賈西", "Brian Olsavsky", "Olsavsky"], "google": ["us", "tw"], "boost": 0},
        {"name": "NVIDIA高層", "market": "美股", "keywords": ["Jensen Huang", "黃仁勳", "Colette Kress", "Kress"], "google": ["us", "tw"], "boost": 0},
        {"name": "Meta高層", "market": "美股", "keywords": ["Mark Zuckerberg", "Zuckerberg", "祖克柏", "Susan Li"], "google": ["us", "tw"], "boost": 0},
        {"name": "Tesla高層", "market": "美股", "keywords": ["Elon Musk", "Musk", "馬斯克", "Vaibhav Taneja", "Taneja"], "google": ["us", "tw"], "boost": 0},
    ],
    "feeds": [
        {"name": "證交所 重大訊息（上市）", "url": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "type": "twse_json", "market": "台股", "keep_all": False, "official": True, "enabled": True},
        {"name": "櫃買中心 重大訊息（上櫃）", "url": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "type": "twse_json", "market": "台股", "keep_all": False, "official": True, "enabled": True},
        {"name": "PTT 股票版", "url": "https://www.ptt.cc/atom/Stock.xml", "type": "rss", "market": "台股", "keep_all": False, "official": False, "enabled": True},
        {"name": "自由時報 財經", "url": "https://news.ltn.com.tw/rss/business.xml", "type": "rss", "market": "台股", "keep_all": False, "official": False, "enabled": True},
        {"name": "SEC 即時公告 8-K", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom", "type": "rss", "market": "美股", "keep_all": False, "official": True, "enabled": True, "user_agent": "TradingNewsMonitor your-email@example.com"},
        {"name": "CNBC 財經", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "type": "rss", "market": "美股", "keep_all": True, "official": False, "enabled": True},
        {"name": "CNBC 頭條", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "type": "rss", "market": "美股", "keep_all": True, "official": False, "enabled": True},
        {"name": "MarketWatch 頭條", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "type": "rss", "market": "美股", "keep_all": True, "official": False, "enabled": True},
        {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "type": "rss", "market": "美股", "keep_all": False, "official": False, "enabled": True},
        {"name": "聯準會 新聞稿", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "type": "rss", "market": "總經", "keep_all": True, "official": True, "enabled": True},
        {"name": "Investing.com 外匯", "url": "https://www.investing.com/rss/news_1.rss", "type": "rss", "market": "期貨/外匯", "keep_all": True, "official": False, "enabled": True},
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "type": "rss", "market": "加密貨幣", "keep_all": True, "official": False, "enabled": True},
        {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "type": "rss", "market": "加密貨幣", "keep_all": True, "official": False, "enabled": True},
        {"name": "Decrypt", "url": "https://decrypt.co/feed", "type": "rss", "market": "加密貨幣", "keep_all": True, "official": False, "enabled": True},
        # 下面三家財經媒體沒有自己對外開放的 RSS（查證時間：2026-09，2026-10 再次確認過，
        # 搜尋不到官方 RSS，Google 新聞的 site: 搜尋是唯一可行的替代方案），
        # 改用 Google 新聞的「site: 限定網站」搜尋來代替，效果接近直接訂閱。
        # when 開 4 天（不是 2 天）：Google 收錄這幾家網站的文章常常會晚個 1~2 天才出現在
        # site: 搜尋結果裡（不是文章本身晚發布，是 Google 晚索引到），視窗開太短會讓那些文章
        # 永遠趕不上（等 Google 索引到的時候，已經「超過」when 的範圍，變成被排除、而不是被收錄）。
        # 開 4 天不會洗版：抓到的候選一律按時間排序只留最新 60 則（max_items_per_source），
        # 多留的候選只是讓真正最新的那些有機會不被「Google 還沒索引到」卡住，不會變多顯示。
        {"name": "經濟日報（經 Google 新聞）", "url": site_news_url("money.udn.com", "4d"), "type": "rss", "market": "台股", "keep_all": True, "official": False, "enabled": True},
        {"name": "工商時報（經 Google 新聞）", "url": site_news_url("ctee.com.tw", "4d"), "type": "rss", "market": "台股", "keep_all": True, "official": False, "enabled": True},
        {"name": "鉅亨網（經 Google 新聞）", "url": site_news_url("news.cnyes.com"), "type": "rss", "market": "台股", "keep_all": True, "official": False, "enabled": True},
        # 華爾街見聞：Google 新聞台灣版沒有收錄，改用它網站自己的資料接口。標題是簡體字。
        # limit 開 50（原本 25）：同樣是為了在流量發得比較快的時段，減少漏接的機會。
        {"name": "華爾街見聞", "url": "https://api-one.wallstcn.com/apiv1/content/information-flow?channel=global&accept=article&limit=50", "type": "wscn_json", "market": "總經", "keep_all": True, "official": False, "enabled": True},
        # 東方財富：官網沒有公開的 RSS。首頁本身內嵌一個「快訊」小工具（id="news_kx_c"），
        # 每次大約有 10 則帶時間的即時新聞，用專用解析器抓。首頁比一般 RSS 大（約 300~400KB），
        # 5 分鐘抓一次的流量會比其他來源略高一些；標題是簡體字。
        {"name": "東方財富（快訊）", "url": "https://www.eastmoney.com/default.html", "type": "eastmoney_kx", "market": "總經", "keep_all": True, "official": False, "enabled": True},
        # 首頁上「東方視點／焦點專題」「財經導讀／7x24快訊」這些一般文章，數量比較多（一次上百則），
        # 大多是中國內需股新聞，跟這份預設清單的關注標的不一定相關，所以刻意不勾「收錄全部」——
        # 只有比對到你關注標的的才會留下來，避免洗版；想要全收，去設定裡把「收錄全部」打開。
        # 這裡沒有每篇文章的確切發布時間，記錄的是程式抓到它的時間，可能跟實際發布時間差幾小時。
        {"name": "東方財富（焦點新聞）", "url": "https://www.eastmoney.com/default.html", "type": "eastmoney_articles", "market": "總經", "keep_all": False, "official": False, "enabled": True},
        # MoneyDJ 理財網：桌面版首頁是廣告/工具頁，真正的新聞要 JavaScript 動態載入，抓不到；
        # 手機版（m.moneydj.com）是伺服器端直接輸出，可以直接抓。只抓「頭條新聞」這個分類
        # （涵蓋範圍最廣，混合台股、美股、總經、商品原物料），其他分類（深滬港股、基金新聞…）
        # 要另外加來源，網址把 a=MB010000 換成別的分類代碼即可。
        {"name": "MoneyDJ理財網", "url": "https://m.moneydj.com/newsList.aspx?a=MB010000", "type": "moneydj", "market": "總經", "keep_all": True, "official": False, "enabled": True},
        # 富途牛牛「要聞」：官網沒有公開 RSS，但首頁改版後的頁面是 Nuxt.js 應用程式，實際資料是從它
        # 自己的 JSON 接口來的（瀏覽器開發者工具的網路請求裡看得到），不用解析內嵌在頁面裡、
        # 被壓縮過變數名稱的 Nuxt 狀態（那份很難穩定解析，一壓縮方式改版就會壞）。
        # 這是「要聞」聚合了多家原始來源（金十數據、財聯社、智通財經APP、中信證券研究等）
        # 混在一起的清單，所以每則新聞的 source 用它接口裡附的原始出處，不是整個寫死「富途牛牛」，
        # 這樣才不會把其中混到的「華爾街見聞」稿件，跟既有的「華爾街見聞」來源分開計算、漏算重要消息來源。
        # 接口回應裡有一個 honeyPotList 欄位（網址裡混了一個假的 id，點進去會暴露是程式在抓）：
        # 解析器只讀 list，完全不碰 honeyPotList，見 fetcher.py 的 parse_futunn_headlines()。
        # 標題是簡體字（原始出處大多是中國財經媒體）。
        {"name": "富途牛牛（要聞）", "url": "https://news.futunn.com/news-site-api/main/get-market-list?size=30&isSupportWebp=true", "type": "futunn_headlines", "market": "總經", "keep_all": True, "official": False, "enabled": True},
    ],
    "importance_words": DEFAULT_IMPORTANCE_WORDS,
    "positive_words": DEFAULT_POSITIVE_WORDS,
    "negative_words": DEFAULT_NEGATIVE_WORDS,
    "neutral_words": DEFAULT_NEUTRAL_WORDS,
    "sector_rules": DEFAULT_SECTOR_RULES,
    "noise_phrases": DEFAULT_NOISE_PHRASES,
    "commodity_context": DEFAULT_COMMODITY_CONTEXT,
    "institution_context": DEFAULT_INSTITUTION_CONTEXT,
    "institution_exclude": DEFAULT_INSTITUTION_EXCLUDE,
    "person_context": DEFAULT_PERSON_CONTEXT,
    "finance_context": DEFAULT_FINANCE_CONTEXT,
    "title_exclude": DEFAULT_TITLE_EXCLUDE,
    "source_exclude": DEFAULT_SOURCE_EXCLUDE,
    "important_sources": DEFAULT_IMPORTANT_SOURCES,
}

VALID_EDITIONS = ("tw", "us")


class ConfigError(Exception):
    pass


def _int(v, default, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _float(v, default, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def split_keywords(v):
    if isinstance(v, str):
        v = re.split(r"[,\n，、;；]+", v)
    out, seen = [], set()
    for k in v or []:
        k = str(k).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def normalize_google(v):
    """'auto' | False | ['tw','us']"""
    if v is None or v is True or v == "auto":
        return "auto"
    if v is False or v == "off" or v == "":
        return False
    if isinstance(v, str):
        v = re.split(r"[+,\s]+", v)
    eds = [e for e in v if e in VALID_EDITIONS]
    return eds if eds else False


def item_keywords(item):
    """關注標的的名稱本身也視為關鍵字。"""
    kws = [item["name"]]
    for k in item.get("keywords", []):
        if k.lower() not in [x.lower() for x in kws]:
            kws.append(k)
    return kws


def _direction(it):
    """這個標的要不要用漲跌用字自動標示「○○利多／利空」。沒設定過時：個股與加密貨幣預設開，
    匯率、期貨、總經這類方向不明確的預設關。"""
    v = it.get("direction")
    if v is not None:
        return bool(v)
    market = str(it.get("market", "")).strip()
    return market in ("台股", "美股", "加密貨幣") or str(it.get("name", "")).strip() in ("黃金", "原油")


def _commodity(it):
    """是否為「商品」類標的（新聞標題要有行情用詞才算相關）。沒設定過時：黃金、原油預設開。"""
    v = it.get("commodity")
    if v is not None:
        return bool(v)
    return str(it.get("name", "")).strip() in ("黃金", "原油")


def flatten_noise(lines):
    """把 noise_phrases 的每一行拆成單一詞組（略過空行與 # 註解）。"""
    out = []
    for line in lines or []:
        line = str(line).strip()
        if line and not line.startswith("#"):
            out += [w.strip() for w in re.split(r"[,，、]", line) if w.strip()]
    return out


def normalize(raw):
    if not isinstance(raw, dict):
        raise ConfigError("設定檔最外層必須是 JSON 物件 { ... }")
    cfg = {}

    s = dict(DEFAULT_SETTINGS)
    s.update(raw.get("settings") or {})
    s["port"] = _int(s.get("port"), 8765, 1024, 65535)
    s["refresh_minutes"] = _int(s.get("refresh_minutes"), 5, 1, 120)
    s["keep_days"] = _int(s.get("keep_days"), 14, 1, 365)
    s["request_timeout"] = _int(s.get("request_timeout"), 15, 3, 120)
    s["max_items_per_source"] = _int(s.get("max_items_per_source"), 60, 5, 500)
    s["dedup_similarity"] = _float(s.get("dedup_similarity"), 0.72, 0.5, 1.0)
    for b in ("open_browser", "sentiment_hint", "verify_ssl", "translate", "images",
             "positive_word_search", "negative_word_search", "neutral_word_search"):
        s[b] = bool(s.get(b, DEFAULT_SETTINGS[b]))
    ai = dict(DEFAULT_SETTINGS["ai_digest"])
    ai.update(raw.get("settings", {}).get("ai_digest") or {})
    ai["api_key"] = str(ai.get("api_key") or "").strip()
    ai["model"] = str(ai.get("model") or DEFAULT_SETTINGS["ai_digest"]["model"]).strip() or DEFAULT_SETTINGS["ai_digest"]["model"]
    ai["digest_hours"] = _int(ai.get("digest_hours"), 12, 1, 24 * 7)
    s["ai_digest"] = ai
    fw = dict(DEFAULT_SETTINGS["focus_weights"])
    fw.update(raw.get("settings", {}).get("focus_weights") or {})
    for k in ("source", "importance", "institution", "net_signal"):
        fw[k] = _int(fw.get(k), DEFAULT_SETTINGS["focus_weights"][k], 0, 100)
    s["focus_weights"] = fw
    if s.get("images_mode") not in ("tables", "all"):
        s["images_mode"] = "tables"
    s["page_user_agent"] = str(s.get("page_user_agent") or DEFAULT_SETTINGS["page_user_agent"]).strip()
    if not re.match(r"^https?://", str(s.get("translate_endpoint", "")), re.I):
        s["translate_endpoint"] = DEFAULT_SETTINGS["translate_endpoint"]
    s["user_agent"] = str(s.get("user_agent") or DEFAULT_SETTINGS["user_agent"]).strip()
    cfg["settings"] = s

    wl, seen = [], set()
    for it in raw.get("watchlist") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        wl.append({
            "name": name,
            "market": str(it.get("market", "")).strip(),
            "keywords": split_keywords(it.get("keywords")),
            "google": normalize_google(it.get("google", "auto")),
            "boost": _int(it.get("boost"), 0, 0, 2),
            "direction": _direction(it),
            "commodity": _commodity(it),
            "institution": bool(it.get("institution")) or str(it.get("market", "")).strip() == "法人報告",
            "person": bool(it.get("person")),
        })
    cfg["watchlist"] = wl

    feeds, names = [], set()
    for f in raw.get("feeds") or []:
        if not isinstance(f, dict):
            continue
        url = str(f.get("url", "")).strip()
        url = _FEED_URL_MIGRATIONS.get(url, url)   # 已知舊網址的一次性代換，見上面的說明
        if not re.match(r"^https?://", url, re.I):
            continue
        name = str(f.get("name", "")).strip() or url
        base, n = name, 2
        while name in names:
            name = "%s (%d)" % (base, n)
            n += 1
        names.add(name)
        nf = {
            "name": name,
            "url": url,
            # 華爾街見聞、東方財富的網址一律用專用解析器（讓使用者在設定畫面當成一般來源貼上就能用）
            # 已經明確指定過我們認得的類型（例如寫在預設清單裡的東方財富兩筆，同一個網址但類型不同）
            # 就照那個走；只有「沒指定」或還是通用的 rss 時，才用網址規律自動判斷。
            "type": (f.get("type") if f.get("type") in ("twse_json", "wscn_json", "eastmoney_kx", "eastmoney_articles", "moneydj", "futunn_headlines")
                    else "wscn_json" if re.match(r"^https?://api-one\.wallstcn\.com/", url, re.I)
                    else "eastmoney_kx" if re.match(r"^https?://(www\.)?eastmoney\.com/", url, re.I)
                    else "moneydj" if re.match(r"^https?://m\.moneydj\.com/", url, re.I)
                    else "futunn_headlines" if re.match(r"^https?://news\.futunn\.com/news-site-api/", url, re.I)
                    else "rss"),
            "market": str(f.get("market", "")).strip(),
            "keep_all": bool(f.get("keep_all", False)),
            "official": bool(f.get("official", False)),
            "enabled": bool(f.get("enabled", True)),
        }
        if f.get("user_agent"):
            nf["user_agent"] = str(f["user_agent"]).strip()
        if f.get("finance_context_required"):
            nf["finance_context_required"] = True
        feeds.append(nf)
    cfg["feeds"] = feeds

    iw = raw.get("importance_words")
    if isinstance(iw, dict):
        cfg["importance_words"] = {str(k).strip(): _int(v, 1, 1, 3) for k, v in iw.items() if str(k).strip()}
    else:
        cfg["importance_words"] = dict(DEFAULT_IMPORTANCE_WORDS)
    sr = raw.get("sector_rules")
    if isinstance(sr, str):
        sr = sr.splitlines()
    cfg["sector_rules"] = [str(x).strip() for x in sr if str(x).strip()] if isinstance(sr, list) else list(DEFAULT_SECTOR_RULES)
    tx = raw.get("title_exclude")
    if isinstance(tx, str):
        tx = tx.splitlines()
    cfg["title_exclude"] = [str(x).strip() for x in tx if str(x).strip()] if isinstance(tx, list) else list(DEFAULT_TITLE_EXCLUDE)
    sx = raw.get("source_exclude")
    if isinstance(sx, str):
        sx = sx.splitlines()
    cfg["source_exclude"] = [str(x).strip() for x in sx if str(x).strip()] if isinstance(sx, list) else list(DEFAULT_SOURCE_EXCLUDE)
    isr = raw.get("important_sources")
    if isinstance(isr, str):
        isr = isr.splitlines()
    cfg["important_sources"] = [str(x).strip() for x in isr if str(x).strip()] if isinstance(isr, list) else list(DEFAULT_IMPORTANT_SOURCES)
    nz = raw.get("noise_phrases")
    if isinstance(nz, str):
        nz = nz.splitlines()
    cfg["noise_phrases"] = [str(x).strip() for x in nz if str(x).strip()] if isinstance(nz, list) else list(DEFAULT_NOISE_PHRASES)
    cx = raw.get("commodity_context")
    cfg["commodity_context"] = split_keywords(cx) if isinstance(cx, (list, str)) else list(DEFAULT_COMMODITY_CONTEXT)
    ix = raw.get("institution_context")
    cfg["institution_context"] = split_keywords(ix) if isinstance(ix, (list, str)) else list(DEFAULT_INSTITUTION_CONTEXT)
    iex = raw.get("institution_exclude")
    cfg["institution_exclude"] = split_keywords(iex) if isinstance(iex, (list, str)) else list(DEFAULT_INSTITUTION_EXCLUDE)
    px = raw.get("person_context")
    cfg["person_context"] = split_keywords(px) if isinstance(px, (list, str)) else list(DEFAULT_PERSON_CONTEXT)
    fx = raw.get("finance_context")
    cfg["finance_context"] = split_keywords(fx) if isinstance(fx, (list, str)) else list(DEFAULT_FINANCE_CONTEXT)
    for key, default in (("positive_words", DEFAULT_POSITIVE_WORDS), ("negative_words", DEFAULT_NEGATIVE_WORDS),
                         ("neutral_words", DEFAULT_NEUTRAL_WORDS)):
        v = raw.get(key)
        cfg[key] = split_keywords(v) if isinstance(v, (list, str)) else list(default)
    return cfg


def load_config(path):
    if not os.path.exists(path):
        cfg = normalize(copy.deepcopy(DEFAULT_CONFIG))
        save_config(path, cfg)
        return cfg
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(
            "config.json 格式有誤（第 %d 行、第 %d 個字附近）：%s\n"
            "常見原因：多了或少了逗號、引號不是半形的 \"。修不好的話，刪除 config.json 再重新啟動即可恢復預設。"
            % (e.lineno, e.colno, e.msg))
    return normalize(raw)


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def market_list(cfg):
    found = []
    for it in cfg.get("watchlist", []):
        if it.get("market"):
            found.append(it["market"])
    for f in cfg.get("feeds", []):
        if f.get("market"):
            found.append(f["market"])
    ordered = [m for m in MARKET_ORDER if m in found]
    ordered += [m for m in dict.fromkeys(found) if m not in ordered]
    return ordered

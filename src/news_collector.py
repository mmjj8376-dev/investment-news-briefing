import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

HOURS_BACK = 24

OUTPUT_DIR = Path("output")
AI_INPUT_PATH = OUTPUT_DIR / "ai_input.json"
COPILOT_PROMPT_PATH = OUTPUT_DIR / "copilot_prompt.txt"
FALLBACK_BRIEFING_PATH = OUTPUT_DIR / "fallback_briefing.md"
FINAL_BRIEFING_PATH = OUTPUT_DIR / "briefing.md"
WATCHLIST_PATH = Path("config/watchlist.json")

DEFAULT_STOCKS = [
    {"ticker": "AAPL", "company_name": "Apple"},
    {"ticker": "AVGO", "company_name": "Broadcom"},
    {"ticker": "GOOGL", "company_name": "Alphabet"},
    {"ticker": "LLY", "company_name": "Eli Lilly"},
    {"ticker": "META", "company_name": "Meta Platforms"},
    {"ticker": "MSFT", "company_name": "Microsoft"},
    {"ticker": "NVDA", "company_name": "NVIDIA"},
    {"ticker": "TSLA", "company_name": "Tesla"},
]

LOW_VALUE_PHRASES = [
    "is it too late to buy", "is it time to buy", "is it time to sell",
    "should you buy", "should you sell", "is a good buy",
    "still a good buy", "best stocks to buy", "stocks to buy now",
    "where will", "price prediction", "stock prediction", "could soar",
    "could skyrocket", "millionaire maker",
]

HIGH_VALUE_TERMS = [
    "earnings", "revenue", "guidance", "forecast", "acquisition", "acquire",
    "merger", "partnership", "contract", "deal", "financing", "funding",
    "investment", "sec", "lawsuit", "antitrust", "regulator",
    "investigation", "ceo", "cfo", "dividend", "buyback", "recall",
]

SOURCE_PRIORITY = {
    "sec": 100, "newsroom": 98, "investor relations": 98, "nvidia": 98,
    "apple": 98, "microsoft": 98, "alphabet": 98, "google": 98,
    "meta": 98, "broadcom": 98, "tesla": 98, "eli lilly": 98,
    "reuters": 95, "bloomberg": 94, "wall street journal": 93, "wsj": 93,
    "financial times": 92, "new york times": 90, "nytimes": 90,
    "cnbc": 88, "barron's": 87, "marketwatch": 86, "semafor": 85,
    "axios": 85, "associated press": 84, "forbes": 75,
    "business insider": 74, "yahoo": 65, "investing.com": 60,
    "tradingview": 55, "thestreet": 50, "stocktwits": 45, "moomoo": 40,
}

EVENT_TERMS = [
    "financing", "funding", "partnership", "deal", "contract", "acquisition",
    "merger", "investment", "earnings", "revenue", "guidance", "lawsuit",
    "antitrust", "investigation", "buyback", "dividend", "ceo", "cfo",
]

EARNINGS_TERMS = {
    "earnings", "revenue", "sales", "guidance", "forecast", "margin",
    "profit", "profits", "eps", "cash flow", "free cash flow", "capex",
    "order", "orders", "backlog", "shipment", "shipments",
}
INDUSTRY_TERMS = {
    "ai", "artificial intelligence", "semiconductor", "chip", "chips",
    "data center", "datacenter", "cloud", "gpu", "accelerator", "regulation",
    "regulator", "antitrust", "export", "tariff", "supply chain", "foundry",
    "manufacturing",
}
MARKET_IMPACT_TERMS = {
    "acquisition", "merger", "buyout", "partnership", "contract", "deal",
    "financing", "funding", "investment", "investigation", "lawsuit",
    "antitrust", "recall", "ban", "approval", "approved", "ceo", "cfo",
    "buyback", "dividend", "guidance",
}
POSITIVE_TERMS = {
    "beats", "beat", "raises guidance", "raise guidance", "record", "wins",
    "win", "approval", "approved", "expands", "expansion", "partnership",
    "contract", "buyback", "dividend increase", "revenue growth",
    "profit growth", "strong demand", "funding", "financing", "investment",
}
NEGATIVE_TERMS = {
    "misses", "miss", "cuts guidance", "cut guidance", "downgrade",
    "lawsuit", "investigation", "antitrust", "recall", "ban",
    "export restriction", "export restrictions", "weak demand", "decline",
    "falls", "fall", "drops", "drop", "plunges", "plunge", "resigns",
    "resign", "delay", "delays",
}

def load_stock_watchlist():
    name_map = {x["ticker"]: x["company_name"] for x in DEFAULT_STOCKS}
    if not WATCHLIST_PATH.exists():
        return DEFAULT_STOCKS

    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Watchlist parse failed: {exc}")
        return DEFAULT_STOCKS

    items = data.get("stocks", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return DEFAULT_STOCKS

    stocks = []
    for item in items:
        if isinstance(item, str):
            ticker = item.upper().strip()
            if ticker in name_map:
                stocks.append({"ticker": ticker, "company_name": name_map[ticker]})
        elif isinstance(item, dict):
            ticker = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
            if not ticker:
                continue
            company_name = item.get("company_name") or item.get("name") or name_map.get(ticker) or ticker
            stocks.append({"ticker": ticker, "company_name": company_name})
    return stocks or DEFAULT_STOCKS

def get_previous_market_move(ticker):
    encoded = urllib.parse.quote(ticker)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{encoded}?range=10d&interval=1d&includePrePost=false&events=div%2Csplits"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        usable = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            usable.append({
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
                "close": float(close),
            })
        if len(usable) < 2:
            return None
        prev, latest = usable[-2], usable[-1]
        return {
            "previous_date": prev["date"],
            "previous_close": prev["close"],
            "latest_date": latest["date"],
            "latest_close": latest["close"],
            "change_pct": (latest["close"] / prev["close"] - 1) * 100,
        }
    except Exception as exc:
        print(f"{ticker} price data unavailable: {exc}")
        return None

def get_google_news(ticker, company_name):
    query = urllib.parse.quote(f'"{company_name}" OR "{ticker}"')
    url = (
        "https://news.google.com/rss/search"
        f"?q={query}&hl=en-US&gl=US&ceid=US:en"
    )
    print(f"\nSearching news for {ticker} ({company_name})...")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    articles = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        pub_date = item.findtext("pubDate", "").strip()
        source_element = item.find("source")
        source = (
            source_element.text.strip()
            if source_element is not None and source_element.text
            else "Unknown"
        )
        try:
            published = parsedate_to_datetime(pub_date)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if published >= cutoff:
            articles.append({
                "title": title,
                "source": source,
                "published": published,
                "link": link,
            })
    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    for old, new in {
        "$500b": "500 billion", "$500bn": "500 billion",
        "half-trillion": "500 billion", "half trillion": "500 billion",
    }.items():
        title = title.replace(old, new)
    title = re.sub(r"\$([0-9,.]+)\s*billion", r"\1 billion", title)
    title = re.sub(r"\$([0-9,.]+)\s*million", r"\1 million", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()

def tokenize(title):
    stopwords = {
        "the", "a", "an", "and", "or", "to", "for", "of", "on", "in",
        "with", "as", "at", "from", "by", "is", "are", "be", "its",
        "this", "that", "after", "amid", "new", "says",
    }
    return {w for w in normalize_title(title).split() if len(w) >= 3 and w not in stopwords}

def source_score(source):
    source_lower = source.lower()
    for key, score in SOURCE_PRIORITY.items():
        if key in source_lower:
            return score
    return 50

def is_low_value_article(title):
    normalized = normalize_title(title)
    if any(term in normalized for term in HIGH_VALUE_TERMS):
        return False
    return any(phrase in normalized for phrase in LOW_VALUE_PHRASES)

def filter_low_value_articles(articles):
    kept, removed = [], []
    for article in articles:
        (removed if is_low_value_article(article["title"]) else kept).append(article)
    return kept, removed

def title_similarity(a, b):
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()

def remove_near_duplicates(articles, threshold=0.82):
    unique_articles, duplicates = [], []
    for article in articles:
        duplicate_index = None
        for index, existing in enumerate(unique_articles):
            if title_similarity(article["title"], existing["title"]) >= threshold:
                duplicate_index = index
                break
        if duplicate_index is None:
            unique_articles.append(article)
            continue
        existing = unique_articles[duplicate_index]
        if source_score(article["source"]) > source_score(existing["source"]):
            unique_articles[duplicate_index] = article
            duplicates.append({"article": existing, "kept": article})
        else:
            duplicates.append({"article": article, "kept": existing})
    return unique_articles, duplicates

def event_similarity(a, b):
    ta, tb = tokenize(a["title"]), tokenize(b["title"])
    if not ta or not tb:
        return 0
    inter, union = ta & tb, ta | tb
    jaccard = len(inter) / len(union)
    na, nb = normalize_title(a["title"]), normalize_title(b["title"])
    shared_event = any(term in na and term in nb for term in EVENT_TERMS)
    shared_big_number = "500 billion" in na and "500 billion" in nb
    if shared_big_number:
        return max(jaccard, 0.75)
    if shared_event and len(inter) >= 3:
        return max(jaccard, 0.60)
    return jaccard

def cluster_events(articles, threshold=0.48):
    clusters = []
    for article in articles:
        matched = None
        for cluster in clusters:
            if event_similarity(article, cluster["representative"]) >= threshold:
                matched = cluster
                break
        if matched is None:
            clusters.append({"representative": article, "articles": [article]})
            continue
        matched["articles"].append(article)
        if source_score(article["source"]) > source_score(matched["representative"]["source"]):
            matched["representative"] = article
    return clusters

def cluster_merge_score(a, b):
    return max(
        (event_similarity(x, y) for x in a["articles"] for y in b["articles"]),
        default=0,
    )

def merge_event_clusters(clusters, threshold=0.60):
    merged = []
    for cluster in clusters:
        matched_index = None
        for index, existing in enumerate(merged):
            if cluster_merge_score(cluster, existing) >= threshold:
                matched_index = index
                break
        if matched_index is None:
            merged.append({
                "representative": cluster["representative"],
                "articles": list(cluster["articles"]),
            })
            continue
        existing = merged[matched_index]
        existing["articles"].extend(cluster["articles"])
        if source_score(cluster["representative"]["source"]) > source_score(existing["representative"]["source"]):
            existing["representative"] = cluster["representative"]
    return merged

def score_direction(text):
    pos = sum(1 for term in POSITIVE_TERMS if term in text)
    neg = sum(1 for term in NEGATIVE_TERMS if term in text)
    net = pos - neg
    return 3 if net >= 3 else 2 if net == 2 else 1 if net == 1 else -1 if net == -1 else -2 if net == -2 else -3 if net <= -3 else 0

def pre_score_cluster(ticker, company_name, cluster):
    rep = cluster["representative"]
    text = normalize_title(rep["title"]) + " " + " ".join(
        normalize_title(a["title"]) for a in cluster["articles"][:20]
    )
    direct = 25 if ticker.lower() in text or company_name.lower() in text else 10
    earnings = min(20, sum(1 for t in EARNINGS_TERMS if t in text) * 5)
    industry = min(15, sum(1 for t in INDUSTRY_TERMS if t in text) * 3)
    price_impact = min(15, sum(1 for t in MARKET_IMPACT_TERMS if t in text) * 4)
    age_hours = (datetime.now(timezone.utc) - rep["published"]).total_seconds() / 3600
    novelty = 10 if age_hours <= 6 else 8 if age_hours <= 12 else 7 if age_hours <= 18 else 6
    coverage = len(cluster["articles"])
    broad = 10 if coverage >= 20 else 8 if coverage >= 10 else 6 if coverage >= 5 else 4 if coverage >= 2 else 2
    if "500 billion" in text or "trillion" in text:
        broad = min(10, broad + 2)
    reliability = min(5, max(1, round(source_score(rep["source"]) / 20)))
    total = max(0, min(100, direct + earnings + industry + price_impact + novelty + broad + reliability))
    return {
        "importance_score": total,
        "direction": score_direction(text),
        "score_breakdown": {
            "direct_impact": direct,
            "earnings_cashflow": earnings,
            "industry_sector": industry,
            "stock_price_impact": price_impact,
            "novelty": novelty,
            "broader_market": broad,
            "source_reliability": reliability,
        },
    }

def build_event_record(ticker, company_name, event_id, cluster):
    rep = cluster["representative"]
    sources = []
    for article in cluster["articles"]:
        if article["source"] not in sources:
            sources.append(article["source"])
    return {
        "event_id": event_id,
        "ticker": ticker,
        "company_name": company_name,
        "title": rep["title"],
        "source": rep["source"],
        "published": rep["published"].isoformat(),
        "link": rep["link"],
        "coverage_count": len(cluster["articles"]),
        "other_sources": sources[:10],
        **pre_score_cluster(ticker, company_name, cluster),
    }

def direction_label(direction):
    return {
        3: "강한 호재", 2: "호재", 1: "약한 호재", 0: "중립",
        -1: "약한 악재", -2: "악재", -3: "강한 악재",
    }.get(direction, "중립")

def importance_label(score):
    return (
        "매우 중요" if score >= 90 else
        "중요" if score >= 80 else
        "의미 있는 뉴스" if score >= 70 else
        "참고" if score >= 50 else
        "낮은 중요도"
    )

def format_market_line(price):
    if not price:
        return "최근 거래일 종가/등락률: 가격 데이터 확인 불가"
    return (
        f"최근 거래일({price['latest_date']}) 종가: ${price['latest_close']:.2f} | "
        f"전일 대비: {price['change_pct']:+.2f}%"
    )

def make_fallback_comment(event):
    names = {
        "direct_impact": "기업 직접 영향",
        "earnings_cashflow": "실적/현금흐름",
        "industry_sector": "산업 영향",
        "stock_price_impact": "주가 영향 가능성",
        "novelty": "신규성",
        "broader_market": "시장 파급력",
        "source_reliability": "출처 신뢰도",
    }
    strongest = sorted(
        event["score_breakdown"].items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]
    reasons = ", ".join(names[key] for key, _ in strongest)
    return f"규칙 기반 사전평가에서 {reasons} 항목의 점수가 상대적으로 높았습니다."

def write_fallback_briefing(companies):
    lines = [
        "# 투자 뉴스 브리핑",
        "",
        "> Copilot AI 분석을 사용할 수 없을 때 자동 생성되는 0원 규칙 기반 fallback입니다.",
        "> 모든 중복 제거 이벤트를 중요도 사전점수 순으로 표시합니다.",
        "",
    ]
    for company in companies:
        lines.extend([
            f"# {company['ticker']} - {company['company_name']}",
            "",
            f"- {format_market_line(company['price_data'])}",
            f"- 최근 뉴스 검색 범위: {HOURS_BACK}시간",
            f"- 중복 제거 후 이벤트: {len(company['events'])}개",
            "",
            "## 주요 뉴스 - 중요도순",
            "",
        ])
        if not company["events"]:
            lines.extend(["검색 범위 내 독립 뉴스 이벤트가 없습니다.", ""])
            continue
        for index, event in enumerate(company["events"], 1):
            lines.extend([
                f"### {index}. {event['title']}",
                "",
                f"- 중요도: **{event['importance_score']}/100 ({importance_label(event['importance_score'])})**",
                f"- 방향성: **{event['direction']:+d} ({direction_label(event['direction'])})**",
                f"- 대표 출처: {event['source']}",
                f"- 동일 이벤트 보도 수: {event['coverage_count']}건",
                f"- 투자 코멘트: {make_fallback_comment(event)}",
                f"- 기사 시간: {event['published']}",
                f"- 원문 링크: {event['link']}",
                "",
            ])
    text = "\n".join(lines)
    FALLBACK_BRIEFING_PATH.write_text(text, encoding="utf-8")
    FINAL_BRIEFING_PATH.write_text(text, encoding="utf-8")

def write_copilot_prompt(companies):
    data_json = json.dumps(companies, ensure_ascii=False, indent=2)
    prompt = f"""
당신은 미국 주식 기관투자자를 지원하는 보수적인 투자 뉴스 애널리스트입니다.

아래 WATCHLIST DATA에는 여러 종목의 최근 거래일 종가, 전일 대비 등락률,
최근 {HOURS_BACK}시간 뉴스, 중복 제거된 독립 이벤트,
Python 규칙 기반 중요도 사전점수가 포함되어 있습니다.

중요 규칙:
1. 제공된 정보만 사용하세요. 기사 제목에 없는 사실을 지어내지 마세요.
2. 같은 사건을 반복해서 쓰지 마세요.
3. 회사 IR/공식 발표, SEC, Reuters, Bloomberg, WSJ, Financial Times를 우선하세요.
4. Python importance_score는 참고용입니다. 각 이벤트의 최종 중요도를 다시 판단하세요.
5. 중요도는 100점 만점:
   - 기업 직접 영향 25
   - 실적/현금흐름 20
   - 산업/섹터 15
   - 주가 영향 가능성 15
   - 신규성 10
   - 시장 파급력 10
   - 출처 신뢰도 5
6. 방향성: +3 강한 호재 / +2 호재 / +1 약한 호재 / 0 중립 /
   -1 약한 악재 / -2 악재 / -3 강한 악재
7. 모든 EVENTS를 빠짐없이 포함하세요. 중요도가 낮아도 제외하지 마세요.
8. 각 종목 안에서는 최종 중요도 높은 순서대로 정렬하세요.
9. 종목 제목 아래 최근 거래일 종가와 전일 대비 등락률을 표시하세요.
10. 주가 등락률과 뉴스의 인과관계를 단정하지 마세요.
11. 기사 제목만으로 부족하면 "추가 확인 필요"라고 명시하세요.
12. 한국어로 작성하세요.
13. 링크는 제공된 링크를 그대로 사용하세요.

형식:

# 투자 뉴스 브리핑

## AAPL - Apple
- 최근 거래일 종가: $000.00
- 전일 대비: +0.00%

### 핵심 요약
2~4문장

### 주요 뉴스 - 중요도순

#### 1. [한국어 제목]
- 중요도:
- 방향성:
- 한줄 요약:
- 투자 코멘트:
- 주가 영향:
- 추가 확인 필요:
- 대표 출처:
- 기사 시간:
- 원문 링크:

해당 종목의 모든 이벤트를 같은 형식으로 작성한 뒤 다음 종목으로 넘어가세요.

WATCHLIST DATA:
{data_json}
""".strip()
    COPILOT_PROMPT_PATH.write_text(prompt, encoding="utf-8")

def process_company(ticker, company_name):
    price_data = get_previous_market_move(ticker)
    raw_news = get_google_news(ticker, company_name)
    filtered_news, low_value_removed = filter_low_value_articles(raw_news)
    deduplicated_news, duplicates_removed = remove_near_duplicates(filtered_news)
    first_pass_clusters = cluster_events(deduplicated_news)
    merged_clusters = merge_event_clusters(first_pass_clusters)

    event_records = [
        build_event_record(ticker, company_name, index, cluster)
        for index, cluster in enumerate(merged_clusters, 1)
    ]
    event_records.sort(
        key=lambda event: (
            event["importance_score"],
            event["coverage_count"],
            source_score(event["source"]),
            event["published"],
        ),
        reverse=True,
    )

    print(f"\n{ticker} PROCESSING SUMMARY")
    print("-" * 70)
    print(f"Raw articles:             {len(raw_news)}")
    print(f"Low-value removed:        {len(low_value_removed)}")
    print(f"Near-duplicates removed:  {len(duplicates_removed)}")
    print(f"First-pass clusters:      {len(first_pass_clusters)}")
    print(f"Merged event clusters:    {len(merged_clusters)}")
    print(f"Events sent to Copilot:   {len(event_records)}")
    print(f"Market:                   {format_market_line(price_data)}")

    return {
        "ticker": ticker,
        "company_name": company_name,
        "price_data": price_data,
        "events": event_records,
    }

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    watchlist = load_stock_watchlist()

    print("=" * 70)
    print("INVESTMENT NEWS BRIEFING")
    print("=" * 70)
    print("Stock watchlist: " + ", ".join(item["ticker"] for item in watchlist))

    companies = []
    for item in watchlist:
        try:
            companies.append(process_company(item["ticker"], item["company_name"]))
        except Exception as exc:
            print(f"\n{item['ticker']} processing failed: {exc}")
            print("Continuing with the next ticker.")

    AI_INPUT_PATH.write_text(
        json.dumps(companies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_copilot_prompt(companies)
    write_fallback_briefing(companies)

    total_events = sum(len(company["events"]) for company in companies)

    print("\n" + "=" * 70)
    print("TOTAL SUMMARY")
    print("=" * 70)
    print(f"Successfully processed stocks: {len(companies)}")
    print(f"Total merged events:           {total_events}")
    print(f"AI input:                      {AI_INPUT_PATH}")
    print(f"Copilot prompt:                {COPILOT_PROMPT_PATH}")
    print(f"Fallback briefing:             {FALLBACK_BRIEFING_PATH}")
    print(f"Final briefing:                {FINAL_BRIEFING_PATH}")
    print()
    print("No paid OpenAI API is called by this Python script.")

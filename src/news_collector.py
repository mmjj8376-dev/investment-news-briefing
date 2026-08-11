"""
Collects recent news for every entry in config/watchlist.json, removes
duplicate coverage of the same event, pre-scores each remaining event
(0-100 importance, -3..+3 direction), attaches the previous day's closing
price/change, and writes three files under output/:

  - ai_input.json        structured data handed to the AI
  - copilot_prompt.txt   instructions + ai_input.json for the Copilot CLI
  - briefing.md          zero-cost rule-based fallback briefing

A watchlist entry can be a plain stock ({"ticker","company"}) or an ETF
({"ticker","company","type":"etf","sector":...,"top_holdings":[...]}). For
an ETF, news is collected for the fund itself, for each of its top holdings,
and for its sector, then merged into one deduplicated, importance-sorted
list. Each event is tagged with which of those it relates to.

No third-party packages are required (the GitHub Actions workflow does not
run `pip install`), so only the Python standard library is used.

Design rules (do not silently reintroduce old limits):
  - Every event that survives quality filtering + dedup is kept. There is
    no "importance >= 70" cutoff. Ordering, not filtering, is what the
    importance score is for.
  - There is no hard "top 20" cap. MAX_EVENTS_PER_TICKER below is an
    optional safety valve for the future (e.g. if a prompt gets too long
    for the AI) -- it stays disabled (None) unless someone deliberately
    turns it on.
  - A failed price lookup must never abort the news pipeline.
  - ETF top holdings are read from watchlist.json, not fetched live --
    free holdings APIs are unreliable/rate-limited, so holdings are
    maintained by hand and should be refreshed every month or two.
"""

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

BASE_DIR = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"
OUTPUT_DIR = BASE_DIR / "output"

HOURS_BACK = 24
MAX_EVENTS_PER_TICKER = None  # e.g. set to 40 later if prompts become too long. None = no cap.
DEDUP_TITLE_SIMILARITY = 0.55
DEDUP_TIME_WINDOW_HOURS = 18

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Source reliability tiers (rubric item: 출처 신뢰도, 5 points max)
# ---------------------------------------------------------------------------
TIER1_KEYWORDS = [
    "reuters", "bloomberg", "wall street journal", "wsj", "financial times",
    "businesswire", "business wire", "pr newswire", "globenewswire",
    "sec.gov", "sec filing", "investor relations",
]
TIER2_KEYWORDS = [
    "cnbc", "associated press", "ap news", "marketwatch", "barron",
    "yahoo finance", "forbes", "fortune", "axios", "business insider",
    "the verge", "techcrunch",
]
TIER_SCORE = {1: 5, 2: 3, 3: 1}

# Clickbait / opinion pieces get dropped entirely, not just down-scored.
# (Sector-level searches surface a lot of "N stocks to buy" listicles, so
# a few extra patterns are included for that case.)
LOW_QUALITY_TITLE_PATTERNS = [
    r"should you buy", r"should you sell", r"is it time to buy",
    r"is now the time", r"here'?s why", r"\btop \d+ stocks\b",
    r"\d+ reasons? (?:to|why)", r"better buy", r"buy or sell",
    r"should investors", r"is .* a buy", r"stocks to (?:buy|watch|avoid|sell)",
    r"best .* stocks", r"top picks", r"stocks? to consider",
]

# ---------------------------------------------------------------------------
# Pre-scoring keyword buckets (rubric: 100 points total)
# ---------------------------------------------------------------------------
DIRECT_IMPACT_KEYWORDS = [
    "earnings", "ceo", "acquisition", "merger", "lawsuit", "recall",
    "contract", "partnership", "unveils", "announces", "launch", "launches",
    "guidance", "resigns", "appoints", "investigation", "fine", "settlement",
]
FINANCIAL_KEYWORDS = [
    "earnings", "revenue", "profit", "eps", "guidance", "beat", "beats",
    "miss", "misses", "cash flow", "dividend", "buyback", "margin",
]
INDUSTRY_KEYWORDS = [
    "chip", "semiconductor", "ai", "regulation", "tariff", "supply chain",
    "sector", "industry", "antitrust", "policy", "export controls",
]
PRICE_IMPACT_KEYWORDS = [
    "surge", "surges", "plunge", "plunges", "soar", "soars", "drop", "drops",
    "rally", "rallies", "upgrade", "upgrades", "downgrade", "downgrades",
    "price target", "shares fall", "shares rise", "shares jump", "shares sink",
]
MARKET_WIDE_KEYWORDS = [
    "wall street", "s&p 500", "nasdaq", "dow jones", "broader market",
    "fed", "interest rate", "inflation",
]

POSITIVE_WORDS = [
    "beats", "beat estimates", "surge", "soar", "rally", "upgrade",
    "raises guidance", "record revenue", "record profit", "wins contract",
    "partnership", "expands", "announces deal", "buyback",
    "dividend increase", "strong demand", "outperform", "price target raised",
    "unveils", "launches", "approval", "breakthrough", "beats expectations",
]
NEGATIVE_WORDS = [
    "plunge", "slump", "downgrade", "cuts guidance", "misses estimates",
    "lawsuit", "investigation", "recall", "layoffs", "delay", "shortage",
    "warns", "fraud", "probe", "fine", "ban", "resigns", "price target cut",
    "sell-off", "decline", "miss estimates",
]

DIRECTION_LABELS = {
    3: "강한 호재", 2: "호재", 1: "약한 호재",
    0: "중립",
    -1: "약한 악재", -2: "악재", -3: "강한 악재",
}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def fetch_news_by_query(query, label):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[WARN] {label}: failed to fetch news ({exc})")
        return []

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        print(f"[WARN] {label}: failed to parse RSS ({exc})")
        return []

    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate")
        source_el = item.find("source")
        source = (source_el.text or "Unknown").strip() if source_el is not None else "Unknown"

        if title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        if not title or not pub_date_raw:
            continue
        try:
            published = parsedate_to_datetime(pub_date_raw)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        items.append({
            "title": title,
            "link": link,
            "source": source,
            "published": published,
        })
    return items


def is_low_quality(title):
    lowered = title.lower()
    return any(re.search(pattern, lowered) for pattern in LOW_QUALITY_TITLE_PATTERNS)


def classify_source_tier(source_name, company=None):
    lowered = source_name.lower()
    if any(k in lowered for k in TIER1_KEYWORDS):
        return 1
    if company and company.lower() in lowered and ("newsroom" in lowered or " ir" in f" {lowered}"):
        return 1
    if any(k in lowered for k in TIER2_KEYWORDS):
        return 2
    return 3


# ---------------------------------------------------------------------------
# Deduplication -- multiple outlets covering the same event become one event
# ---------------------------------------------------------------------------
def normalize_title(title):
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def cluster_articles(articles):
    clusters = []
    for art in articles:
        norm = normalize_title(art["title"])
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            ratio = difflib.SequenceMatcher(None, norm, normalize_title(rep["title"])).ratio()
            hours_apart = abs((art["published"] - rep["published"]).total_seconds()) / 3600
            if ratio >= DEDUP_TITLE_SIMILARITY and hours_apart <= DEDUP_TIME_WINDOW_HOURS:
                cluster.append(art)
                cluster.sort(key=lambda a: (a["source_tier"], a["published"]))
                placed = True
                break
        if not placed:
            clusters.append([art])

    events = []
    for cluster in clusters:
        rep = cluster[0]
        secondary = sorted({m["source"] for m in cluster[1:] if m["source"] != rep["source"]})
        related_to = sorted({m["related_to"] for m in cluster})
        events.append({**rep, "secondary_sources": secondary, "related_to": related_to})
    return events


# ---------------------------------------------------------------------------
# Pre-scoring (heuristic baseline; the AI does the final call)
# ---------------------------------------------------------------------------
def bucket_score(text, keywords, max_points):
    hits = sum(1 for kw in keywords if kw in text)
    if hits <= 0:
        return 0
    if hits == 1:
        return round(max_points * 0.6)
    return max_points


def recency_score(hours_ago):
    if hours_ago <= 3:
        return 10
    if hours_ago <= 6:
        return 8
    if hours_ago <= 12:
        return 6
    if hours_ago <= 18:
        return 4
    if hours_ago <= 24:
        return 2
    return 0


def compute_pre_score(event, hours_ago):
    text = event["title"].lower()
    breakdown = {
        "기업_직접_영향": bucket_score(text, DIRECT_IMPACT_KEYWORDS, 25),
        "실적_현금흐름_영향": bucket_score(text, FINANCIAL_KEYWORDS, 20),
        "산업_섹터_영향": bucket_score(text, INDUSTRY_KEYWORDS, 15),
        "주가_영향_가능성": bucket_score(text, PRICE_IMPACT_KEYWORDS, 15),
        "신규성": recency_score(hours_ago),
        "시장_파급력": bucket_score(text, MARKET_WIDE_KEYWORDS, 10),
        "출처_신뢰도": TIER_SCORE.get(event["source_tier"], 1),
    }
    total = min(100, sum(breakdown.values()))
    return total, breakdown


def compute_direction(event):
    text = event["title"].lower()
    positive_hits = sum(1 for w in POSITIVE_WORDS if w in text)
    negative_hits = sum(1 for w in NEGATIVE_WORDS if w in text)
    net = positive_hits - negative_hits
    return max(-3, min(3, net))


def build_queries_for_stock(stock):
    """Return [(search_query, company_for_tier_check, related_to_tag), ...].

    A plain stock has exactly one query. An ETF has one query for the fund
    itself, one per top holding, and (if a sector is set) one sector query.
    """
    ticker = stock["ticker"]
    company = stock["company"]

    if stock.get("type") != "etf":
        return [(f'"{company}" OR {ticker} stock', company, ticker)]

    queries = [(f'"{company}" OR {ticker} ETF', company, ticker)]
    for holding in stock.get("top_holdings", []):
        h_ticker = holding["ticker"]
        h_company = holding["company"]
        queries.append((f'"{h_company}" OR {h_ticker} stock', h_company, f"holding:{h_ticker}"))

    sector = stock.get("sector")
    if sector:
        queries.append((f'"{sector}" sector stocks OR "{sector}" industry outlook', None, "sector"))

    return queries


def render_related_to(related_to, stock):
    """Turn related_to tags like ['NVDA', 'holding:AAPL', 'sector'] into Korean labels."""
    holding_names = {h["ticker"]: h["company"] for h in stock.get("top_holdings", [])}
    labels = []
    for tag in related_to:
        if tag == stock["ticker"]:
            labels.append(f"{stock['ticker']} 자체")
        elif tag.startswith("holding:"):
            h_ticker = tag.split(":", 1)[1]
            h_company = holding_names.get(h_ticker, h_ticker)
            labels.append(f"보유종목 {h_ticker}({h_company})")
        elif tag == "sector":
            labels.append(f"{stock.get('sector', '섹터')} 섹터")
        else:
            labels.append(tag)
    return ", ".join(labels)


def collect_events_for_stock(stock, now):
    ticker = stock["ticker"]
    company = stock["company"]
    cutoff = now - timedelta(hours=HOURS_BACK)

    raw = []
    for query, tier_company, related_to in build_queries_for_stock(stock):
        label = f"{ticker}:{related_to}"
        for art in fetch_news_by_query(query, label):
            art["related_to"] = related_to
            art["tier_company"] = tier_company
            raw.append(art)

    filtered = []
    for art in raw:
        if art["published"] < cutoff:
            continue
        if is_low_quality(art["title"]):
            continue
        art["source_tier"] = classify_source_tier(art["source"], art["tier_company"])
        filtered.append(art)

    events = cluster_articles(filtered)

    scored = []
    for ev in events:
        hours_ago = (now - ev["published"]).total_seconds() / 3600
        score, breakdown = compute_pre_score(ev, hours_ago)
        direction = compute_direction(ev)
        scored.append({
            "ticker": ticker,
            "company": company,
            "title": ev["title"],
            "link": ev["link"],
            "primary_source": ev["source"],
            "secondary_sources": ev["secondary_sources"],
            "related_to": ev["related_to"],
            "related_to_display": render_related_to(ev["related_to"], stock),
            "published_utc": ev["published"].strftime("%Y-%m-%d %H:%M UTC"),
            "hours_ago": round(hours_ago, 1),
            "pre_importance_score": score,
            "pre_score_breakdown": breakdown,
            "pre_direction": direction,
            "pre_direction_label": DIRECTION_LABELS[direction],
        })

    scored.sort(key=lambda e: e["pre_importance_score"], reverse=True)

    if MAX_EVENTS_PER_TICKER is not None and len(scored) > MAX_EVENTS_PER_TICKER:
        dropped = len(scored) - MAX_EVENTS_PER_TICKER
        print(f"[INFO] {ticker}: capping {len(scored)} -> {MAX_EVENTS_PER_TICKER} events (dropped {dropped})")
        scored = scored[:MAX_EVENTS_PER_TICKER]

    return scored


# ---------------------------------------------------------------------------
# Previous close / change -- must never raise, only ever return None
#
# Stooq's CSV export now sits behind a JavaScript bot-check page, so a plain
# urllib GET no longer gets real data from it. Yahoo Finance's public chart
# endpoint returns JSON directly and needs no auth, so that's used instead.
# ---------------------------------------------------------------------------
def fetch_previous_close(ticker):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        "?range=5d&interval=1d"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        timestamps = result["timestamp"]
        pairs = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
        if len(pairs) < 2:
            return None

        (_, prev_close), (last_ts, last_close) = pairs[-2], pairs[-1]
        if prev_close == 0:
            return None

        change_pct = (last_close - prev_close) / prev_close * 100
        return {
            "date": datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "close": round(last_close, 2),
            "change_pct": round(change_pct, 2),
        }
    except Exception as exc:  # noqa: BLE001 -- a price failure must not break the pipeline
        print(f"[WARN] {ticker}: failed to fetch previous close ({exc})")
        return None


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------
def build_ai_input(watchlist, now):
    tickers_payload = []
    for stock in watchlist:
        ticker = stock["ticker"]
        company = stock["company"]
        events = collect_events_for_stock(stock, now)
        price = fetch_previous_close(ticker)
        tickers_payload.append({
            "ticker": ticker,
            "company": company,
            "is_etf": stock.get("type") == "etf",
            "sector": stock.get("sector"),
            "top_holdings": stock.get("top_holdings", []),
            "previous_close": price,
            "event_count": len(events),
            "events": events,
        })
    return {
        "generated_at_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "hours_back": HOURS_BACK,
        "tickers": tickers_payload,
    }


def format_price_line(previous_close):
    if not previous_close:
        return "전일 종가: 데이터 조회 실패"
    sign = "+" if previous_close["change_pct"] >= 0 else ""
    return f"전일 종가: ${previous_close['close']} ({sign}{previous_close['change_pct']}%)"


COPILOT_PROMPT_TEMPLATE = """\
당신은 미국 주식 투자자를 위한 한국어 뉴스 브리핑 작성자입니다.

[반드시 지켜야 할 규칙]
1. 아래 NEWS_DATA(JSON)에 들어있는 뉴스만 사용하세요. 목록에 없는 사실을
   지어내지 마세요. 확실하지 않은 부분은 "추가 확인 필요"라고 쓰세요.
2. 특정 중요도 이상만 보여주는 컷은 없습니다. 각 종목의 events 배열에
   있는 뉴스를 하나도 빠짐없이 전부 포함하세요.
3. 각 뉴스마다 당신이 최종 판단한 중요도(0~100)와 방향성(-3~+3)을
   부여하고, 그 중요도가 높은 순서로 정렬해서 보여주세요.
   (JSON의 pre_importance_score/pre_direction은 참고용 초기값입니다.
   내용을 보고 필요하면 조정하세요.)
4. 방향성 표기: +3 강한 호재, +2 호재, +1 약한 호재, 0 중립,
   -1 약한 악재, -2 악재, -3 강한 악재.
5. 각 종목 섹션 맨 위에는 previous_close 정보를 이용해 전일 종가와
   전일 등락률을 보여주세요. previous_close가 null이면
   "전일 종가: 데이터 조회 실패"라고 쓰고 뉴스 브리핑은 계속 작성하세요.
6. 같은 사건을 다룬 뉴스는 이미 하나의 이벤트로 합쳐져 있습니다
   (primary_source가 대표 출처, secondary_sources가 추가 출처).
7. is_etf가 true인 종목은 ETF입니다. 이 경우 events 안의 각 뉴스에는
   related_to_display 필드가 있어서 그 뉴스가 "ETF 자체", "보유종목 중
   하나(어떤 종목인지 표시됨)", "섹터 전반" 중 무엇과 관련있는지 알려줍니다.
   각 뉴스의 "한줄 요약" 앞이나 별도 항목으로 이 관련성을 한국어로 표시하세요
   (예: "[관련: 보유종목 NVDA(NVIDIA)]").
8. 모든 설명은 한국어로 작성하세요.

[각 뉴스 이벤트마다 포함할 항목]
- 제목(한국어로 번역/요약)
- 중요도: N/100
- 방향성: 부호 (레이블)
- 관련 (ETF인 경우만: ETF 자체 / 보유종목 / 섹터 중 어느 것인지)
- 한줄 요약
- 투자 코멘트
- 주가 영향
- 추가 확인 필요 (없으면 "없음")
- 대표 출처 (필요시 추가 출처도)
- 기사 시간
- 원문 링크

[출력 형식 예시]
# {ticker} - {company} 투자 뉴스 브리핑

전일 종가: $182.35 (+2.41%)

## 핵심 요약
(전체 뉴스 흐름에 대한 2~3문장 요약)

## 주요 뉴스

### 1. (제목)
- 중요도: 94/100
- 방향성: +3 (강한 호재)
- 한줄 요약: ...
- 투자 코멘트: ...
- 주가 영향: ...
- 추가 확인 필요: ...
- 대표 출처: ...
- 기사 시간: ...
- 원문 링크: ...

### 2. (다음 뉴스, 중요도 내림차순으로 계속)
...

여러 종목이 있으면 종목별로 위 형식을 반복하고 "---"로 구분하세요.

[NEWS_DATA]
{news_data}
"""


def build_copilot_prompt(ai_input):
    news_data = json.dumps(ai_input, ensure_ascii=False, indent=2)
    first_ticker = ai_input["tickers"][0] if ai_input["tickers"] else {"ticker": "TICKER", "company": "Company"}
    return COPILOT_PROMPT_TEMPLATE.format(
        ticker=first_ticker["ticker"],
        company=first_ticker["company"],
        news_data=news_data,
    )


def build_fallback_briefing(ai_input):
    sections = ["# Daily Investment News Briefing (rule-based fallback)", ""]
    sections.append(f"생성 시각(UTC): {ai_input['generated_at_utc']}")
    sections.append("")

    for stock in ai_input["tickers"]:
        header = f"## {stock['ticker']} - {stock['company']}"
        if stock["is_etf"]:
            header += f" (ETF · {stock['sector']} 섹터)" if stock["sector"] else " (ETF)"
        sections.append(header)
        sections.append(format_price_line(stock["previous_close"]))
        sections.append("")

        if not stock["events"]:
            sections.append("최근 24시간 내 유효한 뉴스가 없습니다.")
            sections.append("")
            continue

        for idx, event in enumerate(stock["events"], start=1):
            sign = "+" if event["pre_direction"] > 0 else ""
            sections.append(f"### {idx}. {event['title']}")
            sections.append(f"- 중요도: {event['pre_importance_score']}/100")
            sections.append(f"- 방향성: {sign}{event['pre_direction']} ({event['pre_direction_label']})")
            if stock["is_etf"]:
                sections.append(f"- 관련: {event['related_to_display']}")
            sections.append("- 한줄 요약: (Copilot 미사용 - 원문 제목 참고)")
            sections.append("- 투자 코멘트: 추가 확인 필요")
            sections.append("- 주가 영향: 추가 확인 필요")
            source_line = event["primary_source"]
            if event["secondary_sources"]:
                source_line += " (추가 출처: " + ", ".join(event["secondary_sources"]) + ")"
            sections.append(f"- 출처: {source_line}")
            sections.append(f"- 기사 시간: {event['published_utc']}")
            sections.append(f"- 원문 링크: {event['link']}")
            sections.append("")

        sections.append("---")
        sections.append("")

    return "\n".join(sections)


def main():
    now = datetime.now(timezone.utc)
    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))

    OUTPUT_DIR.mkdir(exist_ok=True)

    ai_input = build_ai_input(watchlist, now)
    (OUTPUT_DIR / "ai_input.json").write_text(
        json.dumps(ai_input, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    prompt = build_copilot_prompt(ai_input)
    (OUTPUT_DIR / "copilot_prompt.txt").write_text(prompt, encoding="utf-8")

    fallback = build_fallback_briefing(ai_input)
    (OUTPUT_DIR / "briefing.md").write_text(fallback, encoding="utf-8")

    total_events = sum(t["event_count"] for t in ai_input["tickers"])
    print(f"[INFO] Collected {total_events} news events across {len(watchlist)} ticker(s).")


if __name__ == "__main__":
    main()

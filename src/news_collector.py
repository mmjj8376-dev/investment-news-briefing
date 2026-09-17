"""
Collects recent news for every entry in config/watchlist.json, removes
duplicate coverage of the same event, pre-scores each remaining event
(0-100 importance, -3..+3 direction), attaches the previous day's closing
price/change, and writes these under output/:

  - ai_input.json           all tickers combined, for reference/debugging
  - tickers/{TICKER}.json   one ticker's data each -- what the AI actually reads
  - prompts/{TICKER}.txt    instructions for the main per-ticker briefing
  - prompts/synthesis_{T}.txt  instructions for the short neutral per-ticker
                            synthesis used in the second ("매크로 · 보유종목")
                            email -- see PORTFOLIO_SYNTHESIS_TEMPLATE
  - macro.json              today's macro/economic news (not ticker-specific)
  - prompts/macro.txt        instructions for the macro summary email section
  - ticker_order.txt        ticker list in watchlist order, one per line
  - briefing.md             zero-cost rule-based fallback briefing
  - fallback/{TICKER}.md, fallback/synthesis_{T}.md, fallback/macro.md
                            per-item rule-based fallbacks the workflow
                            substitutes in when Copilot fails for just
                            that one item

The workflow invokes the Copilot CLI once per ticker (see ticker_order.txt),
each time pointed at just that ticker's small file, and concatenates the
results. A single combined file covering every ticker was too large for the
CLI's file-reading tool to get through in one pass. The second email's
per-ticker synthesis and macro summary follow the same one-call-per-item
pattern for the same reason.

The second email (macro summary + per-holding synthesis) is intentionally
never allowed to contain a buy/sell/hold call or any other personalized
trading instruction -- see PORTFOLIO_SYNTHESIS_TEMPLATE and
MACRO_PROMPT_TEMPLATE. It summarizes what happened; it does not tell the
reader what to do about it.

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
#
# Different outlets word the same story very differently ("Jefferies
# Downgrades Apple" vs "AAPL Falls After Jefferies Downgrades Stock Over 20th
# Anniversary iPhone Setback -- But Gene Munster Sees 'Strong Next 12
# Months'"). Comparing raw character sequences (difflib) misses these
# because word order/length differ too much, even though the same handful of
# distinctive words (jefferies, downgrades, aapl) appear in both.
#
# A plain Jaccard word-overlap ratio (shared / union) *also* misses this
# specific case: one outlet's headline is much longer/more detailed than the
# other's, so the union is large and dilutes the ratio even though the short
# headline's words are almost entirely contained in the long one. The
# overlap coefficient (shared / smaller-title's word count) is robust to
# that length asymmetry -- it asks "does most of the SHORTER title's content
# also appear in the other one?" rather than "do the two titles look similar
# overall?". A minimum shared-word count guards against two short titles
# matching on a single incidental word.
# ---------------------------------------------------------------------------
DEDUP_WORD_OVERLAP = 0.4
DEDUP_MIN_SHARED_WORDS = 2

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "it", "its",
    "this", "that", "after", "over", "amid", "than", "into", "up", "down",
    "out", "about", "against", "why", "what", "how", "will", "would", "could",
    "should", "stock", "stocks", "shares", "share", "says", "say", "said",
    "report", "reports", "reported", "news", "here", "now", "today",
}


def normalize_title(title):
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def title_word_set(norm_title):
    return {w for w in norm_title.split() if len(w) > 2 and w not in STOPWORDS}


def word_overlap_ratio(words_a, words_b):
    if not words_a or not words_b:
        return 0.0
    shared = len(words_a & words_b)
    if shared < DEDUP_MIN_SHARED_WORDS:
        return 0.0
    smaller = min(len(words_a), len(words_b))
    return shared / smaller if smaller else 0.0


def cluster_articles(articles):
    clusters = []
    for art in articles:
        norm = normalize_title(art["title"])
        words = title_word_set(norm)
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            rep_norm = normalize_title(rep["title"])
            char_ratio = difflib.SequenceMatcher(None, norm, rep_norm).ratio()
            overlap = word_overlap_ratio(words, title_word_set(rep_norm))
            hours_apart = abs((art["published"] - rep["published"]).total_seconds()) / 3600
            is_match = char_ratio >= DEDUP_TITLE_SIMILARITY or overlap >= DEDUP_WORD_OVERLAP
            if is_match and hours_apart <= DEDUP_TIME_WINDOW_HOURS:
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
# Macro / economic news -- not tied to any one ticker. Used for the second
# "매크로 · 보유종목" email, which is a neutral news digest, never a trade
# recommendation (see PORTFOLIO_SYNTHESIS_TEMPLATE below for why).
# ---------------------------------------------------------------------------
MACRO_QUERIES = [
    ('"Federal Reserve" OR FOMC OR "interest rate decision"', "fed"),
    ('"CPI report" OR inflation OR "PCE inflation"', "inflation"),
    ('"jobs report" OR unemployment OR "non-farm payrolls"', "jobs"),
    ('GDP OR "economic growth" OR recession', "growth"),
    ('"stock market today" OR "Wall Street" OR "S&P 500"', "market"),
]


def collect_macro_events(now):
    cutoff = now - timedelta(hours=HOURS_BACK)

    raw = []
    for query, tag in MACRO_QUERIES:
        for art in fetch_news_by_query(query, f"macro:{tag}"):
            art["related_to"] = tag
            art["tier_company"] = None
            raw.append(art)

    filtered = []
    for art in raw:
        if art["published"] < cutoff:
            continue
        if is_low_quality(art["title"]):
            continue
        art["source_tier"] = classify_source_tier(art["source"], None)
        filtered.append(art)

    events = cluster_articles(filtered)

    scored = []
    for ev in events:
        hours_ago = (now - ev["published"]).total_seconds() / 3600
        score, breakdown = compute_pre_score(ev, hours_ago)
        direction = compute_direction(ev)
        scored.append({
            "title": ev["title"],
            "link": ev["link"],
            "primary_source": ev["source"],
            "secondary_sources": ev["secondary_sources"],
            "related_to": ev["related_to"],
            "published_utc": ev["published"].strftime("%Y-%m-%d %H:%M UTC"),
            "hours_ago": round(hours_ago, 1),
            "pre_importance_score": score,
            "pre_score_breakdown": breakdown,
            "pre_direction": direction,
            "pre_direction_label": DIRECTION_LABELS[direction],
        })

    scored.sort(key=lambda e: e["pre_importance_score"], reverse=True)
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


# One Copilot invocation handles ONE ticker's data file, not the whole
# watchlist. A single combined file (dozens of tickers, hundreds of events)
# was too big for the CLI's file-reading tool to digest in one shot -- it
# had to chunk-read it, lost track partway through, tried to shell out to
# jq (blocked by --no-ask-user), and gave up asking whether to continue.
# Scoping each run to one ticker's file keeps every read small enough to
# finish in a single pass, however many tickers or events exist in total.
TICKER_PROMPT_TEMPLATE = """\
당신은 미국 주식 투자자를 위한 한국어 뉴스 브리핑 작성자입니다. 이 실행은
사람이 지켜보지 않는 자동화된 배치 작업입니다. 대화가 아니므로, 중간에
멈춰서 "계속 원하시면 요청해주세요" 같은 말을 하거나 다음 지시를 기다려서는
안 됩니다. 한 번의 응답으로 이 종목 하나의 브리핑을 끝까지 완료하세요.

이 저장소 안의 {ticker_data_path} 파일을 먼저 읽으세요. 그 JSON 파일에는
{ticker}({company}) 이 종목 하나에 대해 오늘 수집된 뉴스 데이터가 들어
있습니다. 이 지시문에는 뉴스 데이터를 직접 넣지 않았으니, 반드시 그 파일을
읽어서 사용하세요.

[반드시 지켜야 할 규칙]
0. events 배열에 있는 뉴스를 하나도 빠뜨리지 말고 전부, 끝까지 작성하세요.
   이벤트가 많아도 요약해서 건너뛰거나 "나머지는 요청하시면 작성하겠습니다"
   라고 미루지 마세요. 이벤트 수가 많으면 각 항목의 문장을 짧게 줄여서
   (투자 코멘트/주가 영향 각 1문장) 분량을 조절하되, 이벤트 자체는 절대
   빠뜨리지 마세요. 완결성이 상세함보다 우선입니다.
1. {ticker_data_path} 파일에 들어있는 뉴스만 사용하세요. 그 파일에 없는
   사실을 지어내지 마세요. 확실하지 않은 부분은 "추가 확인 필요"라고
   쓰세요.
2. 특정 중요도 이상만 보여주는 컷은 없습니다. events 배열에 있는 뉴스를
   하나도 빠짐없이 전부 포함하세요.
3. 각 뉴스마다 당신이 최종 판단한 중요도(0~100)와 방향성(-3~+3)을
   부여하고, 그 중요도가 높은 순서로 정렬해서 보여주세요.
   (JSON의 pre_importance_score/pre_direction은 참고용 초기값입니다.
   내용을 보고 필요하면 조정하세요.)
4. 방향성 표기: +3 강한 호재, +2 호재, +1 약한 호재, 0 중립,
   -1 약한 악재, -2 악재, -3 강한 악재.
5. 맨 위에는 previous_close 정보를 이용해 전일 종가와 전일 등락률을
   보여주세요. previous_close가 null이면 "전일 종가: 데이터 조회 실패"
   라고 쓰고 브리핑은 계속 작성하세요.
6. 같은 사건을 다룬 뉴스는 이미 하나의 이벤트로 합쳐져 있습니다
   (primary_source가 대표 출처, secondary_sources가 추가 출처). 그래도
   제목만 다르고 실제로는 같은 사건(예: 같은 애널리스트의 같은 하향
   리포트를 여러 매체가 다르게 표현한 경우)으로 보이는 이벤트가 남아
   있다면, 그것들도 당신이 판단해서 하나의 항목으로 합치고 출처를
   나열하세요. 표현이 다르다고 다른 사건인 것은 아닙니다.
7. is_etf가 true이면 이 종목은 ETF입니다. 이 경우 events 안의 각 뉴스에는
   related_to_display 필드가 있어서 그 뉴스가 "ETF 자체", "보유종목 중
   하나(어떤 종목인지 표시됨)", "섹터 전반" 중 무엇과 관련있는지 알려줍니다.
   각 뉴스의 "한줄 요약" 앞이나 별도 항목으로 이 관련성을 한국어로
   표시하세요 (예: "[관련: 보유종목 NVDA(NVIDIA)]").
8. 모든 설명은 한국어로 작성하세요.
9. 원문 링크는 {ticker_data_path} 파일의 link 값을 한 글자도 빠뜨리지 말고
   그대로 출력하세요. 길다고 줄이거나 "..."으로 자르지 마세요 -- 한 글자만
   잘려도 링크가 깨져서 클릭할 수 없게 됩니다.

[각 뉴스 이벤트마다 포함할 항목]
- 제목(한국어로 번역/요약)
- 중요도: N/100
- 방향성: 부호 (레이블)
- 관련 (ETF인 경우만: ETF 자체 / 보유종목 / 섹터 중 어느 것인지)
- 한줄 요약
- 투자 코멘트
- 주가 영향: "하방 압력 가능", "단기 변동성 확대" 같은 두루뭉술한 말만 쓰지
  마세요. previous_close의 실제 종가·등락률 숫자를 근거로 구체적으로
  쓰세요. 예: "이미 전일 -1.62% 하락한 상태에서 이 뉴스가 추가 하방
  압력으로 작용할 가능성" 처럼, 지금 이 종목이 실제로 얼마나/어느
  방향으로 움직였는지를 문장에 반영하세요. previous_close가 null이면
  숫자를 지어내지 말고 "전일 가격 데이터 없음"이라고 쓰세요.
- 추가 확인 필요 (없으면 "없음")
- 대표 출처 (필요시 추가 출처도)
- 기사 시간
- 원문 링크

[출력 형식 예시]
# {ticker} - {company} 투자 뉴스 브리핑

전일 종가: $182.35 (+2.41%)

## 핵심 요약
(이 종목 전체 뉴스 흐름에 대한 2~3문장 요약)

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

다시 한번 강조합니다:
- 뉴스 데이터는 이 지시문 안이 아니라 {ticker_data_path} 파일 안에
  있습니다. 그 파일을 꼭 읽고 시작하세요.
- events 배열의 모든 뉴스를 끝까지 다 쓰기 전에는 응답을 마치지 마세요.
  중간에 멈추고 다음 지시를 기다리지 마세요.
- 원문 링크는 절대 축약/생략하지 말고 전체를 그대로 출력하세요.
"""


def build_ticker_prompt(ticker_payload):
    ticker = ticker_payload["ticker"]
    return TICKER_PROMPT_TEMPLATE.format(
        ticker=ticker,
        company=ticker_payload["company"],
        ticker_data_path=f"output/tickers/{ticker}.json",
    )


# ---------------------------------------------------------------------------
# Second email: a neutral macro summary + a short, non-directive synthesis
# per holding. Neither of these may ever contain a buy/sell/hold call or
# any other personalized trading instruction -- that crosses into
# personalized investment advice, which this pipeline does not provide.
# They summarize what happened; the reader decides what it means for them.
# ---------------------------------------------------------------------------
MACRO_PROMPT_TEMPLATE = """\
당신은 매크로 경제 뉴스를 정리하는 한국어 브리핑 작성자입니다. 이 실행은
사람이 지켜보지 않는 자동화된 배치 작업입니다. 중간에 멈추지 말고 한 번에
끝까지 작성하세요.

이 저장소 안의 {macro_data_path} 파일을 읽으세요. 오늘 수집된 미국/글로벌
매크로 경제 뉴스(금리, 물가, 고용, 성장률, 시장 전반 동향 등)가 events
배열에 들어 있습니다.

[반드시 지켜야 할 규칙]
1. {macro_data_path} 파일에 있는 뉴스만 사용하세요. 없는 사실을 지어내지
   마세요. 확실하지 않으면 "추가 확인 필요"라고 쓰세요.
2. 특정 종목이나 자산에 대한 매수/매도/홀딩 추천, 투자 지시는 절대 하지
   마세요 -- 이건 시장 전반 매크로 뉴스 요약이지 투자 자문이 아닙니다.
3. events 배열의 이벤트를 하나도 빠뜨리지 말고 반영하되, 개별 나열보다는
   아래 주제별로 묶어서 정리하세요.
4. 원문 링크는 절대 줄이거나 "..."으로 자르지 말고 그대로 출력하세요.
5. 한국어로 작성하세요.

[출력 형식]
# 매크로 · 경제지표 브리핑

## 핵심 요약
(오늘 매크로 뉴스 흐름을 2~4문장으로 요약)

## 금리 · 통화정책
## 물가 · 인플레이션
## 고용
## 성장 · 경기
## 시장 전반

각 섹션마다, 관련 뉴스가 있으면 이벤트별로:
- 한줄 요약
- 대표 출처
- 기사 시간
- 원문 링크

관련 뉴스가 없는 섹션은 "관련 뉴스 없음"이라고만 쓰세요.

다시 한번 강조합니다: {macro_data_path} 파일을 꼭 읽고 시작하고, 매수/매도
추천은 절대 하지 말고, events의 모든 이벤트를 빠짐없이 반영하세요.
"""


def build_macro_prompt():
    return MACRO_PROMPT_TEMPLATE.format(macro_data_path="output/macro.json")


PORTFOLIO_SYNTHESIS_TEMPLATE = """\
당신은 보유 종목에 대한 오늘의 뉴스를 중립적으로 요약하는 한국어 작성자
입니다. 이 실행은 자동화된 배치 작업이니 중간에 멈추지 마세요.

이 저장소 안의 {ticker_data_path} 파일을 읽으세요. {ticker}({company})에
대해 오늘 수집된 뉴스 데이터가 들어 있습니다.

[반드시 지켜야 할 규칙 -- 매우 중요]
1. "매수", "매도", "홀딩", "추천", "비중 확대/축소" 같은 투자 판단이나
   지시는 절대 쓰지 마세요. 이건 투자 자문이 아니라, 오늘 이 종목에
   어떤 뉴스가 있었고 그게 대체로 긍정적이었는지 부정적이었는지를
   사실 기반으로 요약하는 것입니다. 최종 판단은 전적으로 읽는 사람의
   몫이며, 당신은 그 판단을 대신 내리면 안 됩니다.
2. {ticker_data_path} 파일에 있는 뉴스만 사용하고 없는 사실을 지어내지
   마세요.
3. events 배열이 비어 있으면 "오늘 수집된 뉴스 없음"이라고만 쓰세요.
4. previous_close의 실제 종가·등락률 숫자를 언급하세요. null이면
   "전일 가격 데이터 없음"이라고 쓰세요.
5. 개별 기사를 하나하나 나열하지 마세요 (그건 다른 메일인 "투자 뉴스
   브리핑"에서 이미 다룹니다). 대신 3~5문장 이내로, 오늘 나온 긍정적
   소식과 부정적 소식이 각각 무엇이었는지, 전체적으로 어느 쪽 뉴스가
   더 많았는지를 짧게 종합하세요.
6. 한국어로 작성하세요.

[출력 형식]
### {ticker} ({company})
전일 종가: (실제 숫자)

(3~5문장 요약)

{ticker_data_path} 파일을 꼭 읽고 시작하세요.
"""


def build_portfolio_synthesis_prompt(ticker_payload):
    ticker = ticker_payload["ticker"]
    return PORTFOLIO_SYNTHESIS_TEMPLATE.format(
        ticker=ticker,
        company=ticker_payload["company"],
        ticker_data_path=f"output/tickers/{ticker}.json",
    )


def render_ticker_fallback_section(stock):
    """Rule-based markdown for one ticker -- used both for the combined
    fallback file and as a per-ticker substitute when Copilot returns
    nothing usable for that specific ticker."""
    sections = []
    header = f"## {stock['ticker']} - {stock['company']}"
    if stock["is_etf"]:
        header += f" (ETF · {stock['sector']} 섹터)" if stock["sector"] else " (ETF)"
    sections.append(header)
    sections.append(format_price_line(stock["previous_close"]))
    sections.append("")

    if not stock["events"]:
        sections.append("최근 24시간 내 유효한 뉴스가 없습니다.")
        sections.append("")
        return sections

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

    return sections


def build_fallback_briefing(ai_input):
    sections = ["# Daily Investment News Briefing (rule-based fallback)", ""]
    sections.append(f"생성 시각(UTC): {ai_input['generated_at_utc']}")
    sections.append("")

    for stock in ai_input["tickers"]:
        sections.extend(render_ticker_fallback_section(stock))
        sections.append("---")
        sections.append("")

    return "\n".join(sections)


def render_macro_fallback(macro_events):
    lines = ["# 매크로 · 경제지표 브리핑 (규칙 기반 대체)", ""]
    if not macro_events:
        lines.append("오늘 수집된 매크로 뉴스가 없습니다.")
        return "\n".join(lines)

    for idx, ev in enumerate(macro_events, start=1):
        lines.append(f"{idx}. {ev['title']}")
        source_line = ev["primary_source"]
        if ev["secondary_sources"]:
            source_line += " (추가 출처: " + ", ".join(ev["secondary_sources"]) + ")"
        lines.append(f"   - 출처: {source_line} / 기사 시간: {ev['published_utc']}")
        lines.append(f"   - 링크: {ev['link']}")
        lines.append("")

    return "\n".join(lines)


def render_ticker_synthesis_fallback(stock):
    """Rule-based (not AI) neutral one-liner per holding -- a count of
    positive/negative-leaning headlines, never a buy/sell/hold call."""
    lines = [f"### {stock['ticker']} ({stock['company']})"]
    lines.append(format_price_line(stock["previous_close"]))
    lines.append("")

    events = stock["events"]
    if not events:
        lines.append("오늘 수집된 뉴스 없음.")
        lines.append("")
        return lines

    positive = sum(1 for e in events if e["pre_direction"] > 0)
    negative = sum(1 for e in events if e["pre_direction"] < 0)
    neutral = len(events) - positive - negative
    lines.append(
        f"오늘 뉴스 {len(events)}건 중 호재 성격 {positive}건, 악재 성격 {negative}건, "
        f"중립 {neutral}건 (규칙 기반 집계이며 AI 요약이 아닙니다). "
        "자세한 개별 뉴스는 같은 날짜의 '투자 뉴스 브리핑' 메일을 참고하세요."
    )
    lines.append("")
    return lines


def main():
    now = datetime.now(timezone.utc)
    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))

    OUTPUT_DIR.mkdir(exist_ok=True)
    tickers_dir = OUTPUT_DIR / "tickers"
    prompts_dir = OUTPUT_DIR / "prompts"
    fallback_dir = OUTPUT_DIR / "fallback"
    tickers_dir.mkdir(exist_ok=True)
    prompts_dir.mkdir(exist_ok=True)
    fallback_dir.mkdir(exist_ok=True)

    ai_input = build_ai_input(watchlist, now)
    (OUTPUT_DIR / "ai_input.json").write_text(
        json.dumps(ai_input, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ticker_order = []
    for ticker_payload in ai_input["tickers"]:
        ticker = ticker_payload["ticker"]
        ticker_order.append(ticker)

        (tickers_dir / f"{ticker}.json").write_text(
            json.dumps(ticker_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (prompts_dir / f"{ticker}.txt").write_text(
            build_ticker_prompt(ticker_payload), encoding="utf-8"
        )
        # Used by the workflow if Copilot returns nothing usable for this
        # specific ticker, so one bad/empty AI response can't blank out an
        # entire ticker's section (or, worse, silently pass as "successful"
        # with nothing in it).
        (fallback_dir / f"{ticker}.md").write_text(
            "\n".join(render_ticker_fallback_section(ticker_payload)), encoding="utf-8"
        )
        # Second email: same idea, but a short neutral synthesis prompt
        # instead of the full itemized briefing prompt.
        (prompts_dir / f"synthesis_{ticker}.txt").write_text(
            build_portfolio_synthesis_prompt(ticker_payload), encoding="utf-8"
        )
        (fallback_dir / f"synthesis_{ticker}.md").write_text(
            "\n".join(render_ticker_synthesis_fallback(ticker_payload)), encoding="utf-8"
        )

    (OUTPUT_DIR / "ticker_order.txt").write_text("\n".join(ticker_order) + "\n", encoding="utf-8")

    fallback = build_fallback_briefing(ai_input)
    (OUTPUT_DIR / "briefing.md").write_text(fallback, encoding="utf-8")

    macro_events = collect_macro_events(now)
    macro_payload = {
        "generated_at_utc": ai_input["generated_at_utc"],
        "hours_back": HOURS_BACK,
        "events": macro_events,
    }
    (OUTPUT_DIR / "macro.json").write_text(
        json.dumps(macro_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (prompts_dir / "macro.txt").write_text(build_macro_prompt(), encoding="utf-8")
    (fallback_dir / "macro.md").write_text(render_macro_fallback(macro_events), encoding="utf-8")

    total_events = sum(t["event_count"] for t in ai_input["tickers"])
    print(
        f"[INFO] Collected {total_events} ticker news events and "
        f"{len(macro_events)} macro events across {len(watchlist)} ticker(s)."
    )


if __name__ == "__main__":
    main()

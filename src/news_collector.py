import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path


# ============================================================
# SETTINGS
# ============================================================

HOURS_BACK = 24
MAX_AI_EVENTS = 20
MIN_IMPORTANCE_SCORE = 70

OUTPUT_DIR = Path("output")
AI_INPUT_PATH = OUTPUT_DIR / "ai_input.json"
COPILOT_PROMPT_PATH = OUTPUT_DIR / "copilot_prompt.txt"
FALLBACK_BRIEFING_PATH = OUTPUT_DIR / "fallback_briefing.md"
FINAL_BRIEFING_PATH = OUTPUT_DIR / "briefing.md"


LOW_VALUE_PHRASES = [
    "is it too late to buy",
    "is it time to buy",
    "is it time to sell",
    "should you buy",
    "should you sell",
    "is a good buy",
    "still a good buy",
    "best stocks to buy",
    "stocks to buy now",
    "where will",
    "price prediction",
    "stock prediction",
    "could soar",
    "could skyrocket",
    "millionaire maker",
]


HIGH_VALUE_TERMS = [
    "earnings",
    "revenue",
    "guidance",
    "forecast",
    "acquisition",
    "acquire",
    "merger",
    "partnership",
    "contract",
    "deal",
    "financing",
    "funding",
    "investment",
    "sec",
    "lawsuit",
    "antitrust",
    "regulator",
    "investigation",
    "ceo",
    "cfo",
    "dividend",
    "buyback",
    "recall",
]


SOURCE_PRIORITY = {
    "sec": 100,
    "newsroom": 98,
    "investor relations": 98,
    "nvidia": 98,
    "apple": 98,
    "microsoft": 98,
    "alphabet": 98,
    "google": 98,
    "meta": 98,
    "broadcom": 98,
    "tesla": 98,
    "eli lilly": 98,
    "reuters": 95,
    "bloomberg": 94,
    "wall street journal": 93,
    "wsj": 93,
    "financial times": 92,
    "new york times": 90,
    "nytimes": 90,
    "cnbc": 88,
    "barron's": 87,
    "marketwatch": 86,
    "semafor": 85,
    "axios": 85,
    "associated press": 84,
    "forbes": 75,
    "business insider": 74,
    "yahoo": 65,
    "investing.com": 60,
    "tradingview": 55,
    "thestreet": 50,
    "stocktwits": 45,
    "moomoo": 40,
}


EVENT_TERMS = [
    "financing",
    "funding",
    "partnership",
    "deal",
    "contract",
    "acquisition",
    "merger",
    "investment",
    "earnings",
    "revenue",
    "guidance",
    "lawsuit",
    "antitrust",
    "investigation",
    "buyback",
    "dividend",
    "ceo",
    "cfo",
]


EARNINGS_TERMS = {
    "earnings", "revenue", "sales", "guidance", "forecast", "margin",
    "profit", "profits", "eps", "cash flow", "free cash flow", "capex",
    "order", "orders", "backlog", "shipment", "shipments",
}

INDUSTRY_TERMS = {
    "ai", "artificial intelligence", "semiconductor", "chip", "chips",
    "data center", "datacenter", "cloud", "gpu", "accelerator",
    "regulation", "regulator", "antitrust", "export", "tariff",
    "supply chain", "foundry", "manufacturing",
}

MARKET_IMPACT_TERMS = {
    "acquisition", "merger", "buyout", "partnership", "contract", "deal",
    "financing", "funding", "investment", "investigation", "lawsuit",
    "antitrust", "recall", "ban", "approval", "approved", "ceo", "cfo",
    "buyback", "dividend", "guidance",
}

POSITIVE_TERMS = {
    "beats", "beat", "raises guidance", "raise guidance", "record",
    "wins", "win", "approval", "approved", "expands", "expansion",
    "partnership", "contract", "buyback", "dividend increase",
    "revenue growth", "profit growth", "strong demand", "funding",
    "financing", "investment",
}

NEGATIVE_TERMS = {
    "misses", "miss", "cuts guidance", "cut guidance", "downgrade",
    "lawsuit", "investigation", "antitrust", "recall", "ban",
    "export restriction", "export restrictions", "weak demand",
    "decline", "falls", "fall", "drops", "drop", "plunges", "plunge",
    "resigns", "resign", "delay", "delays",
}


# ============================================================
# NEWS COLLECTION
# ============================================================

def get_google_news(ticker, company_name):
    query = f'"{company_name}" OR "{ticker}"'
    encoded_query = urllib.parse.quote(query)

    url = (
        "https://news.google.com/rss/search"
        f"?q={encoded_query}"
        "&hl=en-US"
        "&gl=US"
        "&ceid=US:en"
    )

    print(f"\nSearching news for {ticker} ({company_name})...")
    print("-" * 70)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_BACK)

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
            articles.append(
                {
                    "title": title,
                    "source": source,
                    "published": published,
                    "link": link,
                }
            )

    articles.sort(key=lambda article: article["published"], reverse=True)
    return articles


# ============================================================
# NORMALIZATION / SOURCE QUALITY
# ============================================================

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"\s+-\s+[^-]+$", "", title)

    replacements = {
        "$500b": "500 billion",
        "$500bn": "500 billion",
        "half-trillion": "500 billion",
        "half trillion": "500 billion",
    }

    for old, new in replacements.items():
        title = title.replace(old, new)

    title = re.sub(r"\$([0-9,.]+)\s*billion", r"\1 billion", title)
    title = re.sub(r"\$([0-9,.]+)\s*million", r"\1 million", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    return title


def tokenize(title):
    stopwords = {
        "the", "a", "an", "and", "or", "to", "for", "of", "on", "in",
        "with", "as", "at", "from", "by", "is", "are", "be", "its",
        "this", "that", "after", "amid", "new", "says",
    }

    return {
        word
        for word in normalize_title(title).split()
        if len(word) >= 3 and word not in stopwords
    }


def source_score(source):
    source_lower = source.lower()

    for key, score in SOURCE_PRIORITY.items():
        if key in source_lower:
            return score

    return 50


# ============================================================
# FILTER / DEDUP / EVENT CLUSTERING
# ============================================================

def is_low_value_article(title):
    normalized = normalize_title(title)

    if any(term in normalized for term in HIGH_VALUE_TERMS):
        return False

    return any(phrase in normalized for phrase in LOW_VALUE_PHRASES)


def filter_low_value_articles(articles):
    kept = []
    removed = []

    for article in articles:
        if is_low_value_article(article["title"]):
            removed.append(article)
        else:
            kept.append(article)

    return kept, removed


def title_similarity(title_a, title_b):
    a = normalize_title(title_a)
    b = normalize_title(title_b)
    return SequenceMatcher(None, a, b).ratio()


def remove_near_duplicates(articles, threshold=0.82):
    unique_articles = []
    duplicates = []

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


def event_similarity(article_a, article_b):
    tokens_a = tokenize(article_a["title"])
    tokens_b = tokenize(article_b["title"])

    if not tokens_a or not tokens_b:
        return 0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)

    normalized_a = normalize_title(article_a["title"])
    normalized_b = normalize_title(article_b["title"])

    shared_event_term = any(
        term in normalized_a and term in normalized_b
        for term in EVENT_TERMS
    )

    shared_big_number = (
        "500 billion" in normalized_a
        and "500 billion" in normalized_b
    )

    if shared_big_number:
        return max(jaccard, 0.75)

    if shared_event_term and len(intersection) >= 3:
        return max(jaccard, 0.60)

    return jaccard


def cluster_events(articles, threshold=0.48):
    clusters = []

    for article in articles:
        matched_cluster = None

        for cluster in clusters:
            representative = cluster["representative"]
            if event_similarity(article, representative) >= threshold:
                matched_cluster = cluster
                break

        if matched_cluster is None:
            clusters.append(
                {
                    "representative": article,
                    "articles": [article],
                }
            )
            continue

        matched_cluster["articles"].append(article)

        current_rep = matched_cluster["representative"]
        if source_score(article["source"]) > source_score(current_rep["source"]):
            matched_cluster["representative"] = article

    return clusters


def cluster_merge_score(cluster_a, cluster_b):
    best_score = 0

    for article_a in cluster_a["articles"]:
        for article_b in cluster_b["articles"]:
            best_score = max(
                best_score,
                event_similarity(article_a, article_b),
            )

    return best_score


def merge_event_clusters(clusters, threshold=0.60):
    merged = []

    for cluster in clusters:
        matched_index = None

        for index, existing in enumerate(merged):
            if cluster_merge_score(cluster, existing) >= threshold:
                matched_index = index
                break

        if matched_index is None:
            merged.append(
                {
                    "representative": cluster["representative"],
                    "articles": list(cluster["articles"]),
                }
            )
            continue

        existing = merged[matched_index]
        existing["articles"].extend(cluster["articles"])

        current_rep = existing["representative"]
        new_rep = cluster["representative"]

        if source_score(new_rep["source"]) > source_score(current_rep["source"]):
            existing["representative"] = new_rep

    return merged


# ============================================================
# ZERO-COST PRE-SCORING
# ============================================================

def contains_any(text, terms):
    return any(term in text for term in terms)


def score_direction(text):
    positive = sum(1 for term in POSITIVE_TERMS if term in text)
    negative = sum(1 for term in NEGATIVE_TERMS if term in text)

    net = positive - negative

    if net >= 3:
        return 3
    if net == 2:
        return 2
    if net == 1:
        return 1
    if net == -1:
        return -1
    if net == -2:
        return -2
    if net <= -3:
        return -3
    return 0


def pre_score_cluster(ticker, company_name, cluster):
    rep = cluster["representative"]
    title = normalize_title(rep["title"])
    all_titles = " ".join(
        normalize_title(article["title"])
        for article in cluster["articles"][:20]
    )
    text = f"{title} {all_titles}"

    ticker_l = ticker.lower()
    company_l = company_name.lower()

    # 1) Direct company impact: 0-25
    direct = 25 if (ticker_l in text or company_l in text) else 10

    # 2) Earnings / cash-flow impact: 0-20
    earnings_hits = sum(1 for term in EARNINGS_TERMS if term in text)
    earnings = min(20, earnings_hits * 5)

    # 3) Industry / sector impact: 0-15
    industry_hits = sum(1 for term in INDUSTRY_TERMS if term in text)
    industry = min(15, industry_hits * 3)

    # 4) Potential stock-price impact: 0-15
    market_hits = sum(1 for term in MARKET_IMPACT_TERMS if term in text)
    price_impact = min(15, market_hits * 4)

    # 5) Novelty: 0-10, based on recency
    age_hours = (
        datetime.now(timezone.utc) - rep["published"]
    ).total_seconds() / 3600

    if age_hours <= 6:
        novelty = 10
    elif age_hours <= 12:
        novelty = 8
    elif age_hours <= 18:
        novelty = 7
    else:
        novelty = 6

    # 6) Broader market impact: 0-10
    coverage_count = len(cluster["articles"])
    broad = 0
    if coverage_count >= 20:
        broad = 10
    elif coverage_count >= 10:
        broad = 8
    elif coverage_count >= 5:
        broad = 6
    elif coverage_count >= 2:
        broad = 4
    else:
        broad = 2

    if "500 billion" in text or "trillion" in text:
        broad = min(10, broad + 2)

    # 7) Source reliability: 0-5
    reliability = max(1, round(source_score(rep["source"]) / 20))
    reliability = min(5, reliability)

    total = (
        direct
        + earnings
        + industry
        + price_impact
        + novelty
        + broad
        + reliability
    )

    total = max(0, min(100, total))

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
    scoring = pre_score_cluster(ticker, company_name, cluster)

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
        **scoring,
    }


# ============================================================
# OUTPUT FOR COPILOT CLI + ZERO-COST FALLBACK
# ============================================================

def direction_label(direction):
    labels = {
        3: "강한 호재",
        2: "호재",
        1: "약한 호재",
        0: "중립",
        -1: "약한 악재",
        -2: "악재",
        -3: "강한 악재",
    }
    return labels.get(direction, "중립")


def importance_label(score):
    if score >= 90:
        return "매우 중요"
    if score >= 80:
        return "중요"
    if score >= 70:
        return "의미 있는 뉴스"
    if score >= 50:
        return "참고"
    return "제외"


def make_fallback_comment(event):
    breakdown = event["score_breakdown"]
    strongest = sorted(
        breakdown.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]

    names = {
        "direct_impact": "기업 직접 영향",
        "earnings_cashflow": "실적/현금흐름",
        "industry_sector": "산업 영향",
        "stock_price_impact": "주가 영향 가능성",
        "novelty": "신규성",
        "broader_market": "시장 파급력",
        "source_reliability": "출처 신뢰도",
    }

    reasons = ", ".join(names[key] for key, _ in strongest)
    return f"규칙 기반 사전평가에서 {reasons} 항목의 점수가 상대적으로 높았습니다."


def write_fallback_briefing(ticker, company_name, events):
    qualifying = [
        event
        for event in events
        if event["importance_score"] >= MIN_IMPORTANCE_SCORE
    ]

    lines = [
        f"# {ticker} - {company_name} 투자 뉴스 브리핑",
        "",
        "> AI 분석을 사용할 수 없을 때 자동 생성되는 0원 규칙 기반 fallback입니다.",
        "> 제목/출처/보도 빈도만으로 평가하므로 최종 투자판단용이 아니라 선별용입니다.",
        "",
        f"- 최근 검색 범위: {HOURS_BACK}시간",
        f"- AI 후보 이벤트: {len(events)}개",
        f"- 중요도 {MIN_IMPORTANCE_SCORE}점 이상: {len(qualifying)}개",
        "",
        "## 주요 뉴스",
        "",
    ]

    if not qualifying:
        lines.append("70점 이상 이벤트가 없습니다.")
    else:
        for index, event in enumerate(qualifying, start=1):
            lines.extend(
                [
                    f"### {index}. {event['title']}",
                    "",
                    f"- 중요도: **{event['importance_score']}/100 ({importance_label(event['importance_score'])})**",
                    f"- 방향성: **{event['direction']:+d} ({direction_label(event['direction'])})**",
                    f"- 대표 출처: {event['source']}",
                    f"- 동일 이벤트 보도 수: {event['coverage_count']}건",
                    f"- 투자 코멘트: {make_fallback_comment(event)}",
                    f"- 링크: {event['link']}",
                    "",
                ]
            )

    FALLBACK_BRIEFING_PATH.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    FINAL_BRIEFING_PATH.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def write_copilot_prompt(ticker, company_name, events):
    event_json = json.dumps(
        events,
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
당신은 미국 주식 기관투자자를 지원하는 보수적인 투자 뉴스 애널리스트입니다.

대상 기업:
- Ticker: {ticker}
- Company: {company_name}

아래 NEWS EVENTS는 최근 {HOURS_BACK}시간의 뉴스 제목, 출처, 기사 시간,
동일 사건 보도 수, 그리고 Python 규칙 기반 사전점수를 포함합니다.

중요 규칙:
1. 제공된 정보만 사용하세요. 기사 제목에 없는 사실을 지어내지 마세요.
2. 같은 사건을 반복해서 쓰지 마세요.
3. 회사 IR/공식 발표, SEC, Reuters, Bloomberg, WSJ, FT 등 신뢰도 높은 출처를 우선하세요.
4. Python의 importance_score는 참고용 사전점수일 뿐입니다. 당신이 최종 중요도를 다시 판단하세요.
5. 최종 중요도는 반드시 아래 100점 체계를 사용하세요.
   - 기업 직접 영향: 25
   - 실적/현금흐름 영향: 20
   - 산업/섹터 영향: 15
   - 주가 영향 가능성: 15
   - 신규성: 10
   - 시장 파급력: 10
   - 출처 신뢰도: 5
6. 방향성:
   +3 강한 호재 / +2 호재 / +1 약한 호재 / 0 중립 /
   -1 약한 악재 / -2 악재 / -3 강한 악재
7. 최종 중요도 70점 이상만 브리핑에 포함하세요.
8. 한국어로 작성하세요.
9. 기사 제목만으로 판단하기 어려운 부분은 "추가 확인 필요"라고 명시하세요.
10. 링크는 NEWS EVENTS에 제공된 링크를 그대로 사용하세요.

아래 형식을 정확히 따르세요.

# {ticker} - {company_name} 투자 뉴스 브리핑

## 핵심 요약
3~5문장.

## 주요 뉴스

### 1. [한국어로 자연스럽게 정리한 제목]
- 종목: {ticker}
- 중요도: 00/100 (등급)
- 방향성: +0 (중립)
- 한줄 요약:
- 투자 코멘트:
- 주가 영향:
- 추가 확인 필요:
- 대표 출처:
- 기사 시간:
- 원문 링크:

필요한 뉴스 수만 작성하세요. 70점 미만은 제외하세요.

NEWS EVENTS:
{event_json}
""".strip()

    COPILOT_PROMPT_PATH.write_text(prompt, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    ticker = "NVDA"
    company_name = "NVIDIA"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    raw_news = get_google_news(ticker, company_name)

    filtered_news, low_value_removed = filter_low_value_articles(raw_news)

    deduplicated_news, duplicates_removed = remove_near_duplicates(
        filtered_news
    )

    first_pass_clusters = cluster_events(deduplicated_news)
    merged_clusters = merge_event_clusters(first_pass_clusters)

    event_records = [
        build_event_record(
            ticker,
            company_name,
            event_id=index,
            cluster=cluster,
        )
        for index, cluster in enumerate(merged_clusters, start=1)
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

    ai_events = event_records[:MAX_AI_EVENTS]

    AI_INPUT_PATH.write_text(
        json.dumps(ai_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    write_copilot_prompt(
        ticker,
        company_name,
        ai_events,
    )

    write_fallback_briefing(
        ticker,
        company_name,
        ai_events,
    )

    print("\n" + "=" * 70)
    print("NEWS PROCESSING SUMMARY")
    print("=" * 70)
    print(f"Raw articles:             {len(raw_news)}")
    print(f"Low-value removed:        {len(low_value_removed)}")
    print(f"Near-duplicates removed:  {len(duplicates_removed)}")
    print(f"First-pass clusters:      {len(first_pass_clusters)}")
    print(f"Merged event clusters:    {len(merged_clusters)}")
    print(f"Prepared for AI:          {len(ai_events)}")
    print(f"Rule-based 70+ events:    {sum(1 for e in ai_events if e['importance_score'] >= MIN_IMPORTANCE_SCORE)}")
    print()
    print(f"AI input:                 {AI_INPUT_PATH}")
    print(f"Copilot prompt:           {COPILOT_PROMPT_PATH}")
    print(f"Fallback briefing:        {FALLBACK_BRIEFING_PATH}")
    print(f"Final briefing:           {FINAL_BRIEFING_PATH}")
    print()
    print("No paid API was called by this Python script.")

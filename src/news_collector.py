import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime


# ============================================================
# SETTINGS
# ============================================================

HOURS_BACK = 24

# AI에 너무 많은 뉴스를 보내지 않기 위한 상한
MAX_AI_ARTICLES = 20

# GitHub Models
GITHUB_MODELS_URL = (
    "https://models.github.ai/inference/chat/completions"
)

MODEL = "openai/gpt-4.1"


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
    "nvidia": 98,
    "reuters": 95,
    "bloomberg": 94,
    "wall street journal": 93,
    "wsj": 93,
    "financial times": 92,
    "ft": 92,
    "new york times": 90,
    "nytimes": 90,
    "cnbc": 88,
    "barron's": 87,
    "marketwatch": 86,
    "semafor": 85,
    "axios": 85,
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


# ============================================================
# GOOGLE NEWS
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

    print(
        f"\nSearching news for "
        f"{ticker} ({company_name})..."
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
    ) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)

    now = datetime.now(timezone.utc)

    cutoff = now - timedelta(
        hours=HOURS_BACK
    )

    articles = []

    for item in root.findall(".//item"):

        title = item.findtext(
            "title",
            "",
        ).strip()

        link = item.findtext(
            "link",
            "",
        ).strip()

        pub_date = item.findtext(
            "pubDate",
            "",
        ).strip()

        source_element = item.find(
            "source"
        )

        source = (
            source_element.text.strip()
            if source_element is not None
            and source_element.text
            else "Unknown"
        )

        try:

            published = (
                parsedate_to_datetime(
                    pub_date
                )
            )

            if published.tzinfo is None:

                published = (
                    published.replace(
                        tzinfo=timezone.utc
                    )
                )

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

    articles.sort(
        key=lambda x: x["published"],
        reverse=True,
    )

    return articles


# ============================================================
# TITLE NORMALIZATION
# ============================================================

def normalize_title(title):

    title = title.lower()

    title = re.sub(
        r"\s+-\s+[^-]+$",
        "",
        title,
    )

    replacements = {
        "$500b": "500 billion",
        "$500bn": "500 billion",
        "half-trillion": "500 billion",
        "half trillion": "500 billion",
    }

    for old, new in replacements.items():
        title = title.replace(
            old,
            new,
        )

    title = re.sub(
        r"\$([0-9,.]+)\s*billion",
        r"\1 billion",
        title,
    )

    title = re.sub(
        r"\$([0-9,.]+)\s*million",
        r"\1 million",
        title,
    )

    title = re.sub(
        r"[^a-z0-9\s]",
        " ",
        title,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    return title


def tokenize(title):

    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "for",
        "of",
        "on",
        "in",
        "with",
        "as",
        "at",
        "from",
        "by",
        "is",
        "are",
        "be",
        "its",
        "this",
        "that",
        "after",
        "amid",
        "new",
        "says",
    }

    words = normalize_title(
        title
    ).split()

    return {
        word
        for word in words
        if len(word) >= 3
        and word not in stopwords
    }


# ============================================================
# SOURCE QUALITY
# ============================================================

def source_score(source):

    source_lower = source.lower()

    for key, score in (
        SOURCE_PRIORITY.items()
    ):

        if key in source_lower:
            return score

    return 50


# ============================================================
# LOW VALUE FILTER
# ============================================================

def is_low_value_article(title):

    normalized = normalize_title(
        title
    )

    if any(
        term in normalized
        for term in HIGH_VALUE_TERMS
    ):
        return False

    return any(
        phrase in normalized
        for phrase in LOW_VALUE_PHRASES
    )


def filter_low_value_articles(
    articles,
):

    kept = []
    removed = []

    for article in articles:

        if is_low_value_article(
            article["title"]
        ):
            removed.append(
                article
            )

        else:
            kept.append(
                article
            )

    return kept, removed


# ============================================================
# DUPLICATE REMOVAL
# ============================================================

def title_similarity(
    title_a,
    title_b,
):

    a = normalize_title(
        title_a
    )

    b = normalize_title(
        title_b
    )

    return SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


def remove_near_duplicates(
    articles,
    threshold=0.82,
):

    unique_articles = []
    duplicates = []

    for article in articles:

        duplicate_index = None

        for index, existing in enumerate(
            unique_articles
        ):

            similarity = (
                title_similarity(
                    article["title"],
                    existing["title"],
                )
            )

            if similarity >= threshold:

                duplicate_index = index
                break

        if duplicate_index is None:

            unique_articles.append(
                article
            )

            continue

        existing = unique_articles[
            duplicate_index
        ]

        if (
            source_score(
                article["source"]
            )
            >
            source_score(
                existing["source"]
            )
        ):

            unique_articles[
                duplicate_index
            ] = article

            duplicates.append(
                {
                    "article": existing,
                    "kept": article,
                }
            )

        else:

            duplicates.append(
                {
                    "article": article,
                    "kept": existing,
                }
            )

    return (
        unique_articles,
        duplicates,
    )


# ============================================================
# EVENT SIMILARITY
# ============================================================

def event_similarity(
    article_a,
    article_b,
):

    tokens_a = tokenize(
        article_a["title"]
    )

    tokens_b = tokenize(
        article_b["title"]
    )

    if not tokens_a or not tokens_b:
        return 0

    intersection = (
        tokens_a & tokens_b
    )

    union = (
        tokens_a | tokens_b
    )

    jaccard = (
        len(intersection)
        / len(union)
    )

    normalized_a = (
        normalize_title(
            article_a["title"]
        )
    )

    normalized_b = (
        normalize_title(
            article_b["title"]
        )
    )

    shared_event_term = any(
        term in normalized_a
        and term in normalized_b
        for term in EVENT_TERMS
    )

    shared_big_number = (
        "500 billion" in normalized_a
        and
        "500 billion" in normalized_b
    )

    if shared_big_number:

        return max(
            jaccard,
            0.75,
        )

    if (
        shared_event_term
        and len(intersection) >= 3
    ):

        return max(
            jaccard,
            0.60,
        )

    return jaccard


# ============================================================
# FIRST PASS EVENT CLUSTERING
# ============================================================

def cluster_events(
    articles,
    threshold=0.48,
):

    clusters = []

    for article in articles:

        matched_cluster = None

        for cluster in clusters:

            representative = (
                cluster[
                    "representative"
                ]
            )

            similarity = (
                event_similarity(
                    article,
                    representative,
                )
            )

            if similarity >= threshold:

                matched_cluster = (
                    cluster
                )

                break

        if matched_cluster is None:

            clusters.append(
                {
                    "representative":
                        article,
                    "articles":
                        [article],
                }
            )

            continue

        matched_cluster[
            "articles"
        ].append(
            article
        )

        current_rep = (
            matched_cluster[
                "representative"
            ]
        )

        if (
            source_score(
                article["source"]
            )
            >
            source_score(
                current_rep["source"]
            )
        ):

            matched_cluster[
                "representative"
            ] = article

    return clusters


# ============================================================
# SECOND PASS CLUSTER MERGING
# ============================================================

def cluster_merge_score(
    cluster_a,
    cluster_b,
):

    best_score = 0

    for article_a in (
        cluster_a["articles"]
    ):

        for article_b in (
            cluster_b["articles"]
        ):

            score = event_similarity(
                article_a,
                article_b,
            )

            if score > best_score:
                best_score = score

    return best_score


def merge_event_clusters(
    clusters,
    threshold=0.60,
):

    merged = []

    for cluster in clusters:

        matched_index = None

        for index, existing in enumerate(
            merged
        ):

            score = (
                cluster_merge_score(
                    cluster,
                    existing,
                )
            )

            if score >= threshold:

                matched_index = index
                break

        if matched_index is None:

            merged.append(
                {
                    "representative":
                        cluster[
                            "representative"
                        ],
                    "articles":
                        list(
                            cluster[
                                "articles"
                            ]
                        ),
                }
            )

            continue

        existing = merged[
            matched_index
        ]

        existing[
            "articles"
        ].extend(
            cluster["articles"]
        )

        current_rep = (
            existing[
                "representative"
            ]
        )

        new_rep = (
            cluster[
                "representative"
            ]
        )

        if (
            source_score(
                new_rep["source"]
            )
            >
            source_score(
                current_rep["source"]
            )
        ):

            existing[
                "representative"
            ] = new_rep

    return merged


# ============================================================
# PREPARE AI INPUT
# ============================================================

def prepare_ai_articles(
    clusters,
):

    ranked = sorted(
        clusters,
        key=lambda cluster: (
            len(cluster["articles"]),
            source_score(
                cluster[
                    "representative"
                ]["source"]
            ),
            cluster[
                "representative"
            ]["published"],
        ),
        reverse=True,
    )

    selected = []

    for cluster in ranked[
        :MAX_AI_ARTICLES
    ]:

        rep = cluster[
            "representative"
        ]

        selected.append(
            {
                "title":
                    rep["title"],
                "source":
                    rep["source"],
                "published":
                    rep[
                        "published"
                    ].isoformat(),
                "link":
                    rep["link"],
                "coverage_count":
                    len(
                        cluster[
                            "articles"
                        ]
                    ),
            }
        )

    return selected


# ============================================================
# GITHUB MODELS AI
# ============================================================

def analyze_with_github_models(
    ticker,
    company_name,
    articles,
):

    token = os.environ.get(
        "GITHUB_TOKEN"
    )

    if not token:

        print(
            "\nERROR: "
            "GITHUB_TOKEN not found."
        )

        return None

    article_text = json.dumps(
        articles,
        ensure_ascii=False,
        indent=2,
    )

    system_prompt = """
You are an equity research news analyst
supporting an institutional investor.

Analyze only the news supplied by the user.

Do not invent facts.

Several headlines may refer to the same event.
Treat them as one event.

Give more weight to:
1. Company official sources
2. SEC / regulatory sources
3. Reuters
4. Bloomberg
5. Wall Street Journal
6. Financial Times
7. CNBC
8. Other financial media

Ignore clickbait and low-information articles.

Write the final answer in Korean.

Focus on information that could matter to:
- earnings
- revenue
- margins
- capex
- cash flow
- valuation
- competitive position
- regulation
- major customers
- major suppliers
- strategic risk

For each important event explain:
- what happened
- why it matters
- likely stock impact
- whether the impact is short-term or long-term
- important risks or uncertainties

Do not repeat the same event.

Use this exact structure:

# 핵심 요약

# 주요 뉴스

## 1. [뉴스 제목]
- 중요도:
- 내용:
- 투자 포인트:
- 주가 영향:
- 리스크:
- 출처:

# 투자자 관점 종합

# 추가 확인 필요 사항
""".strip()

    user_prompt = f"""
Company: {company_name}
Ticker: {ticker}

Below are news event representatives
collected during the last {HOURS_BACK} hours.

coverage_count means approximately how
many articles were grouped into that event.

Analyze the news for an institutional
U.S. equity investor.

NEWS DATA:

{article_text}
""".strip()

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "temperature": 0.2,
        "max_tokens": 3000,
    }

    data = json.dumps(
        payload
    ).encode(
        "utf-8"
    )

    request = urllib.request.Request(
        GITHUB_MODELS_URL,
        data=data,
        method="POST",
        headers={
            "Accept":
                "application/vnd.github+json",
            "Authorization":
                f"Bearer {token}",
            "Content-Type":
                "application/json",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            result = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        return (
            result[
                "choices"
            ][0][
                "message"
            ][
                "content"
            ]
        )

    except Exception as exc:

        print(
            "\nGitHub Models "
            "request failed:"
        )

        print(exc)

        return None


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    ticker = "NVDA"
    company_name = "NVIDIA"

    raw_news = get_google_news(
        ticker,
        company_name,
    )

    (
        filtered_news,
        low_value_removed,
    ) = (
        filter_low_value_articles(
            raw_news
        )
    )

    (
        deduplicated_news,
        duplicates_removed,
    ) = (
        remove_near_duplicates(
            filtered_news
        )
    )

    event_clusters = (
        cluster_events(
            deduplicated_news
        )
    )

    merged_clusters = (
        merge_event_clusters(
            event_clusters
        )
    )

    print(
        "\n"
        + "=" * 70
    )

    print(
        "NEWS PROCESSING SUMMARY"
    )

    print(
        "=" * 70
    )

    print(
        f"Raw articles:             "
        f"{len(raw_news)}"
    )

    print(
        f"Low-value removed:        "
        f"{len(low_value_removed)}"
    )

    print(
        f"Near-duplicates removed:  "
        f"{len(duplicates_removed)}"
    )

    print(
        f"First-pass clusters:      "
        f"{len(event_clusters)}"
    )

    print(
        f"Merged event clusters:    "
        f"{len(merged_clusters)}"
    )

    ai_articles = (
        prepare_ai_articles(
            merged_clusters
        )
    )

    print(
        f"Sent to AI:               "
        f"{len(ai_articles)}"
    )

    print(
        "\n"
        + "=" * 70
    )

    print(
        "STARTING GITHUB MODELS "
        "AI ANALYSIS"
    )

    print(
        "=" * 70
    )

    analysis = (
        analyze_with_github_models(
            ticker,
            company_name,
            ai_articles,
        )
    )

    if analysis:

        print(
            "\n"
            + "=" * 70
        )

        print(
            "INVESTMENT NEWS BRIEFING"
        )

        print(
            "=" * 70
        )

        print()

        print(
            analysis
        )

    else:

        print(
            "\nAI analysis was "
            "not generated."
        )

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime


HOURS_BACK = 24
MAX_OUTPUT = 30


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

    articles.sort(
        key=lambda article: article["published"],
        reverse=True,
    )

    return articles


def normalize_title(title):
    title = title.lower()

    title = re.sub(r"\s+-\s+[^-]+$", "", title)

    title = title.replace("$500b", "500 billion")
    title = title.replace("$500bn", "500 billion")
    title = title.replace("half-trillion", "500 billion")
    title = title.replace("half trillion", "500 billion")

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

    title = re.sub(r"[^a-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

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

    words = normalize_title(title).split()

    return {
        word
        for word in words
        if len(word) >= 3 and word not in stopwords
    }


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

    return any(
        phrase in normalized
        for phrase in LOW_VALUE_PHRASES
    )


def title_similarity(title_a, title_b):
    a = normalize_title(title_a)
    b = normalize_title(title_b)

    return SequenceMatcher(None, a, b).ratio()


def filter_low_value_articles(articles):
    kept = []
    removed = []

    for article in articles:
        if is_low_value_article(article["title"]):
            removed.append(article)
        else:
            kept.append(article)

    return kept, removed


def remove_near_duplicates(articles, threshold=0.82):
    unique_articles = []
    duplicates = []

    for article in articles:
        duplicate_index = None

        for index, existing in enumerate(unique_articles):
            similarity = title_similarity(
                article["title"],
                existing["title"],
            )

            if similarity >= threshold:
                duplicate_index = index
                break

        if duplicate_index is None:
            unique_articles.append(article)
            continue

        existing = unique_articles[duplicate_index]

        if source_score(article["source"]) > source_score(
            existing["source"]
        ):
            unique_articles[duplicate_index] = article

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

            similarity = event_similarity(
                article,
                representative,
            )

            if similarity >= threshold:
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

        if source_score(article["source"]) > source_score(
            current_rep["source"]
        ):
            matched_cluster["representative"] = article

    return clusters


def cluster_merge_score(cluster_a, cluster_b):
    best_score = 0

    for article_a in cluster_a["articles"]:
        for article_b in cluster_b["articles"]:
            score = event_similarity(
                article_a,
                article_b,
            )

            if score > best_score:
                best_score = score

    return best_score


def merge_event_clusters(clusters, threshold=0.60):
    merged = []

    for cluster in clusters:
        matched_index = None

        for index, existing in enumerate(merged):
            score = cluster_merge_score(
                cluster,
                existing,
            )

            if score >= threshold:
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

        existing["articles"].extend(
            cluster["articles"]
        )

        current_rep = existing["representative"]
        new_rep = cluster["representative"]

        if source_score(new_rep["source"]) > source_score(
            current_rep["source"]
        ):
            existing["representative"] = new_rep

    return merged


def print_article(index, article):
    print(f"[{index}] {article['title']}")
    print(f"Source: {article['source']}")
    print(f"Time:   {article['published']}")
    print(f"Link:   {article['link']}")
    print()


if __name__ == "__main__":
    ticker = "NVDA"
    company_name = "NVIDIA"

    raw_news = get_google_news(
        ticker,
        company_name,
    )

    filtered_news, low_value_removed = filter_low_value_articles(
        raw_news
    )

    deduplicated_news, duplicates_removed = remove_near_duplicates(
        filtered_news
    )

    event_clusters = cluster_events(
        deduplicated_news
    )

    merged_clusters = merge_event_clusters(
        event_clusters
    )

    representatives = [
        cluster["representative"]
        for cluster in merged_clusters
    ]

    representatives.sort(
        key=lambda article: article["published"],
        reverse=True,
    )

    print("\n" + "=" * 70)
    print("NEWS PROCESSING SUMMARY")
    print("=" * 70)

    print(f"Raw articles:             {len(raw_news)}")
    print(f"Low-value removed:        {len(low_value_removed)}")
    print(f"Near-duplicates removed:  {len(duplicates_removed)}")
    print(f"First-pass clusters:      {len(event_clusters)}")
    print(f"Merged event clusters:    {len(merged_clusters)}")
    print(f"Remaining for AI review:  {len(representatives)}")

    if duplicates_removed:
        print("\nSOURCE PRIORITY / DUPLICATE EXAMPLES")
        print("-" * 70)

        for item in duplicates_removed[:10]:
            print(
                f"- Removed: "
                f"{item['article']['source']} | "
                f"{item['article']['title']}"
            )

            print(
                f"  Kept:    "
                f"{item['kept']['source']} | "
                f"{item['kept']['title']}"
            )

            print()

    print("\nMERGED EVENT CLUSTERS")
    print("-" * 70)

    large_clusters = sorted(
        merged_clusters,
        key=lambda cluster: len(cluster["articles"]),
        reverse=True,
    )

    for index, cluster in enumerate(
        large_clusters[:10],
        start=1,
    ):
        representative = cluster["representative"]

        print(
            f"[Cluster {index}] "
            f"{len(cluster['articles'])} articles"
        )

        print(
            f"Representative: "
            f"{representative['source']} | "
            f"{representative['title']}"
        )

        for article in cluster["articles"][:10]:
            print(
                f"  - {article['source']} | "
                f"{article['title']}"
            )

        print()

    print("\nARTICLES REMAINING FOR LATER AI ANALYSIS")
    print("-" * 70)

    for index, article in enumerate(
        representatives[:MAX_OUTPUT],
        start=1,
    ):
        print_article(
            index,
            article,
        )

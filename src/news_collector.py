import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime


# ---------------------------------------------------------
# Settings
# ---------------------------------------------------------

HOURS_BACK = 24
MAX_OUTPUT = 30

# These phrases are usually opinion / price-chasing articles rather than
# genuinely new corporate events.
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

# Keep these even if a title happens to contain an opinion-like phrase.
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
    """
    Normalize a headline so near-duplicate headlines can be compared.
    """
    title = title.lower()

    # Remove publisher suffixes such as " - Bloomberg.com"
    title = re.sub(r"\s+-\s+[^-]+$", "", title)

    # Normalize money / numbers a little
    title = re.sub(r"\$([0-9,.]+)\s*billion", r"\1 billion", title)
    title = re.sub(r"\$([0-9,.]+)\s*million", r"\1 million", title)

    # Remove punctuation
    title = re.sub(r"[^a-z0-9\s]", " ", title)

    # Collapse spaces
    title = re.sub(r"\s+", " ", title).strip()

    return title


def is_low_value_article(title):
    """
    Remove obvious opinion / click-driven articles while preserving
    headlines that contain high-value corporate event terms.
    """
    normalized = normalize_title(title)

    if any(term in normalized for term in HIGH_VALUE_TERMS):
        return False

    return any(
        phrase in normalized
        for phrase in LOW_VALUE_PHRASES
    )


def title_similarity(title_a, title_b):
    """
    Compare two normalized headlines.
    1.0 = effectively identical.
    """
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
    """
    Remove only very similar headlines.

    Important:
    This does NOT try to solve full event clustering.
    Different headlines about the same event will be handled later by AI.
    """
    unique_articles = []
    duplicates = []

    for article in articles:
        duplicate_of = None

        for existing in unique_articles:
            similarity = title_similarity(
                article["title"],
                existing["title"],
            )

            if similarity >= threshold:
                duplicate_of = existing
                break

        if duplicate_of:
            duplicates.append(
                {
                    "article": article,
                    "duplicate_of": duplicate_of,
                }
            )
        else:
            unique_articles.append(article)

    return unique_articles, duplicates


def print_article(index, article):
    print(f"[{index}] {article['title']}")
    print(f"Source: {article['source']}")
    print(f"Time:   {article['published']}")
    print(f"Link:   {article['link']}")
    print()


if __name__ == "__main__":
    ticker = "NVDA"
    company_name = "NVIDIA"

    raw_news = get_google_news(ticker, company_name)

    filtered_news, low_value_removed = filter_low_value_articles(
        raw_news
    )

    unique_news, duplicates_removed = remove_near_duplicates(
        filtered_news
    )

    print("\n" + "=" * 70)
    print("NEWS PROCESSING SUMMARY")
    print("=" * 70)

    print(f"Raw articles:             {len(raw_news)}")
    print(f"Low-value removed:        {len(low_value_removed)}")
    print(f"Near-duplicates removed:  {len(duplicates_removed)}")
    print(f"Remaining for AI review:  {len(unique_news)}")

    if low_value_removed:
        print("\nLOW-VALUE ARTICLES REMOVED")
        print("-" * 70)

        for article in low_value_removed[:10]:
            print(f"- {article['title']}")

    if duplicates_removed:
        print("\nNEAR-DUPLICATE ARTICLES REMOVED")
        print("-" * 70)

        for item in duplicates_removed[:10]:
            print(f"- Removed: {item['article']['title']}")
            print(f"  Kept:    {item['duplicate_of']['title']}")
            print()

    print("\nARTICLES REMAINING FOR LATER AI ANALYSIS")
    print("-" * 70)

    for index, article in enumerate(
        unique_news[:MAX_OUTPUT],
        start=1,
    ):
        print_article(index, article)

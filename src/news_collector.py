import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime


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
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

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
        reverse=True
    )

    return articles


if __name__ == "__main__":
    ticker = "NVDA"
    company_name = "NVIDIA"

    news = get_google_news(ticker, company_name)

    print(f"\nFound {len(news)} articles from the last 24 hours.\n")

    for index, article in enumerate(news[:20], start=1):
        print(f"[{index}] {article['title']}")
        print(f"Source: {article['source']}")
        print(f"Time:   {article['published']}")
        print(f"Link:   {article['link']}")
        print()

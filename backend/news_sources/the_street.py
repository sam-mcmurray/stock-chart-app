import feedparser
import calendar
import hashlib
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class TheStreetRSSParser:
    def __init__(self, rss_url="https://www.thestreet.com/.rss/full/"):
        self.rss_url = rss_url
        self.feed = None

    def fetch_feed(self):
        """Fetches and parses the RSS feed."""
        self.feed = feedparser.parse(self.rss_url)
        if getattr(self.feed, "bozo", False) and not self.feed.entries:
            logger.warning("TheStreet RSS failed: %s", getattr(self.feed, "bozo_exception", "invalid feed"))

    def parse_entry(self, entry):
        """Parses a single RSS entry into structured fields."""
        content_html = entry.get("content", [{}])[0].get("value", "")
        soup = BeautifulSoup(content_html, "html.parser")

        return {
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "author": entry.get("dc_creator", "Unknown"),
            "published": entry.get("published", ""),
            "description": entry.get("description", ""),
            "categories": [tag.term for tag in entry.get("tags", [])],
            "thumbnail": entry.get("media_thumbnail", [{}])[0].get("url", None),
            "enclosure": entry.get("enclosures", [{}])[0].get("url", None),
            "content_html": content_html,
            "content_text": soup.get_text(separator="\n", strip=True),
            "media_content": self._parse_media_content(entry)
        }

    def _parse_media_content(self, entry):
        """Extracts media:content fields into structured form."""
        media_blocks = []
        for m in entry.get("media_content", []):
            media_blocks.append({
                "url": m.get("url"),
                "type": m.get("type"),
                "medium": m.get("medium"),
                "title": m.get("media_title", None),
                "description": m.get("media_description", None),
            })
        return media_blocks

    def parse_all(self):
        """Fetches and parses all entries in the feed."""
        if self.feed is None:
            self.fetch_feed()

        return [self.parse_entry(entry) for entry in self.feed.entries]

    def print_summary(self, max_entries=5):
        """Prints a readable summary of parsed entries."""
        articles = self.parse_all()
        for i, article in enumerate(articles[:max_entries]):
            print(f"{'='*80}")
            print(f"Title      : {article['title']}")
            print(f"Link       : {article['link']}")
            print(f"Author     : {article['author']}")
            print(f"Published  : {article['published']}")
            print(f"Categories : {', '.join(article['categories'])}")
            print(f"Thumbnail  : {article['thumbnail']}")
            print(f"Content:\n{article['content_text'][:500]}...\n")


def load_thestreet_rss():
    parser = TheStreetRSSParser()
    parser.fetch_feed()
    if not parser.feed or not parser.feed.entries:
        return []
    articles = []
    for entry in parser.feed.entries:
        title = entry.get("title", "")
        url = entry.get("link", "")
        if not title or not url:
            continue
        content_html = entry.get("content", [{}])[0].get("value", "")
        description = entry.get("description", "")
        summary = BeautifulSoup(description or content_html, "html.parser").get_text(" ", strip=True)
        articles.append({
            "source": "TheStreet",
            "headline": title,
            "summary": summary,
            "url": url,
            "datetime": int(calendar.timegm(entry.get("published_parsed"))) if entry.get("published_parsed") else 0,
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest(),
            "related": [],
            "image": entry.get("media_thumbnail", [{}])[0].get("url", ""),
            "author": entry.get("dc_creator") or entry.get("author") or "",
        })
    return articles


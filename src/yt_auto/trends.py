from __future__ import annotations

import re
import os
from collections import Counter
from typing import Dict, List, Tuple
from urllib.parse import quote_plus

import feedparser
import requests


STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "this", "that", "from", "into", "after", "before",
    "have", "has", "had", "are", "was", "were", "about", "real", "story", "stories", "history", "news",
    "video", "shorts", "com", "www", "http", "https", "amp", "nbsp", "based", "week", "practice",
    "association", "reader", "digest", "american", "report", "dispatch",
    "verywell", "books", "book", "frontiers", "media", "help", "vocal", "will", "they", "your", "people",
    "national", "geographic", "magazine", "documentary", "documentaries", "factual", "factualamerica",
    "america", "world", "what", "why", "how", "facts",
}


class TrendScout:
    def __init__(self, timezone: str = "Asia/Karachi") -> None:
        self.timezone = timezone
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "yt-auto/1.0"})

    def _tokenize(self, text: str) -> List[str]:
        clean = re.sub(r"<[^>]+>", " ", text)
        terms = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", clean.lower())
        out = []
        for term in terms:
            if term in STOPWORDS:
                continue
            if term.endswith(".com") or term.endswith(".org"):
                continue
            out.append(term)
        return out

    def fetch_news_terms(self, keywords: List[str], limit: int = 30) -> Dict[str, int]:
        query = " OR ".join(keywords[:6])
        rss_url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = self.session.get(rss_url, timeout=8)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception:
            return {}
        counter: Counter[str] = Counter()
        for entry in feed.entries[:80]:
            title = getattr(entry, "title", "")
            for term in self._tokenize(title):
                counter[term] += 1
        return dict(counter.most_common(limit))

    def fetch_google_related_terms(self, keywords: List[str], limit: int = 30) -> Dict[str, int]:
        if str(os.getenv("YT_ENABLE_PYTRENDS", "0")).lower() not in {"1", "true", "yes", "on"}:
            return {}
        try:
            from pytrends.request import TrendReq
        except Exception:
            return {}

        try:
            pytrends = TrendReq(hl="en-US", tz=300)
            pytrends.build_payload(keywords[:5], timeframe="now 7-d")
            related = pytrends.related_queries()
        except Exception:
            return {}

        counter: Counter[str] = Counter()
        for keyword in keywords[:5]:
            bucket = related.get(keyword) if isinstance(related, dict) else None
            if not bucket:
                continue
            top = bucket.get("top")
            if top is None:
                continue
            try:
                rows = top.head(20)
                for _, row in rows.iterrows():
                    term = str(row.get("query", "")).strip().lower()
                    value = int(row.get("value", 0))
                    if term:
                        for token in self._tokenize(term):
                            counter[token] += max(1, value // 20)
            except Exception:
                continue

        return dict(counter.most_common(limit))

    def collect(self, keywords: List[str], limit: int = 25) -> List[Tuple[str, int]]:
        news = self.fetch_news_terms(keywords, limit=limit)
        related = self.fetch_google_related_terms(keywords, limit=limit)

        merged: Counter[str] = Counter()
        merged.update(news)
        merged.update(related)

        if not merged:
            for kw in keywords:
                for token in self._tokenize(kw):
                    merged[token] += 1

        return merged.most_common(limit)

import logging
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from src.config import load_routes

logger = logging.getLogger(__name__)

URGENCY_HOURS = 2  # posts younger than this get URGENT flag


def fetch_deals(routes: dict) -> list[dict]:
    """Fetch and filter promotional flight deals from RSS feeds."""
    keywords_dest = [k.lower() for k in routes.get("deals_keywords_destination", [])]
    keywords_promo = [k.lower() for k in routes.get("deals_keywords_promo", [])]
    feeds = routes.get("deals_feeds", [])

    all_deals = []
    for feed_url in feeds:
        deals = _parse_feed(feed_url, keywords_dest, keywords_promo)
        all_deals.extend(deals)
        logger.info("Feed %s → %d deals found", feed_url, len(deals))

    # Deduplicate by link
    seen = set()
    unique = []
    for d in all_deals:
        if d["link"] not in seen:
            seen.add(d["link"])
            unique.append(d)

    return sorted(unique, key=lambda x: x["published_at"], reverse=True)


def _parse_feed(feed_url: str, keywords_dest: list, keywords_promo: list) -> list[dict]:
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        logger.warning("Failed to parse feed %s: %s", feed_url, e)
        return []

    deals = []
    for entry in parsed.entries:
        title = getattr(entry, "title", "").lower()
        summary = getattr(entry, "summary", "").lower()
        text = f"{title} {summary}"

        has_destination = any(k in text for k in keywords_dest)
        has_promo = any(k in text for k in keywords_promo)

        if not (has_destination or has_promo):
            continue

        published = _parse_date(entry)
        age_hours = _age_hours(published)

        flags = []
        if age_hours <= URGENCY_HOURS:
            flags.append("URGENTE")
        if any(k in text for k in ["black friday", "cyber monday"]):
            flags.append("CAMPANHA")
        if any(k in text for k in ["relâmpago", "flash", "relampago"]):
            flags.append("EXPIRA_BREVE")

        # Extract price if mentioned (R$ N.NNN or R$N.NNN)
        import re
        price_match = re.search(r"R\$\s*(\d[\d.,]+)", getattr(entry, "title", ""))
        price_str = price_match.group(0) if price_match else None

        deals.append({
            "title": getattr(entry, "title", ""),
            "link": getattr(entry, "link", feed_url),
            "published_at": published.isoformat() if published else "",
            "age_hours": round(age_hours, 1),
            "flags": flags,
            "price_mention": price_str,
            "has_destination_keyword": has_destination,
            "has_promo_keyword": has_promo,
            "source": feed_url.split("/")[2],  # domain only
        })

    return deals


def _parse_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            import time as _time
            return datetime.fromtimestamp(_time.mktime(t), tz=timezone.utc)
    return None


def _age_hours(dt: datetime | None) -> float:
    if not dt:
        return 999.0
    now = datetime.now(tz=timezone.utc)
    return (now - dt).total_seconds() / 3600

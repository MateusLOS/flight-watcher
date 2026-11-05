"""
SerpApi Google Flights verifier.

Uses real-time Google Flights data (via SerpApi) to cross-check prices for
MAXIMO and EXCELENTE tier offers before alerting. Consumes ~1 API call per
offer verified — the free tier has 100 calls/month, so only top offers are
verified.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
_ENDPOINT = "https://serpapi.com/search"
_MAX_VERIFICATIONS = 5  # hard cap per run to protect the monthly quota


def _search_google_flights(origin: str, destination: str, dep_date: str) -> float | None:
    """
    Returns the cheapest one-way price (BRL) from Google Flights,
    or None if the search fails or returns no results.
    """
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": dep_date,
        "currency": "BRL",
        "hl": "pt",
        "type": "2",  # one-way
        "api_key": SERPAPI_KEY,
    }
    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("SerpApi request failed (%s→%s %s): %s", origin, destination, dep_date, exc)
        return None

    prices: list[float] = []
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section) or []:
            p = flight.get("price")
            if isinstance(p, (int, float)) and p > 0:
                prices.append(float(p))

    if not prices:
        logger.info("SerpApi returned no prices for %s→%s %s", origin, destination, dep_date)
        return None

    return min(prices)


def verify_top_offers(offers: list[dict]) -> list[dict]:
    """
    For each offer in `offers`, query Google Flights and add:
        offer["serp_price"]    — cheapest price found (BRL), or None
        offer["serp_verified"] — True if SerpApi confirmed a comparable price

    Only the first _MAX_VERIFICATIONS offers are queried (quota guard).
    """
    if not SERPAPI_KEY:
        logger.info("SERPAPI_KEY not set — skipping real-time verification")
        return offers

    verified = []
    calls = 0
    for offer in offers:
        if calls >= _MAX_VERIFICATIONS:
            offer["serp_price"] = None
            offer["serp_verified"] = False
            verified.append(offer)
            continue

        origin = offer.get("origin", "")
        dest = offer.get("destination", "")
        dep_date = offer.get("departure_date", "")

        if not (origin and dest and dep_date):
            offer["serp_price"] = None
            offer["serp_verified"] = False
            verified.append(offer)
            continue

        logger.info("SerpApi: verifying %s→%s %s", origin, dest, dep_date)
        serp_price = _search_google_flights(origin, dest, dep_date)
        calls += 1

        offer["serp_price"] = serp_price
        # "verified" means Google Flights confirms a price within 20% of what we found
        if serp_price is not None and offer.get("price_brl", 0) > 0:
            ratio = serp_price / offer["price_brl"]
            offer["serp_verified"] = 0.80 <= ratio <= 1.20
        else:
            offer["serp_verified"] = False

        verified.append(offer)
        time.sleep(0.5)  # polite rate limiting

    logger.info("SerpApi: verified %d/%d offers (%d calls used)", calls, len(offers), calls)
    return verified

"""
Flight search using Travelpayouts API (free, no Amadeus required).
Register at: https://www.travelpayouts.com/developers/api
Get token at: https://www.travelpayouts.com/programs/100/tools/api
"""
import logging
import time

import requests

from src.config import TRAVELPAYOUTS_TOKEN

logger = logging.getLogger(__name__)

BASE = "https://api.travelpayouts.com"

_last_call: float = 0.0
MIN_INTERVAL = 0.35  # seconds between calls to stay under rate limits


def _get(endpoint: str, params: dict, timeout: int = 20) -> dict | None:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_call = time.time()

    params = {**params, "token": TRAVELPAYOUTS_TOKEN, "currency": "brl"}
    try:
        resp = requests.get(f"{BASE}{endpoint}", params=params, timeout=timeout)
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("API call failed %s: %s", endpoint, e)
        return None


def get_cheapest_destinations(origin: str, departure_month: str | None = None) -> list[dict]:
    """
    Returns cheapest one-way fares from origin to any destination.
    One call returns ALL destinations — very quota-efficient.
    departure_month: "YYYY-MM" (optional)
    """
    params: dict = {
        "origin": origin,
        "one_way": "true",
        "limit": 1000,
        "sorting": "price",
    }
    if departure_month:
        params["departure_at"] = departure_month

    data = _get("/v2/prices/latest", params)
    if not data or not data.get("success"):
        return []

    results = []
    for item in data.get("data", []):
        try:
            results.append({
                "origin": item.get("origin", origin),
                "destination": item["destination"],
                "departure_date": item.get("departure_at", "")[:10],
                "price_brl": float(item["value"]),
                "carrier": item.get("airline", ""),
                "stops": item.get("transfers", 0),
                "duration_hours": (item.get("duration") or 0) / 60,
                "booking_link": _build_link(
                    origin, item["destination"],
                    item.get("departure_at", "")[:10]
                ),
                "fare_basis": "",
                "layovers": [],
                "source": "travelpayouts",
            })
        except (KeyError, ValueError):
            continue
    return results


def get_cheapest_dates(origin: str, destination: str,
                       departure_month: str | None = None) -> list[dict]:
    """
    Returns cheapest fares for a specific route across all dates in a month.
    departure_month: "YYYY-MM"
    """
    params: dict = {
        "origin": origin,
        "destination": destination,
        "one_way": "true",
        "calendar_type": "departure_date",
        "limit": 30,
        "sorting": "price",
    }
    if departure_month:
        params["departure_at"] = departure_month

    data = _get("/aviasales/v3/prices_for_dates", params)
    if not data or not data.get("success"):
        return []

    results = []
    for item in data.get("data", []):
        try:
            results.append({
                "origin": origin,
                "destination": destination,
                "departure_date": item.get("departure_at", "")[:10],
                "price_brl": float(item["price"]),
                "carrier": item.get("airline", ""),
                "stops": item.get("transfers", 0),
                "duration_hours": (item.get("duration") or 0) / 60,
                "booking_link": _build_link(origin, destination,
                                            item.get("departure_at", "")[:10]),
                "fare_basis": "",
                "layovers": [],
                "source": "travelpayouts",
            })
        except (KeyError, ValueError):
            continue
    return sorted(results, key=lambda x: x["price_brl"])


def get_exchange_rate() -> dict:
    """Fetch current BRL/USD rate from free exchangerate API."""
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        brl_per_usd = resp.json()["rates"].get("BRL", 5.5)
        return {
            "brl_usd": brl_per_usd,
            "brl_usd_30d": brl_per_usd,
            "brl_usd_90d_avg": brl_per_usd,
        }
    except Exception as e:
        logger.warning("Exchange rate fetch failed: %s. Using 5.5 fallback.", e)
        return {"brl_usd": 5.5, "brl_usd_30d": 5.5, "brl_usd_90d_avg": 5.5}


def _build_link(origin: str, destination: str, departure: str) -> str:
    """Deep link to Aviasales.com.br (Travelpayouts partner) for the route."""
    date_compact = departure.replace("-", "") if departure else ""
    return f"https://www.aviasales.com.br/search/{origin}{date_compact}{destination}1"

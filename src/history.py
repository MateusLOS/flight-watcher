import json
from datetime import date
from pathlib import Path

import numpy as np
from scipy import stats

from src.config import HISTORY_PATH


def load_history() -> dict:
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_history(history: dict) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def route_key(origin: str, destination: str, season: str = "all") -> str:
    return f"{origin}→{destination}:{season}"


def get_season(travel_date: str) -> str:
    """Returns 'high' or 'low' season based on the travel month."""
    month = int(travel_date[5:7])
    return "high" if month in (6, 7, 8, 12) else "low"


def update_route_price(history: dict, origin: str, destination: str,
                       price_brl: float, travel_date: str) -> None:
    season = get_season(travel_date)
    today = date.today().isoformat()

    for key in [route_key(origin, destination), route_key(origin, destination, season)]:
        if key not in history:
            history[key] = {
                "observations": [],
                "last_updated": today,
                "last_7d": [],
                "last_14d": [],
                "last_30d": [],
            }

        entry = history[key]
        entry["observations"].append({"date": today, "price": price_brl})
        entry["last_updated"] = today

        # Keep rolling windows
        all_prices = [o["price"] for o in entry["observations"]]
        # Last 7/14/30 days (by index since we store daily)
        entry["last_7d"] = all_prices[-7:]
        entry["last_14d"] = all_prices[-14:]
        entry["last_30d"] = all_prices[-30:]

        # Recompute percentiles if enough data
        if len(all_prices) >= 5:
            entry["percentiles"] = {
                "p10": float(np.percentile(all_prices, 10)),
                "p25": float(np.percentile(all_prices, 25)),
                "p50": float(np.percentile(all_prices, 50)),
                "p75": float(np.percentile(all_prices, 75)),
                "p90": float(np.percentile(all_prices, 90)),
            }
            entry["all_time_min"] = float(min(all_prices))
            entry["n_obs"] = len(all_prices)
            if len(all_prices) >= 2:
                entry["std"] = float(np.std(all_prices))
        else:
            entry["n_obs"] = len(all_prices)

        entry["last_price"] = price_brl


def get_route_stats(history: dict, origin: str, destination: str,
                    travel_date: str) -> dict | None:
    season = get_season(travel_date)
    # Prefer season-specific, fall back to all-time
    for key in [route_key(origin, destination, season), route_key(origin, destination)]:
        if key in history and "percentiles" in history[key]:
            return history[key]
    return None

from datetime import date, datetime

import numpy as np

BOOKING_WINDOWS = {
    "europa":        {"sweet_min": 90,  "sweet_max": 210, "too_early": 300},
    "america_norte": {"sweet_min": 70,  "sweet_max": 180, "too_early": 270},
    "asia":          {"sweet_min": 120, "sweet_max": 270, "too_early": 365},
}

WEIGHTS = {
    "price":      0.35,
    "trend":      0.20,
    "timing":     0.20,
    "volatility": 0.15,
    "fx":         0.05,
    "quality":    0.05,
}

TIER_THRESHOLDS = {
    "MAXIMO":      75,   # antes 85 — histórico raso não atinge 85 facilmente
    "EXCELENTE":   58,   # antes 70
    "BOM":         42,   # antes 55
    "INFORMATIVO": 0,    # novo: mostra qualquer oferta encontrada (fase cold start)
}


def price_score(price: float, hist: dict) -> float:
    if not hist or "percentiles" not in hist:
        return 50.0  # neutro sem histórico

    p = hist["percentiles"]
    n = hist.get("n_obs", 0)
    confidence = min(1.0, n / 30)

    if price < p["p10"]:
        base = 90.0 + (p["p10"] - price) / max(p["p10"], 1) * 10
    elif price < p["p25"]:
        rng = max(p["p25"] - p["p10"], 1)
        base = 70.0 + (p["p25"] - price) / rng * 20
    elif price < p["p50"]:
        rng = max(p["p50"] - p["p25"], 1)
        base = 45.0 + (p["p50"] - price) / rng * 25
    elif price < p["p75"]:
        rng = max(p["p75"] - p["p50"], 1)
        base = 20.0 + (p["p75"] - price) / rng * 25
    else:
        base = max(0.0, 20.0 - (price - p["p75"]) / max(p["p75"], 1) * 20)

    base = min(100.0, base)
    return base * confidence + 50.0 * (1 - confidence)


def trend_score(price_brl: float, hist: dict, brl_usd: float, brl_usd_30d: float) -> float:
    if not hist:
        return 50.0

    last_7d = hist.get("last_7d", [])
    last_30d = hist.get("last_30d", [])

    if not last_7d or not last_30d:
        return 50.0

    # Normalize to USD for fair comparison
    price_usd = price_brl / brl_usd
    avg_7d_usd = (sum(last_7d) / len(last_7d)) / brl_usd
    avg_30d_usd = (sum(last_30d) / len(last_30d)) / brl_usd_30d

    drop_7d = (avg_7d_usd - price_usd) / max(avg_7d_usd, 1) * 100
    drop_30d = (avg_30d_usd - price_usd) / max(avg_30d_usd, 1) * 100

    score = 50.0 + drop_30d * 2 + drop_7d * 1.5
    return min(100.0, max(0.0, score))


def timing_score(departure_date: str, region: str) -> float:
    dep = datetime.strptime(departure_date, "%Y-%m-%d").date()
    days = (dep - date.today()).days

    if days <= 0:
        return 0.0

    w = BOOKING_WINDOWS.get(region, BOOKING_WINDOWS["europa"])

    if w["sweet_min"] <= days <= w["sweet_max"]:
        return 100.0
    if days > w["too_early"]:
        # Very early: good inventory but uncertain pricing
        excess = days - w["too_early"]
        return max(40.0, 60.0 - excess * 0.1)
    if days < 42:
        # Too late: last-minute premium
        return max(5.0, days / 42 * 20)
    # Early edge: too_early → sweet_max
    if days > w["sweet_max"]:
        rng = w["too_early"] - w["sweet_max"]
        return 100.0 - (days - w["sweet_max"]) / max(rng, 1) * 40
    # Late edge: 42 → sweet_min
    rng = w["sweet_min"] - 42
    return 20.0 + (days - 42) / max(rng, 1) * 80


def volatility_score(price: float, hist: dict) -> float:
    last_14d = hist.get("last_14d", []) if hist else []
    if len(last_14d) < 3:
        return 50.0  # neutro sem dados suficientes

    avg = np.mean(last_14d)
    std = np.std(last_14d)
    cv = std / max(avg, 1)  # coeficiente de variação

    if cv > 0.20 and price < avg:
        return 90.0  # volátil e barato agora → compre já
    if cv > 0.20:
        return 60.0  # volátil mas não está barato
    if cv < 0.08:
        return 40.0  # estável → pode esperar
    return 55.0


def fx_score(brl_usd: float, brl_usd_90d_avg: float) -> float:
    if not brl_usd_90d_avg:
        return 50.0
    # BRL mais forte = passagens internacionais mais baratas em termos reais
    stronger_pct = (brl_usd_90d_avg - brl_usd) / max(brl_usd_90d_avg, 1) * 100
    return min(100.0, max(0.0, 50.0 + stronger_pct * 5))


def quality_score(stops: int, duration_hours: float, fare_basis: str = "") -> float:
    score = 100.0
    score -= stops * 15
    if duration_hours > 14:
        score -= (duration_hours - 14) * 2
    # Promotional fare classes (W, L, S, V prefix) get a bonus
    if fare_basis and fare_basis[0].upper() in "WLSV":
        score = min(100.0, score + 10)
    return max(0.0, score)


def compute_opportunity_score(
    price: float,
    hist: dict,
    departure_date: str,
    region: str,
    brl_usd: float,
    brl_usd_30d: float,
    brl_usd_90d_avg: float,
    stops: int,
    duration_hours: float,
    fare_basis: str = "",
) -> dict:
    scores = {
        "price":      price_score(price, hist),
        "trend":      trend_score(price, hist, brl_usd, brl_usd_30d),
        "timing":     timing_score(departure_date, region),
        "volatility": volatility_score(price, hist),
        "fx":         fx_score(brl_usd, brl_usd_90d_avg),
        "quality":    quality_score(stops, duration_hours, fare_basis),
    }

    total = sum(scores[k] * WEIGHTS[k] for k in scores)

    tier = "RESUMO"
    for t, threshold in TIER_THRESHOLDS.items():
        if total >= threshold:
            tier = t
            break

    is_volatile = hist.get("last_14d") and len(hist["last_14d"]) >= 3 and (
        np.std(hist["last_14d"]) / max(np.mean(hist["last_14d"]), 1) > 0.20
    )

    return {
        "total": round(total, 1),
        "tier": tier,
        "breakdown": {k: round(v, 1) for k, v in scores.items()},
        "is_volatile": bool(is_volatile),
        "is_high_season": hist is None or True,  # filled by caller
    }

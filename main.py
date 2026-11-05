"""
Flight Watcher — entrypoint principal.
Uso:
    python main.py                  # Execução completa
    python main.py --dry-run        # Sem enviar Telegram
    python main.py --test-notify    # Envia mensagem de teste
    python main.py --deals-only     # Apenas verifica promoções RSS
    python main.py --origin CNF     # Testa somente uma origem
"""
import argparse
import logging
import os
from datetime import date, timedelta

from src.config import DRY_RUN, SERPAPI_KEY, load_routes, get_all_destination_airports, get_destination_region
from src.history import load_history, save_history, update_route_price, get_route_stats
from src.scorer import compute_opportunity_score
from src.searcher import get_cheapest_destinations, get_cheapest_dates, get_exchange_rate
from src.deals import fetch_deals
from src.serp_verifier import verify_top_offers
from src.notifier import notify_offers, notify_daily_summary, notify_deals, notify_test, notify_error, notify_no_results

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-notify", action="store_true")
    p.add_argument("--deals-only", action="store_true")
    p.add_argument("--origin", default=None, help="Restrict to one origin IATA code")
    return p.parse_args()


def _months_in_window(travel_window: dict) -> list[str]:
    """Returns list of 'YYYY-MM' strings covering the travel window."""
    start = date.fromisoformat(travel_window["from"]).replace(day=1)
    end = date.fromisoformat(travel_window["to"]).replace(day=1)
    months = []
    cur = start
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        # Advance one month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def run_search(routes: dict, history: dict, fx: dict) -> tuple[dict, list]:
    all_dest_by_region = get_all_destination_airports(routes)
    target_set = {code for codes in all_dest_by_region.values() for code in codes}
    travel_window = routes["travel_window"]
    thresholds = routes["thresholds_brl"]
    hubs = routes["hubs"]
    origins = routes["origins"]
    months = _months_in_window(travel_window)

    offers_by_tier: dict[str, list] = {"MAXIMO": [], "EXCELENTE": [], "BOM": [], "RESUMO": []}
    all_offers: list[dict] = []

    sorted_origins = sorted(origins.items(), key=lambda x: x[1]["priority"])

    for origin_code, origin_info in sorted_origins:
        logger.info("Searching from %s (%s)...", origin_code, origin_info["name"])

        # Phase 1 — cheapest destinations from this origin (1 call = all destinations)
        try:
            raw = get_cheapest_destinations(origin_code)
        except Exception as e:
            notify_error(f"get_cheapest_destinations({origin_code})", e, critical=False)
            raw = []

        relevant = [o for o in raw if o["destination"] in target_set]
        logger.info("  %d relevant destinations found for %s", len(relevant), origin_code)

        if relevant:
            for offer in relevant:
                dest = offer["destination"]
                dep_date = offer.get("departure_date", "")
                region = get_destination_region(dest, routes)
                if not region:
                    continue
                if not (travel_window["from"] <= dep_date <= travel_window["to"]):
                    continue
                threshold = thresholds.get(region, 5000)
                if offer["price_brl"] <= threshold * 1.5:
                    try:
                        _process_offer(offer, region, history, fx, offers_by_tier, all_offers)
                    except Exception as e:
                        notify_error(f"_process_offer {origin_code}→{dest}", e)
        else:
            # Phase 2 fallback — search hub routes month by month
            logger.info("  Fallback: searching hubs month by month for %s", origin_code)
            any_fallback_result = False
            for region, hub_list in hubs.items():
                threshold = thresholds.get(region, 5000)
                for hub in hub_list[:5]:
                    for month in months[:4]:
                        try:
                            date_offers = get_cheapest_dates(origin_code, hub, month)
                        except Exception as e:
                            notify_error(f"get_cheapest_dates({origin_code}→{hub} {month})", e)
                            date_offers = []
                        for offer in date_offers:
                            any_fallback_result = True
                            if offer["price_brl"] <= threshold * 1.3:
                                try:
                                    _process_offer(offer, region, history, fx,
                                                   offers_by_tier, all_offers)
                                except Exception as e:
                                    notify_error(f"_process_offer fallback {origin_code}→{hub}", e)
            if not any_fallback_result:
                notify_no_results(origin_code, "europa")

    return offers_by_tier, all_offers


def _process_offer(offer: dict, region: str, history: dict, fx: dict,
                   offers_by_tier: dict, all_offers: list) -> None:
    origin = offer["origin"]
    dest = offer["destination"]
    dep_date = offer.get("departure_date", "")
    price = offer["price_brl"]

    if not dep_date:
        return

    hist = get_route_stats(history, origin, dest, dep_date)

    score = compute_opportunity_score(
        price=price,
        hist=hist,
        departure_date=dep_date,
        region=region,
        brl_usd=fx["brl_usd"],
        brl_usd_30d=fx["brl_usd_30d"],
        brl_usd_90d_avg=fx["brl_usd_90d_avg"],
        stops=offer.get("stops", 0),
        duration_hours=offer.get("duration_hours", 0.0),
        fare_basis=offer.get("fare_basis", ""),
    )

    enriched = {**offer, "region": region, "score": score, "hist": hist}
    all_offers.append(enriched)
    offers_by_tier[score["tier"]].append(enriched)
    update_route_price(history, origin, dest, price, dep_date)

    logger.info(
        "  %s→%s %s R$%.0f score=%.1f tier=%s",
        origin, dest, dep_date, price, score["total"], score["tier"],
    )


def also_check_via_sp(routes: dict, history: dict, fx: dict,
                      offers_by_tier: dict, all_offers: list) -> None:
    """Multi-origin arbitrage: compare direct CNF flights vs CNF→GRU→destination."""
    bh_to_sp = routes.get("ground_transport_costs_brl", {}).get("BH_to_SP_bus", 180)

    bh_offers = [o for o in all_offers if o["origin"] in ("CNF", "PLU")]
    seen_arbitrage = set()

    for bh_offer in bh_offers:
        dest = bh_offer["destination"]
        dep_date = bh_offer.get("departure_date", "")
        region = bh_offer["region"]
        key = (dest, dep_date[:7])  # dedupe by destination + month

        if key in seen_arbitrage or not dep_date:
            continue
        seen_arbitrage.add(key)

        gru_dates = get_cheapest_dates("GRU", dest, dep_date[:7])
        if not gru_dates:
            continue

        best_gru = min(gru_dates, key=lambda o: o["price_brl"])
        total_via_sp = best_gru["price_brl"] + bh_to_sp

        if total_via_sp < bh_offer["price_brl"] * 0.92:
            savings = bh_offer["price_brl"] - total_via_sp
            logger.info("  Arbitrage: CNF→%s via GRU saves R$%.0f", dest, savings)
            best_gru["arbitrage_note"] = (
                f"Via SP: R${best_gru['price_brl']:,.0f} + R${bh_to_sp} ônibus "
                f"= R${total_via_sp:,.0f} (economiza R${savings:,.0f} vs CNF direto)"
            )
            _process_offer(best_gru, region, history, fx, offers_by_tier, all_offers)


def main():
    args = parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    if args.test_notify:
        notify_test()
        return

    routes = load_routes()
    history = load_history()

    if args.deals_only:
        logger.info("Fetching deals from RSS feeds...")
        deals = fetch_deals(routes)
        logger.info("Found %d relevant deals", len(deals))
        notify_deals(deals)
        return

    logger.info("Fetching exchange rates...")
    try:
        fx = get_exchange_rate()
    except Exception as e:
        notify_error("get_exchange_rate()", e, critical=False)
        fx = {"brl_usd": 5.5, "brl_usd_30d": 5.5, "brl_usd_90d_avg": 5.5}
    logger.info("BRL/USD: %.2f", fx["brl_usd"])

    if args.origin:
        routes["origins"] = {
            k: v for k, v in routes["origins"].items()
            if k == args.origin.upper()
        }

    logger.info("Starting flight search...")
    offers_by_tier, all_offers = run_search(routes, history, fx)

    logger.info("Checking multi-origin arbitrage...")
    also_check_via_sp(routes, history, fx, offers_by_tier, all_offers)

    logger.info(
        "Done. MAXIMO=%d EXCELENTE=%d BOM=%d RESUMO=%d",
        len(offers_by_tier["MAXIMO"]),
        len(offers_by_tier["EXCELENTE"]),
        len(offers_by_tier["BOM"]),
        len(offers_by_tier["RESUMO"]),
    )

    # Real-time cross-check: verify MAXIMO and EXCELENTE offers via Google Flights (SerpApi)
    if SERPAPI_KEY:
        for tier in ("MAXIMO", "EXCELENTE"):
            if offers_by_tier[tier]:
                try:
                    offers_by_tier[tier] = verify_top_offers(offers_by_tier[tier])
                except Exception as e:
                    notify_error("verify_top_offers(SerpApi)", e, critical=False)

    deals = fetch_deals(routes)

    if any(offers_by_tier[t] for t in ("MAXIMO", "EXCELENTE", "BOM")):
        notify_offers(offers_by_tier, routes)

    notify_daily_summary(all_offers, routes)

    if deals:
        notify_deals(deals)

    try:
        save_history(history)
        logger.info("History saved. %d routes tracked.", len(history) - 1)
    except Exception as e:
        notify_error("save_history()", e, critical=True)

    logger.info("Run complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Last-resort catch — notify even if Telegram itself is broken (logs to stderr)
        try:
            notify_error("main() — erro não tratado", e, critical=True)
        except Exception:
            pass
        logger.critical("Unhandled exception: %s", e, exc_info=True)
        raise SystemExit(1)

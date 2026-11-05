"""
SerpApi Google Flights — buscador independente em tempo real.

Funciona de forma autônoma: a cada execução busca a próxima rota da fila de
prioridade (rotação round-robin) usando dados ao vivo do Google Flights (~15min
de cache), independente do Travelpayouts.

Budget: 1 chamada por execução × 3 execuções/dia = ~90 chamadas/mês
(dentro das 100 do plano gratuito).
"""
import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
_ENDPOINT = "https://serpapi.com/search"

# Rotas prioritárias em ordem de importância.
# A cada run, busca a próxima da lista (rotação contínua).
PRIORITY_ROUTES = [
    # (origin, destination, region)
    ("CNF", "LHR", "europa"),
    ("CNF", "CDG", "europa"),
    ("CNF", "MAD", "europa"),
    ("CNF", "LIS", "europa"),
    ("CNF", "FCO", "europa"),
    ("CNF", "AMS", "europa"),
    ("GRU", "LHR", "europa"),
    ("GRU", "CDG", "europa"),
    ("GRU", "MAD", "europa"),
    ("GRU", "MXP", "europa"),
    ("GRU", "FRA", "europa"),
    ("GRU", "AMS", "europa"),
    ("GRU", "LIS", "europa"),
    ("GRU", "JFK", "america_norte"),
    ("GRU", "MIA", "america_norte"),
    ("GRU", "NRT", "asia"),
    ("GRU", "BKK", "asia"),
    ("GRU", "SIN", "asia"),
    ("GIG", "LHR", "europa"),
    ("GIG", "CDG", "europa"),
]

# Datas de partida a testar dentro da janela de viagem.
# Para cada rota, busca o mês mais próximo da lista que ainda não foi buscado.
SEARCH_DATES = [
    "2027-08-04",  # início de agosto (baixa relativa dentro da alta temporada)
    "2027-09-07",  # setembro — pós-alta temporada, geralmente mais barato
    "2027-10-06",  # outubro — baixa temporada europeia
    "2027-11-04",  # novembro
    "2027-12-01",  # dezembro — antes das festas
    "2028-01-07",  # janeiro — baixa temporada
    "2028-02-03",  # fevereiro — carnaval / inverno europeu
]

STATE_PATH = Path(__file__).parent.parent / "data" / "serpapi_state.json"


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"route_index": 0, "date_index": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _build_google_flights_link(origin: str, destination: str, dep_date: str) -> str:
    d = dep_date.replace("-", "")
    return f"https://www.google.com/travel/flights#search;f={origin};t={destination};d={d};tt=o"


def _search(origin: str, destination: str, dep_date: str) -> list[dict]:
    """
    Chama o SerpApi Google Flights para uma rota+data específica.
    Retorna lista de ofertas normalizadas (mesma estrutura do searcher.py).
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
        resp = requests.get(_ENDPOINT, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("SerpApi falhou (%s→%s %s): %s", origin, destination, dep_date, exc)
        return []

    offers = []
    for section in ("best_flights", "other_flights"):
        for itinerary in data.get(section) or []:
            price = itinerary.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue

            flights = itinerary.get("flights") or []
            stops = max(0, len(flights) - 1)
            duration_min = itinerary.get("total_duration", 0)
            carrier = flights[0].get("airline", "") if flights else ""

            # Extrai layovers (aeroportos de conexão)
            layovers = []
            for leg in itinerary.get("layovers") or []:
                code = leg.get("id", "")
                if code:
                    layovers.append(code)

            offers.append({
                "origin": origin,
                "destination": destination,
                "departure_date": dep_date,
                "price_brl": float(price),
                "carrier": carrier,
                "stops": stops,
                "duration_hours": duration_min / 60,
                "booking_link": _build_google_flights_link(origin, destination, dep_date),
                "fare_basis": "",
                "layovers": layovers,
                "source": "serpapi",
            })

    return sorted(offers, key=lambda o: o["price_brl"])


def run_independent_search(calls_budget: int = 1) -> list[dict]:
    """
    Executa a busca independente do SerpApi.

    A cada chamada avança a fila de rotas × datas (round-robin), garantindo
    que ao longo do mês todas as rotas prioritárias sejam cobertas com preços
    ao vivo do Google Flights.

    Retorna lista de ofertas (a mais barata por rota buscada neste run).
    """
    if not SERPAPI_KEY:
        logger.info("SERPAPI_KEY não configurado — busca independente ignorada")
        return []

    state = _load_state()
    route_idx = state.get("route_index", 0) % len(PRIORITY_ROUTES)
    date_idx = state.get("date_index", 0) % len(SEARCH_DATES)

    results: list[dict] = []
    calls_used = 0

    for _ in range(calls_budget):
        origin, destination, region = PRIORITY_ROUTES[route_idx]
        dep_date = SEARCH_DATES[date_idx]

        logger.info(
            "SerpApi (independente): %s→%s em %s", origin, destination, dep_date
        )
        offers = _search(origin, destination, dep_date)
        calls_used += 1

        if offers:
            best = offers[0]
            best["region"] = region
            results.append(best)
            logger.info(
                "  SerpApi encontrou R$ %.0f para %s→%s %s",
                best["price_brl"], origin, destination, dep_date,
            )
        else:
            logger.info("  SerpApi: sem resultados para %s→%s %s", origin, destination, dep_date)

        # Avança: próxima data para a mesma rota; quando esgotar datas, próxima rota
        date_idx = (date_idx + 1) % len(SEARCH_DATES)
        if date_idx == 0:
            route_idx = (route_idx + 1) % len(PRIORITY_ROUTES)

        if calls_used < calls_budget:
            time.sleep(0.5)

    # Salva estado para o próximo run
    _save_state({"route_index": route_idx, "date_index": date_idx})
    logger.info("SerpApi: %d chamada(s) usada(s) neste run", calls_used)
    return results


def verify_offers(offers: list[dict], max_calls: int = 2) -> list[dict]:
    """
    Verificação pontual: confirma preço de ofertas já encontradas pelo
    Travelpayouts. Usa no máximo max_calls chamadas adicionais.
    """
    if not SERPAPI_KEY or not offers:
        return offers

    calls = 0
    for offer in offers:
        if calls >= max_calls:
            break
        origin = offer.get("origin", "")
        dest = offer.get("destination", "")
        dep_date = offer.get("departure_date", "")
        if not (origin and dest and dep_date):
            continue

        logger.info("SerpApi (verificação): %s→%s %s", origin, dest, dep_date)
        fresh = _search(origin, dest, dep_date)
        calls += 1

        if fresh:
            serp_price = fresh[0]["price_brl"]
            offer["serp_price"] = serp_price
            tp_price = offer.get("price_brl", 0)
            offer["serp_verified"] = tp_price > 0 and 0.80 <= (serp_price / tp_price) <= 1.20
        else:
            offer["serp_price"] = None
            offer["serp_verified"] = False

        time.sleep(0.5)

    return offers

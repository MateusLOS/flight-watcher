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
SERPAPI_RUN = os.getenv("SERPAPI_RUN", "false").lower() == "true"
_ENDPOINT = "https://serpapi.com/search"

# Chamadas por execução diária (1x/dia × 8 rotas = 240/mês dentro das 250)
CALLS_PER_DAY = 8

# Rotas prioritárias em ordem de importância.
# Foco: França, Itália e Leste Europeu (destinos prioritários do usuário).
# A cada run, busca as 8 próximas da lista (rotação contínua).
# CNF (BH) primeiro por ser a origem prioritária, depois GRU e GIG.
PRIORITY_ROUTES = [
    # (origin, destination, region)

    # ── FRANÇA ────────────────────────────────────────────────────────────────
    ("CNF", "CDG", "europa"),   # Paris Charles de Gaulle
    ("CNF", "NCE", "europa"),   # Nice — Sul da França
    ("GRU", "CDG", "europa"),
    ("GRU", "NCE", "europa"),
    ("GRU", "LYS", "europa"),   # Lyon
    ("GIG", "CDG", "europa"),

    # ── ITÁLIA ────────────────────────────────────────────────────────────────
    ("CNF", "FCO", "europa"),   # Roma Fiumicino
    ("CNF", "MXP", "europa"),   # Milão Malpensa
    ("GRU", "FCO", "europa"),
    ("GRU", "MXP", "europa"),
    ("GRU", "VCE", "europa"),   # Veneza
    ("GRU", "NAP", "europa"),   # Nápoles
    ("GRU", "BGY", "europa"),   # Milão Bergamo (hub Ryanair — tarifas baratas)
    ("GIG", "FCO", "europa"),

    # ── LESTE EUROPEU ─────────────────────────────────────────────────────────
    ("GRU", "ATH", "europa"),   # Atenas — porta de entrada Grécia/Balcãs
    ("GRU", "IST", "europa"),   # Istambul — hub para Leste Europeu e Oriente
    ("GRU", "WAW", "europa"),   # Varsóvia — hub para Europa Central
    ("GRU", "PRG", "europa"),   # Praga
    ("GRU", "BUD", "europa"),   # Budapeste
    ("GRU", "OTP", "europa"),   # Bucareste
    ("CNF", "ATH", "europa"),
    ("CNF", "IST", "europa"),
    ("GIG", "ATH", "europa"),

    # ── HUBS ÚTEIS (conexões frequentes para destinos acima) ──────────────────
    ("GRU", "LHR", "europa"),   # Londres — conexão frequente para Leste Europeu
    ("GRU", "MAD", "europa"),   # Madrid — conexões Iberia para Leste
    ("GRU", "LIS", "europa"),   # Lisboa — TAP com boas conexões Europa
]

# Datas de partida: foco em set–dez/2027 (prioridade do usuário).
# Agosto e janeiro como margem para validar mais opções.
SEARCH_DATES = [
    "2027-09-08",   # início de setembro — pós-alta temporada, começa baratear
    "2027-10-07",   # outubro — baixa temporada europeia, ótimo custo-benefício
    "2027-11-04",   # novembro — muito barato, especialmente Leste Europeu
    "2027-12-02",   # dezembro — antes das festas de fim de ano
    "2027-09-22",   # segunda quinzena de setembro
    "2027-10-21",   # segunda quinzena de outubro
    "2027-11-18",   # segunda quinzena de novembro
    "2027-12-16",   # segunda quinzena de dezembro (cuidado com alta das festas)
    "2027-08-05",   # agosto — margem para comparar alta temporada
    "2028-01-07",   # janeiro — margem baixa temporada
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
    return f"https://www.kayak.com.br/flights/{origin}-{destination}/{dep_date}?sort=price_a"


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


def run_independent_search() -> list[dict]:
    """
    Executa a busca independente do SerpApi (apenas 1x/dia, run da manhã).

    Faz CALLS_PER_DAY chamadas avançando a fila de rotas × datas (round-robin).
    Com 8 chamadas/dia × 30 dias = 240 chamadas/mês (dentro das 250 do plano).

    Retorna lista de ofertas (a mais barata por rota buscada).
    """
    if not SERPAPI_KEY:
        logger.info("SERPAPI_KEY não configurado — busca independente ignorada")
        return []

    if not SERPAPI_RUN:
        logger.info("SerpApi: não é o run da manhã — pulando busca independente")
        return []

    state = _load_state()
    route_idx = state.get("route_index", 0) % len(PRIORITY_ROUTES)
    date_idx = state.get("date_index", 0) % len(SEARCH_DATES)

    results: list[dict] = []
    calls_used = 0

    for _ in range(CALLS_PER_DAY):
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

        if calls_used < CALLS_PER_DAY:
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

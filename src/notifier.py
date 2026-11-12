import logging

import requests

from src.config import TELEGRAM_BOT_TOKEN, DRY_RUN, load_subscribers

logger = logging.getLogger(__name__)

TIER_EMOJI = {
    "MAXIMO":      "🔴",
    "EXCELENTE":   "🟡",
    "BOM":         "✅",
    "INFORMATIVO": "ℹ️",
    "RESUMO":      "📊",
}

REGION_NAMES = {
    "europa":        "Europa",
    "america_norte": "América do Norte",
    "asia":          "Ásia",
}

MAX_ALERTS_PER_RUN = 8  # avoid flooding
MAX_DEALS_PER_RUN = 3


def notify_error(context: str, error: Exception | str, critical: bool = False) -> None:
    """Send an error notification to Telegram. critical=True pins the message."""
    emoji = "🚨" if critical else "⚠️"
    label = "ERRO CRÍTICO" if critical else "Aviso"
    err_str = str(error)[:800]  # cap length
    msg = (
        f"{emoji} <b>Flight Watcher — {label}</b>\n\n"
        f"<b>Contexto:</b> {context}\n"
        f"<b>Erro:</b> <code>{err_str}</code>\n\n"
        f"<i>O bot continuará rodando. Verifique as configurações se o problema persistir.</i>"
    )
    _send(msg)
    logger.error("[%s] %s: %s", label, context, error)


def notify_status(text: str) -> None:
    """Confirmação de run — enviada para todos os assinantes."""
    _send(text)


def notify_no_results(origin: str, region: str) -> None:
    """Log only — sem resultados é normal quando a API não tem dados cacheados."""
    logger.info("Sem resultados para %s→%s (normal se API sem cache)", origin, region)


def _send(text: str, parse_mode: str = "HTML") -> bool:
    if DRY_RUN:
        print("[DRY RUN] Telegram message:\n", text)
        return True

    recipients = load_subscribers()

    success = True
    for chat_id in recipients:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Telegram send failed (chat_id=%s): %s", chat_id, e)
            success = False
    return success


def notify_offers(offers_by_tier: dict[str, list], routes: dict) -> None:
    """
    Send Telegram notifications grouped by tier.
    offers_by_tier = {"MAXIMO": [...], "EXCELENTE": [...], "BOM": [...]}
    Each offer = {origin, destination, departure_date, price_brl, score, tier, ...}
    """
    total_sent = 0

    for tier in ("MAXIMO", "EXCELENTE", "BOM", "INFORMATIVO"):
        offers = offers_by_tier.get(tier, [])
        offers.sort(key=lambda o: o["score"]["total"], reverse=True)

        # INFORMATIVO: limita a 3 para não inundar
        limit = 3 if tier == "INFORMATIVO" else MAX_ALERTS_PER_RUN

        for offer in offers:
            if total_sent >= limit:
                break
            msg = _format_offer_message(offer)
            _send(msg)
            total_sent += 1


def notify_daily_summary(all_offers: list, routes: dict) -> None:
    """Send one daily summary with the best offer per region."""
    if not all_offers:
        _send("📊 <b>Resumo — sem ofertas hoje</b>\nAPI sem dados novos. Próxima busca às 15h.")
        return

    by_region: dict[str, list] = {}
    for offer in all_offers:
        r = offer.get("region", "europa")
        by_region.setdefault(r, []).append(offer)

    lines = ["✈️ <b>Resumo do dia</b>\n"]

    for region, offers in by_region.items():
        best = min(offers, key=lambda o: o["price_brl"])
        region_name = REGION_NAMES.get(region, region)
        tier = best["score"]["tier"]
        tier_emoji = TIER_EMOJI.get(tier, "")
        hist = best.get("hist") or {}
        p50 = hist.get("percentiles", {}).get("p50")
        vs_media = f" (média R$ {p50:,.0f})" if p50 else ""

        src = best.get("source", "travelpayouts")
        price_tag = "~R$" if src != "serpapi" else "R$"
        lines.append(
            f"{tier_emoji} <b>{region_name}</b>  {best['origin']}→{best['destination']}\n"
            f"   💰 {price_tag} {best['price_brl']:,.0f}{vs_media} · {best['departure_date'][:7]}\n"
            f"   🔗 <a href=\"{best['booking_link']}\">Buscar no Kayak</a>"
        )

    _send("\n\n".join(lines))


def notify_deals(deals: list) -> None:
    """Send notifications for promotional deals found in RSS feeds."""
    if not deals:
        return

    top = deals[:MAX_DEALS_PER_RUN]
    for deal in top:
        flags = deal.get("flags", [])
        flag_str = ""
        if "URGENTE" in flags:
            flag_str = "⚡ URGENTE — "
        elif "CAMPANHA" in flags:
            flag_str = "🛍️ CAMPANHA — "
        elif "EXPIRA_BREVE" in flags:
            flag_str = "⏰ EXPIRA EM BREVE — "

        price_str = f"\n💰 {deal['price_mention']}" if deal.get("price_mention") else ""
        source = deal.get("source", "")

        msg = (
            f"🎯 {flag_str}<b>Promoção detectada</b>\n\n"
            f"{deal['title']}{price_str}\n\n"
            f"🔗 <a href=\"{deal['link']}\">Ver oferta completa</a>\n"
            f"<i>Fonte: {source} · {deal['age_hours']:.0f}h atrás</i>"
        )
        _send(msg)


def notify_test() -> None:
    _send(
        "✅ <b>Flight Watcher — Teste de Conexão</b>\n\n"
        "Bot configurado com sucesso! Você receberá alertas diários de passagens baratas.\n\n"
        "Origens: BH (CNF/PLU) → SP (GRU/CGH) → RJ (GIG/SDU)\n"
        "Destinos: Europa 🇪🇺 | América do Norte 🇺🇸 | Ásia 🌏"
    )


def _format_offer_message(offer: dict) -> str:
    score = offer["score"]
    tier = score["tier"]
    emoji = TIER_EMOJI.get(tier, "📊")

    hist = offer.get("hist") or {}
    p = hist.get("percentiles", {})
    p10_str = f"P10: R$ {p['p10']:,.0f} | " if p.get("p10") else ""
    p50_str = f"Média: R$ {p['p50']:,.0f} | " if p.get("p50") else ""
    min_str = f"Mínimo: R$ {hist['all_time_min']:,.0f}" if hist.get("all_time_min") else ""
    history_line = f"\n📉 {p10_str}{p50_str}{min_str}" if (p10_str or p50_str) else ""

    layovers = offer.get("layovers", [])
    layover_str = f" (via {', '.join(layovers)})" if layovers else ""
    stops = offer.get("stops", 0)

    vol_warning = "\n⏰ <i>Preço muito instável — considere comprar agora</i>" if score.get("is_volatile") else ""
    season_note = ""
    if offer.get("departure_date", "")[5:7] in ("07", "08", "12"):
        season_note = "\n⚠️ <i>Alta temporada — histórico naturalmente mais alto</i>"

    duration_h = offer.get("duration_hours", 0)
    duration_str = f"{int(duration_h)}h{int((duration_h % 1) * 60):02d}min"

    region = offer.get("region", "europa")
    region_name = REGION_NAMES.get(region, region)

    breakdown = score.get("breakdown", {})
    score_detail = (
        f"preço:{breakdown.get('price',0):.0f} "
        f"trend:{breakdown.get('trend',0):.0f} "
        f"timing:{breakdown.get('timing',0):.0f} "
        f"volatil:{breakdown.get('volatility',0):.0f}"
    )

    # Aviso cold start quando histórico é raso
    hist = offer.get("hist") or {}
    n_obs = hist.get("n_obs", 0)
    cold_start = f"\n⚠️ <i>Histórico ainda raso ({n_obs} obs) — score tende a ser conservador</i>" if n_obs < 10 else ""

    # Fonte dos dados e aviso de preço
    source = offer.get("source", "travelpayouts")
    if source == "serpapi":
        source_line = "\n🔍 <i>Fonte: Google Flights (~15min — preço próximo do real)</i>"
        price_note = ""
    else:
        source_line = "\n🗄️ <i>Fonte: Travelpayouts (cache 24–48h)</i>"
        price_note = "\n⚠️ <i>Preço indicativo — confirme o valor atual ao clicar</i>"

    return (
        f"{emoji} <b>OFERTA {tier}</b> — Score: {score['total']}/100\n"
        f"<i>({score_detail})</i>\n\n"
        f"✈️ {offer['origin']} → {offer['destination']} | {region_name}\n"
        f"📅 {offer['departure_date']} · {stops} escala(s){layover_str}\n"
        f"⏱️ {duration_str} total\n"
        f"💰 <b>~R$ {offer['price_brl']:,.0f}</b> (visto em cache)"
        f"{history_line}\n"
        f"🏢 {offer.get('carrier','')}"
        f"{source_line}"
        f"{price_note}"
        f"{cold_start}"
        f"{vol_warning}{season_note}\n\n"
        f"🔗 <a href=\"{offer['booking_link']}\">Buscar no Kayak</a>"
    )

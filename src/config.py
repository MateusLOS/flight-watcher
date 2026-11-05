import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "routes.json"
HISTORY_PATH = BASE_DIR / "data" / "price_history.json"

TRAVELPAYOUTS_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "")  # opcional: segundo destinatário

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
TEST_NOTIFY = os.getenv("TEST_NOTIFY", "false").lower() == "true"


def load_routes() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_all_destination_airports(routes: dict) -> dict[str, list[str]]:
    """Returns {region: [iata_codes]} flattened across all sub-regions."""
    result = {}
    for region, sub_regions in routes["destinations"].items():
        codes = []
        for airports in sub_regions.values():
            codes.extend(airports)
        result[region] = codes
    return result


def get_destination_region(iata: str, routes: dict) -> str | None:
    for region, sub_regions in routes["destinations"].items():
        for airports in sub_regions.values():
            if iata in airports:
                return region
    return None

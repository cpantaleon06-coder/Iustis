from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[1]
DATA = RAIZ / "data"
DIR_INDICE = DATA / "index"

load_dotenv(RAIZ / ".env")


@lru_cache
def cargar_config() -> dict:
    with open(RAIZ / "rag" / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)

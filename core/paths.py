from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parents[1]

if os.name == "nt":
    RUNTIME_DIR = Path.home() / "investment_dashboard_data"
else:
    RUNTIME_DIR = ROOT_DIR

DATA_DIR = RUNTIME_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = RUNTIME_DIR / "output"
DB_PATH = RUNTIME_DIR / "cache.db"


def ensure_dirs() -> None:
    for path in (RAW_DIR, PROCESSED_DIR, OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)

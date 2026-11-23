"""Configuracion aislada de Captacion Web.

La base y las salidas viven bajo `data/captacion_web/`, nunca en
`data/leads.db` ni en los archivos que usa el modo SG-SST.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "captacion_web"
DEFAULT_DB_PATH = DATA_DIR / "captacion_web.db"
DEFAULT_OUTPUT_DIR = DATA_DIR / "runs"
DEFAULT_SOURCE = "fixture-local"
SCHEMA_VERSION = "1"

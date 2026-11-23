"""Regresion de la normalizacion de identidades para caracteres como la 'ñ'.

Solo usa fixtures sinteticos y SQLite en directorios temporales. No abre la
base operativa, `.env` ni servicios externos.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path

from prospector.captacion_web.cli import run_search
from prospector.captacion_web.models import (
    EXTERNAL_ID_LOCAL,
    EXTERNAL_ID_REVISION,
    LOCAL_IDENTITY_VERSION,
    SearchFilters,
    build_identity,
    normalize_identity,
    normalize_text,
)
from prospector.captacion_web.providers import FixtureProvider
from prospector.captacion_web.storage import CaptacionWebStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NINO_COLLISION = FIXTURES / "identity_nino_collision.json"
DELIMITER_COLLISION = FIXTURES / "identity_delimiter_collision.json"
COUNTRIES = FIXTURES / "country_identity.json"
HOMONYMS = FIXTURES / "identity_homonyms.json"

OLD_V1_KEY = "local:etapa1ficticia"
OLD_V2_KEY = "local:v2:etapa1ficticia"
OLD_V3_KEY = "local:v3:etapa1ficticia"
OLD_SEEN_AT = "2026-01-01T00:00:00+00:00"


def _filters(**overrides) -> SearchFilters:
    values = {
        "country": "Colombia",
        "city": "Bogota",
        "category": "Restaurante",
        "keywords": (),
        "location": "",
        "min_rating": 4.5,
        "min_reviews": 30,
    }
    values.update(overrides)
    return SearchFilters(**values)


def _rows(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


class _TemporaryWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "captacion_web.db"
        self.out = self.root / "runs"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestIdentityNormalization(_TemporaryWorkspace):
    def test_direct_identity_el_nino_vs_el_nino_are_different(self) -> None:
        first_kind, first_key = build_identity(
            "fixture-nino-collision-v1", None, "El Niño", "Carrera 10 #20-30", "Bogota", "Colombia"
        )
        second_kind, second_key = build_identity(
            "fixture-nino-collision-v1", None, "El Nino", "Carrera 10 #20-30", "Bogota", "Colombia"
        )

        self.assertEqual(first_kind, EXTERNAL_ID_LOCAL)
        self.assertEqual(second_kind, EXTERNAL_ID_LOCAL)
        self.assertNotEqual(
            first_key,
            second_key,
            "El Niño y El Nino deben producir claves de identidad distintas y no fusionarse",
        )

    def test_two_records_el_nino_keep_two_prospects_and_two_observations(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(NINO_COLLISION),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.counts["prospects_matched"], 2)
        self.assertEqual(report.counts["observations"], 2)

        prospects = _rows(
            self.db,
            "SELECT external_id, name, address_public FROM prospect ORDER BY name",
        )
        self.assertEqual(len(prospects), 2)
        self.assertEqual(len({row["external_id"] for row in prospects}), 2)
        self.assertEqual([row["name"] for row in prospects], ["El Nino", "El Niño"])

        observations = _rows(
            self.db,
            "SELECT p.name, o.rating, o.review_count FROM observation o "
            "JOIN prospect p ON p.id = o.prospect_id ORDER BY p.name",
        )
        self.assertEqual(
            [(row["name"], row["rating"], row["review_count"]) for row in observations],
            [("El Nino", 4.5, 35), ("El Niño", 4.8, 80)],
        )

    def test_repeating_el_nino_is_idempotent_and_appends_observations(self) -> None:
        run_search(_filters(), FixtureProvider(NINO_COLLISION), db_path=self.db, output_dir=self.out)
        run_search(_filters(), FixtureProvider(NINO_COLLISION), db_path=self.db, output_dir=self.out)

        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM prospect")[0]["n"], 2)
        counts = _rows(
            self.db,
            "SELECT p.name, COUNT(o.id) AS observations FROM prospect p "
            "JOIN observation o ON o.prospect_id = p.id GROUP BY p.name ORDER BY p.name",
        )
        self.assertEqual(
            [(row["name"], row["observations"]) for row in counts],
            [("El Nino", 2), ("El Niño", 2)],
        )

    def test_previous_case_cafe_pipe_norte_vs_cafe_remains_separated(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(DELIMITER_COLLISION),
            db_path=self.db,
            output_dir=self.out,
        )
        self.assertEqual(report.counts["prospects_matched"], 2)
        self.assertEqual(report.counts["observations"], 2)
        prospects = _rows(self.db, "SELECT name, address_public FROM prospect ORDER BY name")
        self.assertEqual(
            [(row["name"], row["address_public"]) for row in prospects],
            [("Cafe", "Norte|Calle 1"), ("Cafe|Norte", "Calle 1")],
        )

    def test_filters_preserve_accent_insensitivity(self) -> None:
        # Búsqueda y filtros usan `normalize_text` (sin diacríticos ni tildes).
        # Por tanto, buscar "Bogota" o "Bogotá" debe producir la misma normalización.
        self.assertEqual(normalize_text("Bogotá"), normalize_text("Bogota"))
        self.assertEqual(normalize_text("Restaurante"), normalize_text("restaurante"))
        self.assertEqual(normalize_text("Panadería"), normalize_text("panaderia"))
        self.assertEqual(normalize_text("El Niño"), normalize_text("el nino"))

        # Pero `normalize_identity` preserva la 'ñ' y formas canónicas:
        self.assertNotEqual(normalize_identity("El Niño"), normalize_identity("El Nino"))
        self.assertEqual(normalize_identity("El Niño"), "el niño")
        self.assertEqual(normalize_identity("El Nino"), "el nino")

    def test_normalization_rules_whitespace_casing_and_unicode_forms(self) -> None:
        # 1. Colapso de espacios múltiples y extremos
        self.assertEqual(normalize_identity("   El    Niño   "), "el niño")

        # 2. Casefolding (mayúsculas y minúsculas unificadas)
        self.assertEqual(normalize_identity("EL NIÑO"), "el niño")
        self.assertEqual(normalize_identity("El Niño"), "el niño")

        # 3. Formas canónicas Unicode: NFD (descompuesto) y NFC (precompuesto)
        nfd_nino = unicodedata.normalize("NFD", "El Niño")  # 'n' + '\u0303'
        nfc_nino = unicodedata.normalize("NFC", "El Niño")  # '\u00f1'
        self.assertEqual(normalize_identity(nfd_nino), normalize_identity(nfc_nino))
        self.assertEqual(normalize_identity(nfd_nino), "el niño")

        # 4. Diferenciación de palabras con 'ñ'
        self.assertNotEqual(normalize_identity("Peña"), normalize_identity("Pena"))
        self.assertNotEqual(normalize_identity("Campaña"), normalize_identity("Campana"))

    def test_compatibility_preserves_historical_keys_v1_v2_v3(self) -> None:
        with CaptacionWebStore(self.db):
            pass
        conn = sqlite3.connect(self.db)
        try:
            for index, key in enumerate((OLD_V1_KEY, OLD_V2_KEY, OLD_V3_KEY), start=1):
                conn.execute(
                    """
                    INSERT INTO prospect (
                        id, source, external_id, external_id_kind, name, category,
                        country, city, address_public, phone_public, website_url,
                        business_status, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 'LOCAL', ?, ?, ?, ?, ?, NULL, NULL, 'DESCONOCIDO', ?, ?)
                    """,
                    (
                        f"historical-{index}",
                        "fixture-nino-collision-v1",
                        key,
                        f"Negocio Historico {index}",
                        "Restaurante",
                        "Colombia",
                        "Bogota",
                        f"Calle Historica {index}",
                        OLD_SEEN_AT,
                        OLD_SEEN_AT,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        run_search(_filters(), FixtureProvider(NINO_COLLISION), db_path=self.db, output_dir=self.out)

        rows = _rows(
            self.db,
            "SELECT external_id, last_seen_at FROM prospect ORDER BY external_id",
        )
        # 3 historicos + 2 nuevos del fixture
        self.assertEqual(len(rows), 5)
        by_key = {row["external_id"]: row["last_seen_at"] for row in rows}
        self.assertEqual(by_key[OLD_V1_KEY], OLD_SEEN_AT)
        self.assertEqual(by_key[OLD_V2_KEY], OLD_SEEN_AT)
        self.assertEqual(by_key[OLD_V3_KEY], OLD_SEEN_AT)

        new_keys = [key for key in by_key if key not in (OLD_V1_KEY, OLD_V2_KEY, OLD_V3_KEY)]
        self.assertEqual(len(new_keys), 2)
        self.assertTrue(all(key.startswith("local:v4:") for key in new_keys))

    def test_different_countries_stay_separate(self) -> None:
        provider = FixtureProvider(COUNTRIES)
        run_search(
            _filters(country="Colombia", city="Ciudad Ficticia"),
            provider,
            db_path=self.db,
            output_dir=self.out,
        )
        run_search(
            _filters(country="Ecuador", city="Ciudad Ficticia"),
            provider,
            db_path=self.db,
            output_dir=self.out,
        )

        countries = [
            row["country"]
            for row in _rows(self.db, "SELECT country FROM prospect ORDER BY country")
        ]
        self.assertEqual(countries, ["Colombia", "Ecuador"])

    def test_insufficient_identity_stays_in_review_without_merging(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(HOMONYMS),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.counts["prospects_matched"], 4)
        self.assertEqual(report.counts["observations"], 4)
        prospects = _rows(self.db, "SELECT external_id_kind FROM prospect")
        self.assertEqual(len(prospects), 4)
        self.assertEqual({row["external_id_kind"] for row in prospects}, {EXTERNAL_ID_REVISION})


if __name__ == "__main__":
    unittest.main()

"""Regresion de la colision de identidad local por el delimitador `|`.

Solo usa fixtures sinteticos y SQLite en directorios temporales. No abre la
base operativa, `.env` ni servicios externos.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from prospector.captacion_web.cli import run_search
from prospector.captacion_web.models import (
    EXTERNAL_ID_LOCAL,
    EXTERNAL_ID_REVISION,
    LOCAL_IDENTITY_VERSION,
    SearchFilters,
    build_identity,
)
from prospector.captacion_web.providers import FixtureProvider
from prospector.captacion_web.storage import CaptacionWebStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COLLISION = FIXTURES / "identity_delimiter_collision.json"
COUNTRIES = FIXTURES / "country_identity.json"
HOMONYMS = FIXTURES / "identity_homonyms.json"

OLD_LOCAL_KEY = "local:etapa1ficticia"
OLD_V2_KEY = "local:v2:etapa1ficticia"
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


class TestDelimiterCollision(_TemporaryWorkspace):
    def test_direct_identity_is_unambiguous(self) -> None:
        first_kind, first_key = build_identity(
            "fixture-colision-delimitador-v1", None, "Cafe|Norte", "Calle 1", "Bogota", "Colombia"
        )
        second_kind, second_key = build_identity(
            "fixture-colision-delimitador-v1", None, "Cafe", "Norte|Calle 1", "Bogota", "Colombia"
        )

        self.assertEqual(first_kind, EXTERNAL_ID_LOCAL)
        self.assertEqual(second_kind, EXTERNAL_ID_LOCAL)
        self.assertNotEqual(first_key, second_key)

    def test_two_records_keep_two_prospects_and_two_observations(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(COLLISION),
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
        self.assertEqual(
            [(row["name"], row["address_public"]) for row in prospects],
            [("Cafe", "Norte|Calle 1"), ("Cafe|Norte", "Calle 1")],
        )

        observations = _rows(
            self.db,
            "SELECT p.name, o.rating, o.review_count FROM observation o "
            "JOIN prospect p ON p.id = o.prospect_id ORDER BY p.name",
        )
        self.assertEqual(
            [(row["name"], row["rating"], row["review_count"]) for row in observations],
            [("Cafe", 4.7, 50), ("Cafe|Norte", 4.6, 40)],
        )

    def test_repeating_sufficient_identity_is_idempotent(self) -> None:
        run_search(_filters(), FixtureProvider(COLLISION), db_path=self.db, output_dir=self.out)
        run_search(_filters(), FixtureProvider(COLLISION), db_path=self.db, output_dir=self.out)

        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM prospect")[0]["n"], 2)
        counts = _rows(
            self.db,
            "SELECT p.name, COUNT(o.id) AS observations FROM prospect p "
            "JOIN observation o ON o.prospect_id = p.id GROUP BY p.name ORDER BY p.name",
        )
        self.assertEqual(
            [(row["name"], row["observations"]) for row in counts],
            [("Cafe", 2), ("Cafe|Norte", 2)],
        )

    def test_old_local_keys_are_preserved_and_not_reused(self) -> None:
        with CaptacionWebStore(self.db):
            pass
        conn = sqlite3.connect(self.db)
        try:
            for index, key in enumerate((OLD_LOCAL_KEY, OLD_V2_KEY), start=1):
                conn.execute(
                    """
                    INSERT INTO prospect (
                        id, source, external_id, external_id_kind, name, category,
                        country, city, address_public, phone_public, website_url,
                        business_status, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, 'LOCAL', ?, ?, ?, ?, ?, NULL, NULL, 'DESCONOCIDO', ?, ?)
                    """,
                    (
                        f"legacy-{index}",
                        "fixture-colision-delimitador-v1",
                        key,
                        f"Sede Historica {index}",
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

        run_search(_filters(), FixtureProvider(COLLISION), db_path=self.db, output_dir=self.out)

        rows = _rows(
            self.db,
            "SELECT external_id, last_seen_at FROM prospect ORDER BY external_id",
        )
        self.assertEqual(len(rows), 4)
        by_key = {row["external_id"]: row["last_seen_at"] for row in rows}
        self.assertEqual(by_key[OLD_LOCAL_KEY], OLD_SEEN_AT)
        self.assertEqual(by_key[OLD_V2_KEY], OLD_SEEN_AT)
        new_keys = [key for key in by_key if key not in (OLD_LOCAL_KEY, OLD_V2_KEY)]
        self.assertEqual(len(new_keys), 2)
        self.assertTrue(all(key.startswith(f"local:{LOCAL_IDENTITY_VERSION}:") for key in new_keys))


class TestIdentityBoundaries(_TemporaryWorkspace):
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

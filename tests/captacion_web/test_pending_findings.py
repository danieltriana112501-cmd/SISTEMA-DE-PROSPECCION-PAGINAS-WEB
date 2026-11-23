"""Regresiones de los dos hallazgos pendientes de Captacion Web.

Solo usa fixtures sinteticos y SQLite en directorios temporales. No abre la
base operativa, `.env` ni servicios externos.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prospector.captacion_web.cli import run_search
from prospector.captacion_web.models import (
    RUN_COMPLETA,
    RUN_FALLIDA,
    RUN_PARCIAL,
    SearchFilters,
    normalize_text,
)
from prospector.captacion_web.providers import FixtureProvider
from prospector.captacion_web.storage import CaptacionWebStore

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
COUNTRY_IDENTITY = FIXTURES / "country_identity.json"
SENSITIVE_FAILURES = FIXTURES / "sensitive_failures.json"

SENSITIVE_MARKER = "tok_fallo_sintetico_987"
SAFE_FAILURE_CODE = "QUERY_FAILED"


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


def _dump_text(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        ]
        parts: list[str] = []
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                parts.extend(str(value) for value in row if value is not None)
        return "\n".join(parts)
    finally:
        conn.close()


def _run_cli(db: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "prospector.captacion_web",
            "--input",
            str(SENSITIVE_FAILURES),
            "--db",
            str(db),
            "--output-dir",
            str(out),
            "--country",
            "Colombia",
            "--city",
            "Bogota",
            "--category",
            "Restaurante",
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class _TemporaryWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "captacion_web.db"
        self.out = self.root / "runs"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestCountryInLocalIdentity(_TemporaryWorkspace):
    def test_same_name_address_city_in_different_countries_are_not_merged(self) -> None:
        provider = FixtureProvider(COUNTRY_IDENTITY)
        first = run_search(
            _filters(country="Colombia", city="Ciudad Ficticia"),
            provider,
            db_path=self.db,
            output_dir=self.out,
        )
        second = run_search(
            _filters(country="Ecuador", city="Ciudad Ficticia"),
            provider,
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(first.status, RUN_COMPLETA)
        self.assertEqual(second.status, RUN_COMPLETA)

        prospects = _rows(
            self.db,
            "SELECT id, country, external_id, external_id_kind FROM prospect ORDER BY country",
        )
        self.assertEqual(len(prospects), 2)
        self.assertEqual([p["country"] for p in prospects], ["Colombia", "Ecuador"])
        self.assertEqual({p["external_id_kind"] for p in prospects}, {"LOCAL"})
        self.assertEqual(len({p["external_id"] for p in prospects}), 2)
        self.assertEqual(len({p["id"] for p in prospects}), 2)

        observations = _rows(
            self.db,
            "SELECT p.country, o.rating FROM observation o "
            "JOIN prospect p ON p.id = o.prospect_id ORDER BY p.country",
        )
        self.assertEqual(
            [(row["country"], row["rating"]) for row in observations],
            [("Colombia", 4.6), ("Ecuador", 4.8)],
        )

    def test_repeating_a_sufficient_identity_stays_idempotent(self) -> None:
        provider = FixtureProvider(COUNTRY_IDENTITY)
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
        run_search(
            _filters(country="Colombia", city="Ciudad Ficticia"),
            provider,
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM prospect")[0]["n"], 2)
        counts = _rows(
            self.db,
            "SELECT p.country, COUNT(o.id) AS observations FROM prospect p "
            "JOIN observation o ON o.prospect_id = p.id GROUP BY p.country ORDER BY p.country",
        )
        self.assertEqual(
            [(row["country"], row["observations"]) for row in counts],
            [("Colombia", 2), ("Ecuador", 1)],
        )

    def test_stage1_local_identity_is_not_silently_merged(self) -> None:
        # Simula una fila guardada por la etapa 1: su clave local no incluia
        # pais. La nueva corrida no debe reutilizarla ni fusionar datos.
        with CaptacionWebStore(self.db):
            pass

        old_digest = hashlib.sha256(
            "|".join(
                [
                    normalize_text("fixture-pais-identidad-v1"),
                    normalize_text("Sede Frontera"),
                    normalize_text("Calle 1 #2-3"),
                    normalize_text("Ciudad Ficticia"),
                ]
            ).encode("utf-8")
        ).hexdigest()[:24]
        old_key = f"local:{old_digest}"
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 'LOCAL', ?, ?, ?, ?, ?, NULL, NULL, 'DESCONOCIDO', ?, ?)
                """,
                (
                    "stage1-prospect",
                    "fixture-pais-identidad-v1",
                    old_key,
                    "Sede Frontera",
                    "Restaurante",
                    "Colombia",
                    "Ciudad Ficticia",
                    "Calle 1 #2-3",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        report = run_search(
            _filters(country="Colombia", city="Ciudad Ficticia"),
            FixtureProvider(COUNTRY_IDENTITY),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_COMPLETA)
        rows = _rows(self.db, "SELECT external_id, last_seen_at FROM prospect ORDER BY external_id")
        self.assertEqual(len(rows), 2)
        self.assertIn(old_key, [row["external_id"] for row in rows])
        old_row = next(row for row in rows if row["external_id"] == old_key)
        self.assertEqual(old_row["last_seen_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(len([row for row in rows if row["external_id"] != old_key]), 1)


class TestSafeFailureErrors(_TemporaryWorkspace):
    def test_partial_failure_does_not_leak_fixture_value(self) -> None:
        report = run_search(
            _filters(keywords=("negocio",)),
            FixtureProvider(SENSITIVE_FAILURES),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_PARCIAL)
        self.assertEqual(report.counts["queries_failed"], 1)
        self.assertTrue(any(SAFE_FAILURE_CODE in error for error in report.errors))
        self.assertFalse(any(SENSITIVE_MARKER in error for error in report.errors))

        attempts = _rows(
            self.db,
            "SELECT status, error FROM query_attempt WHERE run_id = ? ORDER BY query",
            (report.run_id,),
        )
        self.assertEqual({attempt["status"] for attempt in attempts}, {"COMPLETA", "FALLIDA"})
        failed = [attempt for attempt in attempts if attempt["status"] == "FALLIDA"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], SAFE_FAILURE_CODE)

        self.assertNotIn(SENSITIVE_MARKER, _dump_text(self.db))
        self.assertNotIn(SENSITIVE_MARKER, report.report_path.read_text(encoding="utf-8"))

        result = _run_cli(self.root / "cli_partial.db", self.root / "cli_partial_runs", "--keywords", "negocio")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(SAFE_FAILURE_CODE, result.stdout)
        self.assertNotIn(SENSITIVE_MARKER, result.stdout + result.stderr)

    def test_total_failure_does_not_leak_fixture_value(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(SENSITIVE_FAILURES),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_FALLIDA)
        self.assertEqual(report.counts["queries_failed"], 1)
        self.assertTrue(any(SAFE_FAILURE_CODE in error for error in report.errors))
        self.assertFalse(any(SENSITIVE_MARKER in error for error in report.errors))

        attempts = _rows(
            self.db,
            "SELECT status, error FROM query_attempt WHERE run_id = ?",
            (report.run_id,),
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "FALLIDA")
        self.assertEqual(attempts[0]["error"], SAFE_FAILURE_CODE)

        self.assertNotIn(SENSITIVE_MARKER, _dump_text(self.db))
        self.assertNotIn(SENSITIVE_MARKER, report.report_path.read_text(encoding="utf-8"))

        result = _run_cli(self.root / "cli_failed.db", self.root / "cli_failed_runs")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(SAFE_FAILURE_CODE, result.stdout)
        self.assertNotIn(SENSITIVE_MARKER, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

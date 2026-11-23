"""Pruebas de la primera vertical de Captacion Web.

Todo se ejecuta con fixtures JSON sinteticos y bases SQLite en directorios
temporales. No se consulta Maps, redes, IA, `.env` ni `data/leads.db`.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prospector import config as legacy_config
from prospector.captacion_web import config as web_config
from prospector.captacion_web.cli import run_search
from prospector.captacion_web.models import (
    RUN_COMPLETA,
    RUN_FALLIDA,
    RUN_PARCIAL,
    SearchFilters,
)
from prospector.captacion_web.providers import FixtureProvider

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_businesses.json"
PARTIAL = FIXTURES / "partial_businesses.json"


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


class TestIsolation(unittest.TestCase):
    def test_web_paths_are_separate_from_legacy_paths(self) -> None:
        self.assertNotEqual(web_config.DEFAULT_DB_PATH, legacy_config.DB_PATH)
        self.assertEqual(web_config.DEFAULT_DB_PATH.parent.name, "captacion_web")
        self.assertNotEqual(web_config.DEFAULT_OUTPUT_DIR, legacy_config.DATA_DIR)
        self.assertEqual(web_config.DEFAULT_DB_PATH.name, "captacion_web.db")


class TestFiltersAndRunMetadata(_TemporaryWorkspace):
    def test_filters_are_applied_and_persisted(self) -> None:
        report = run_search(
            _filters(keywords=("sabor",), location="Chapinero"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_COMPLETA)
        self.assertEqual([p["name"] for p in report.prospects], ["Sabor Local Chapinero"])

        run = _rows(self.db, "SELECT * FROM search_run WHERE id = ?", (report.run_id,))[0]
        self.assertEqual(run["country"], "Colombia")
        self.assertEqual(run["city"], "Bogota")
        self.assertEqual(run["category"], "Restaurante")
        self.assertEqual(json.loads(run["keywords"]), ["sabor"])
        self.assertEqual(run["location"], "Chapinero")
        self.assertEqual(run["min_rating"], 4.5)
        self.assertEqual(run["min_reviews"], 30)
        self.assertEqual(run["status"], RUN_COMPLETA)

    def test_city_filter_selects_only_that_city(self) -> None:
        report = run_search(
            _filters(city="Medellin"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_COMPLETA)
        self.assertEqual([p["name"] for p in report.prospects], ["Paisa Gourmet"])

    def test_category_filter_is_exact(self) -> None:
        report = run_search(
            _filters(category="Panaderia"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual([p["name"] for p in report.prospects], ["Pan de la Esquina"])

    def test_keyword_and_location_filter_combine(self) -> None:
        report = run_search(
            _filters(keywords=("sabor",), location="Usaquen"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual([p["name"] for p in report.prospects], ["Sabor Local Usaquen"])


class TestReputationThresholds(_TemporaryWorkspace):
    def test_thresholds_are_inclusive_and_nulls_go_to_review(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        by_external = {
            row["external_id"]: row
            for row in _rows(
                self.db,
                "SELECT p.external_id, o.rating, o.review_count, o.reputation_state "
                "FROM observation o JOIN prospect p ON p.id = o.prospect_id",
            )
        }

        self.assertEqual(by_external["fx-001"]["reputation_state"], "CUMPLE")
        self.assertEqual(by_external["fx-003"]["reputation_state"], "NO_CUMPLE")
        self.assertEqual(by_external["fx-004"]["reputation_state"], "REVISION")
        self.assertEqual(by_external["fx-001"]["rating"], 4.5)
        self.assertEqual(by_external["fx-001"]["review_count"], 30)

    def test_thresholds_are_configurable(self) -> None:
        report = run_search(
            _filters(min_rating=4.8, min_reviews=100),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        by_external = {
            row["external_id"]: row
            for row in _rows(
                self.db,
                "SELECT p.external_id, o.reputation_state FROM observation o "
                "JOIN prospect p ON p.id = o.prospect_id",
            )
        }
        self.assertEqual(by_external["fx-001"]["reputation_state"], "NO_CUMPLE")
        self.assertEqual(by_external["fx-002"]["reputation_state"], "CUMPLE")
        self.assertEqual(report.status, RUN_COMPLETA)


class TestProvenanceAndIdempotency(_TemporaryWorkspace):
    def test_empty_website_is_not_treated_as_no_website(self) -> None:
        run_search(
            _filters(),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        rows = _rows(
            self.db,
            "SELECT p.external_id, o.website_url, o.website_state FROM observation o "
            "JOIN prospect p ON p.id = o.prospect_id "
            "WHERE p.external_id IN ('fx-001', 'fx-002') ORDER BY p.external_id",
        )
        self.assertIsNone(rows[0]["website_url"])
        self.assertEqual(rows[0]["website_state"], "PENDIENTE_VERIFICACION")
        self.assertEqual(rows[1]["website_url"], "https://saborlocal.example")
        self.assertEqual(rows[1]["website_state"], "PENDIENTE_VERIFICACION")

    def test_missing_business_status_is_unknown_not_invented(self) -> None:
        run_search(
            _filters(),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        rows = _rows(
            self.db,
            "SELECT o.business_status FROM observation o "
            "JOIN prospect p ON p.id = o.prospect_id WHERE p.external_id = 'fx-003'",
        )
        self.assertEqual(rows[0]["business_status"], "DESCONOCIDO")

    def test_repeating_the_same_input_does_not_duplicate_or_delete(self) -> None:
        provider = FixtureProvider(SYNTHETIC)
        first = run_search(_filters(), provider, db_path=self.db, output_dir=self.out)
        second = run_search(_filters(), provider, db_path=self.db, output_dir=self.out)

        self.assertNotEqual(first.run_id, second.run_id)
        prospects = _rows(
            self.db,
            "SELECT id FROM prospect WHERE external_id = 'fx-001'",
        )
        self.assertEqual(len(prospects), 1)

        observations = _rows(
            self.db,
            "SELECT rating, observed_at FROM observation WHERE prospect_id = ? ORDER BY rowid",
            (prospects[0]["id"],),
        )
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["rating"], 4.5)
        self.assertEqual(observations[1]["rating"], 4.5)

    def test_branches_sharing_phone_and_domain_are_not_merged(self) -> None:
        run_search(
            _filters(keywords=("compartida",)),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        branches = _rows(
            self.db,
            "SELECT id, name, phone_public, website_url, external_id_kind "
            "FROM prospect WHERE name LIKE 'Sede Compartida%' ORDER BY name",
        )
        self.assertEqual(len(branches), 2)
        self.assertEqual(branches[0]["phone_public"], branches[1]["phone_public"])
        self.assertEqual(branches[0]["website_url"], branches[1]["website_url"])
        self.assertEqual({b["external_id_kind"] for b in branches}, {"LOCAL"})
        self.assertNotEqual(branches[0]["id"], branches[1]["id"])


class TestRunStates(_TemporaryWorkspace):
    def test_complete_empty_result_is_not_a_failure(self) -> None:
        report = run_search(
            _filters(city="Cali"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_COMPLETA)
        self.assertEqual(report.counts["prospects_matched"], 0)
        self.assertEqual(_rows(self.db, "SELECT * FROM prospect"), [])

    def test_partial_query_is_visible_and_keeps_valid_observations(self) -> None:
        report = run_search(
            _filters(keywords=("sabor",)),
            FixtureProvider(PARTIAL),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_PARCIAL)
        self.assertGreaterEqual(report.counts["records_invalid"], 1)
        self.assertTrue(any("TIMEOUT" in e for e in report.errors))

        attempts = _rows(
            self.db,
            "SELECT status, error FROM query_attempt WHERE run_id = ? ORDER BY query",
            (report.run_id,),
        )
        self.assertEqual({a["status"] for a in attempts}, {"PARCIAL", "FALLIDA"})
        self.assertTrue(any("TIMEOUT" in (a["error"] or "") for a in attempts))

        valid = _rows(
            self.db,
            "SELECT p.external_id FROM observation o JOIN prospect p ON p.id = o.prospect_id",
        )
        self.assertEqual([v["external_id"] for v in valid], ["fx-p01"])

    def test_missing_input_is_a_failure_not_an_empty_search(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(self.root / "no-existe.json"),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_FALLIDA)
        self.assertTrue(report.errors)
        self.assertEqual(report.counts["prospects_matched"], 0)
        run = _rows(self.db, "SELECT status, error FROM search_run WHERE id = ?", (report.run_id,))[0]
        self.assertEqual(run["status"], RUN_FALLIDA)
        self.assertTrue(run["error"])
        self.assertTrue(report.report_path.exists())

    def test_unexpected_provider_error_is_a_failure(self) -> None:
        class _ExplodingProvider:
            source = "proveedor-roto"

            def search(self, filters):  # noqa: ANN001 - doble de prueba
                raise RuntimeError("boom")

        report = run_search(_filters(), _ExplodingProvider(), db_path=self.db, output_dir=self.out)

        self.assertEqual(report.status, RUN_FALLIDA)
        self.assertTrue(any("RuntimeError: boom" in error for error in report.errors))
        run = _rows(self.db, "SELECT status FROM search_run WHERE id = ?", (report.run_id,))[0]
        self.assertEqual(run["status"], RUN_FALLIDA)


class TestReportFile(_TemporaryWorkspace):
    def test_report_preserves_filters_status_and_queries(self) -> None:
        report = run_search(
            _filters(keywords=("sabor",)),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        data = json.loads(report.report_path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "1")
        self.assertEqual(data["run_id"], report.run_id)
        self.assertEqual(data["status"], RUN_COMPLETA)
        self.assertEqual(data["filters"]["country"], "Colombia")
        self.assertEqual(data["filters"]["keywords"], ["sabor"])
        self.assertEqual(data["counts"]["prospects_matched"], 2)
        self.assertEqual(len(data["queries"]), 2)
        self.assertTrue(all(q["status"] == "COMPLETA" for q in data["queries"]))


class TestCli(_TemporaryWorkspace):
    def _run_cli(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "prospector.captacion_web",
                "--input",
                str(SYNTHETIC),
                "--db",
                str(self.db),
                "--output-dir",
                str(self.out),
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

    def test_help_documents_the_new_entry(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "prospector.captacion_web", "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--input", "--db", "--output-dir", "--country", "--city", "--category", "--keywords", "--location", "--min-rating", "--min-reviews"):
            self.assertIn(flag, result.stdout)

    def test_cli_exit_zero_on_complete(self) -> None:
        result = self._run_cli()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("estado: COMPLETA", result.stdout)
        self.assertIn("reporte:", result.stdout)

    def test_cli_exit_three_on_partial(self) -> None:
        result = self._run_cli("--input", str(PARTIAL), "--keywords", "sabor")

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("estado: PARCIAL", result.stdout)
        self.assertIn("TIMEOUT", result.stdout)

    def test_cli_exit_one_on_failure(self) -> None:
        result = self._run_cli("--input", str(self.root / "no-existe.json"))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("estado: FALLIDA", result.stdout)
        self.assertNotIn("estado: COMPLETA", result.stdout)


if __name__ == "__main__":
    unittest.main()

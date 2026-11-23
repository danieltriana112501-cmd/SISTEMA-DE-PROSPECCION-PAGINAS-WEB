"""Pruebas de regresion de los cuatro hallazgos de la revision.

Todo usa fixtures sinteticos y SQLite en directorios temporales. No se abre
`data/leads.db`, no se lee `.env` y no se consulta ningun servicio externo.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prospector.captacion_web import storage as web_storage
from prospector.captacion_web.cli import run_search
from prospector.captacion_web.models import (
    PERSISTED_FIELDS,
    RUN_COMPLETA,
    RUN_PARCIAL,
    DatabaseRejectedError,
    SearchFilters,
)
from prospector.captacion_web.providers import FixtureProvider

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_businesses.json"
HOMONYMS = FIXTURES / "identity_homonyms.json"
INVALID = FIXTURES / "invalid_records.json"
ONLY_INVALID = FIXTURES / "only_invalid.json"
SENSITIVE = FIXTURES / "sensitive_extras.json"

SENSITIVE_MARKERS = (
    "sup3r-secreto-123",
    "tok_sintetico_456",
    "persona.ficticia@example.test",
    "Fulano Sintetico",
    "no persistir",
)

_LEGACY_SCHEMA = """
CREATE TABLE leads (
    id TEXT PRIMARY KEY,
    empresa TEXT NOT NULL,
    sitio_web TEXT,
    telefono_principal TEXT,
    calidad_contacto TEXT,
    data JSON NOT NULL
);
CREATE TABLE zonas_completadas (
    zona TEXT PRIMARY KEY,
    leads INTEGER NOT NULL,
    completado_en TEXT NOT NULL
);
CREATE TABLE raw_maps_cache (
    query TEXT PRIMARY KEY,
    filas JSON NOT NULL,
    scrapeado_en TEXT NOT NULL
);
CREATE TABLE corridas (
    id TEXT PRIMARY KEY,
    zona TEXT NOT NULL,
    inicio TEXT NOT NULL,
    fin TEXT,
    estado TEXT NOT NULL,
    latido TEXT,
    detectados INTEGER NOT NULL DEFAULT 0,
    nuevos INTEGER NOT NULL DEFAULT 0,
    duplicados INTEGER NOT NULL DEFAULT 0,
    con_email INTEGER NOT NULL DEFAULT 0,
    con_celular INTEGER NOT NULL DEFAULT 0,
    decisores INTEGER NOT NULL DEFAULT 0,
    sin_contacto INTEGER NOT NULL DEFAULT 0,
    ia_ok INTEGER NOT NULL DEFAULT 0,
    ia_fallidos INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
INSERT INTO leads (id, empresa, data) VALUES ('legacy-1', 'Empresa Legacy', '{}');
"""


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


def _table_names(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        }
    finally:
        conn.close()


def _dump_text(db: Path) -> str:
    """Serializa el contenido de todas las tablas para buscar filtraciones."""
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


def _make_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.commit()
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


class TestProtectedLegacyDatabase(_TemporaryWorkspace):
    def test_synthetic_legacy_database_is_rejected_and_untouched(self) -> None:
        legacy = self.root / "legacy.db"
        _make_legacy_db(legacy)
        before_tables = _table_names(legacy)
        before_lead = _rows(legacy, "SELECT id, empresa FROM leads")

        with self.assertRaises(DatabaseRejectedError) as caught:
            run_search(_filters(), FixtureProvider(SYNTHETIC), db_path=legacy, output_dir=self.out)

        self.assertIn("rechazada", str(caught.exception).lower())
        self.assertEqual(_table_names(legacy), before_tables)
        self.assertEqual(_rows(legacy, "SELECT id, empresa FROM leads"), before_lead)
        self.assertFalse(before_tables & {"search_run", "query_attempt", "prospect", "observation"})
        self.assertFalse(self.out.exists())

    def test_legacy_path_alias_is_rejected_before_any_read_or_write(self) -> None:
        # Archivo vacio: sin la comparacion de rutas canonicas se aceptaria como
        # base nueva. El alias con `..` debe resolverse y rechazarse igual.
        legacy = self.root / "legacy.db"
        legacy.write_bytes(b"")
        alias_dir = self.root / "sub"
        alias_dir.mkdir()
        alias = alias_dir / ".." / "legacy.db"

        with mock.patch.object(web_storage, "LEGACY_DB_PATH", legacy):
            with self.assertRaises(DatabaseRejectedError):
                run_search(_filters(), FixtureProvider(SYNTHETIC), db_path=alias, output_dir=self.out)

        self.assertEqual(legacy.stat().st_size, 0)
        self.assertEqual(_table_names(legacy), set())

    def test_foreign_database_is_rejected(self) -> None:
        foreign = self.root / "otra.db"
        conn = sqlite3.connect(foreign)
        try:
            conn.execute("CREATE TABLE otra_cosa (id TEXT)")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(DatabaseRejectedError):
            run_search(_filters(), FixtureProvider(SYNTHETIC), db_path=foreign, output_dir=self.out)

    def test_empty_database_file_is_not_assumed_legacy(self) -> None:
        self.db.write_bytes(b"")

        report = run_search(_filters(), FixtureProvider(SYNTHETIC), db_path=self.db, output_dir=self.out)

        self.assertEqual(report.status, RUN_COMPLETA)
        self.assertIn("search_run", _table_names(self.db))

    def test_cli_rejects_legacy_database_with_exit_four(self) -> None:
        legacy = self.root / "legacy.db"
        _make_legacy_db(legacy)
        before_tables = _table_names(legacy)
        before_lead = _rows(legacy, "SELECT id, empresa FROM leads")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "prospector.captacion_web",
                "--input",
                str(SYNTHETIC),
                "--db",
                str(legacy),
                "--output-dir",
                str(self.out),
                "--country",
                "Colombia",
                "--city",
                "Bogota",
                "--category",
                "Restaurante",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("rechazada", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(_table_names(legacy), before_tables)
        self.assertEqual(_rows(legacy, "SELECT id, empresa FROM leads"), before_lead)


class TestInsufficientIdentity(_TemporaryWorkspace):
    def test_homonyms_without_address_are_not_merged(self) -> None:
        report = run_search(_filters(), FixtureProvider(HOMONYMS), db_path=self.db, output_dir=self.out)

        self.assertEqual(report.counts["prospects_matched"], 4)
        prospects = _rows(
            self.db,
            "SELECT name, external_id, external_id_kind FROM prospect ORDER BY name, external_id",
        )
        self.assertEqual(len(prospects), 4)
        self.assertEqual({p["external_id_kind"] for p in prospects}, {"REVISION"})
        self.assertEqual(len({p["external_id"] for p in prospects}), 4)
        self.assertEqual(
            _rows(self.db, "SELECT COUNT(*) AS n FROM observation")[0]["n"],
            4,
        )

    def test_exact_duplicates_without_address_are_both_kept(self) -> None:
        run_search(_filters(), FixtureProvider(HOMONYMS), db_path=self.db, output_dir=self.out)

        duplicates = _rows(
            self.db,
            "SELECT external_id FROM prospect WHERE name = 'Duplicado Exacto' ORDER BY external_id",
        )
        self.assertEqual(len(duplicates), 2)
        self.assertNotEqual(duplicates[0]["external_id"], duplicates[1]["external_id"])

    def test_repeating_insufficient_identity_stays_stable_and_append_only(self) -> None:
        provider = FixtureProvider(HOMONYMS)
        run_search(_filters(), provider, db_path=self.db, output_dir=self.out)
        run_search(_filters(), provider, db_path=self.db, output_dir=self.out)

        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM prospect")[0]["n"], 4)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM observation")[0]["n"], 8)

    def test_sufficient_identity_is_still_idempotent(self) -> None:
        provider = FixtureProvider(SYNTHETIC)
        run_search(_filters(), provider, db_path=self.db, output_dir=self.out)
        run_search(_filters(), provider, db_path=self.db, output_dir=self.out)

        rows = _rows(
            self.db,
            "SELECT p.id, p.external_id_kind, COUNT(o.id) AS observations "
            "FROM prospect p JOIN observation o ON o.prospect_id = p.id "
            "WHERE p.external_id = 'fx-001' GROUP BY p.id",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id_kind"], "PROVEEDOR")
        self.assertEqual(rows[0]["observations"], 2)


class TestValidationBeforeFiltering(_TemporaryWorkspace):
    def test_missing_city_is_invalid_not_silently_filtered(self) -> None:
        report = run_search(_filters(), FixtureProvider(INVALID), db_path=self.db, output_dir=self.out)

        self.assertEqual(report.status, RUN_PARCIAL)
        self.assertGreaterEqual(report.counts["records_invalid"], 2)
        self.assertTrue(any("city" in error for error in report.errors))
        self.assertEqual([p["name"] for p in report.prospects], ["Valido"])

    def test_wrong_types_are_reported_as_invalid(self) -> None:
        report = run_search(_filters(), FixtureProvider(INVALID), db_path=self.db, output_dir=self.out)

        self.assertTrue(any("rating" in error for error in report.errors))
        attempts = _rows(
            self.db,
            "SELECT status, records_invalid, error FROM query_attempt WHERE run_id = ?",
            (report.run_id,),
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "PARCIAL")
        self.assertGreaterEqual(attempts[0]["records_invalid"], 2)
        self.assertTrue(attempts[0]["error"])

    def test_only_invalid_records_is_partial_not_complete_empty(self) -> None:
        report = run_search(
            _filters(),
            FixtureProvider(ONLY_INVALID),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_PARCIAL)
        self.assertEqual(report.counts["prospects_matched"], 0)
        self.assertGreaterEqual(report.counts["records_invalid"], 1)
        self.assertTrue(report.errors)

    def test_genuinely_complete_empty_search_stays_complete(self) -> None:
        report = run_search(
            _filters(city="Cali"),
            FixtureProvider(SYNTHETIC),
            db_path=self.db,
            output_dir=self.out,
        )

        self.assertEqual(report.status, RUN_COMPLETA)
        self.assertEqual(report.counts["prospects_matched"], 0)
        self.assertEqual(report.counts["records_invalid"], 0)


class TestMinimalPersistence(_TemporaryWorkspace):
    def test_extra_fields_are_not_persisted_in_database_or_report(self) -> None:
        report = run_search(_filters(), FixtureProvider(SENSITIVE), db_path=self.db, output_dir=self.out)

        database_text = _dump_text(self.db)
        report_text = report.report_path.read_text(encoding="utf-8")
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, database_text)
            self.assertNotIn(marker, report_text)

    def test_observation_json_uses_only_the_explicit_allowlist(self) -> None:
        run_search(_filters(), FixtureProvider(SENSITIVE), db_path=self.db, output_dir=self.out)

        row = _rows(self.db, "SELECT raw_json FROM observation")[0]
        keys = set(json.loads(row["raw_json"]))
        self.assertEqual(keys, set(PERSISTED_FIELDS))
        self.assertTrue({"name", "rating", "review_count", "address"} <= keys)

    def test_cli_output_does_not_leak_extra_fields(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "prospector.captacion_web",
                "--input",
                str(SENSITIVE),
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
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        combined = result.stdout + result.stderr
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, combined)


if __name__ == "__main__":
    unittest.main()

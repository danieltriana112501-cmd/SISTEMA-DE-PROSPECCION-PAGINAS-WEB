"""Persistencia SQLite propia de Captacion Web.

No usa `data/leads.db` ni comparte tablas con el modo SG-SST. Solo lee
`prospector.config.DB_PATH` para rechazar esa ruta antes de escribir. Cada
ejecucion conserva filtros, intentos, errores y observaciones; repetir la
misma entrada agrega una observacion nueva sin borrar las anteriores.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

from prospector import config as legacy_config

from .models import (
    DatabaseRejectedError,
    ObservedBusiness,
    SearchFilters,
    utc_now_iso,
)

OWN_TABLES = frozenset(
    {
        "search_run",
        "query_attempt",
        "prospect",
        "observation",
        "website_review",
        "social_candidate",
        "ranking_snapshot",
        "decision_maker_review",
    }
)
LEGACY_TABLES = frozenset({"leads", "zonas_completadas", "raw_maps_cache", "corridas"})
LEGACY_DB_PATH = Path(legacy_config.DB_PATH)


def _canonical(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(os.fspath(path)))


def _same_existing_file(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _existing_tables(path: Path) -> set[str]:
    uri = _canonical(path).as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DatabaseRejectedError(f"base rechazada: no se pudo abrir '{path}' en solo lectura: {exc}") from exc
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseRejectedError(
            f"base rechazada: '{path}' no es una base SQLite valida y no se modifico"
        ) from exc
    finally:
        conn.close()
    return {str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")}


def validate_db_target(db_path: Path) -> Path:
    """Rechaza la base SG-SST y bases ajenas antes de crear o escribir nada.

    Una base inexistente o un archivo vacio se consideran propios. Una base que
    ya contiene tablas del sistema SG-SST, o tablas que no pertenecen a esta
    vertical, se rechaza sin abrirla en modo escritura.
    """
    path = Path(db_path).expanduser()
    canonical = _canonical(path)
    if _same_existing_file(path, LEGACY_DB_PATH) or os.path.normcase(str(canonical)) == os.path.normcase(
        str(_canonical(LEGACY_DB_PATH))
    ):
        raise DatabaseRejectedError(
            "base rechazada: la ruta corresponde a la base operativa del sistema SG-SST. "
            "Use una base propia de Captacion Web."
        )

    if not path.exists():
        return canonical
    if path.is_dir():
        raise DatabaseRejectedError(f"base rechazada: '{path}' es un directorio, no una base SQLite")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DatabaseRejectedError(f"base rechazada: no se pudo inspeccionar '{path}': {exc}") from exc
    if size == 0:
        return canonical

    tables = _existing_tables(path)
    if not tables:
        return canonical

    legacy_hit = tables & LEGACY_TABLES
    if legacy_hit:
        raise DatabaseRejectedError(
            "base rechazada: contiene tablas del sistema SG-SST "
            f"({', '.join(sorted(legacy_hit))}) y no se modifico. "
            "Use una base propia de Captacion Web."
        )
    foreign = tables - OWN_TABLES
    if foreign:
        raise DatabaseRejectedError(
            "base rechazada: contiene tablas ajenas a Captacion Web "
            f"({', '.join(sorted(foreign))}) y no se modifico. "
            "Use una base nueva o una base propia de Captacion Web."
        )
    return canonical

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_run (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    country TEXT NOT NULL,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    keywords TEXT NOT NULL,
    location TEXT NOT NULL,
    min_rating REAL NOT NULL,
    min_reviews INTEGER NOT NULL,
    queries_total INTEGER NOT NULL DEFAULT 0,
    queries_failed INTEGER NOT NULL DEFAULT 0,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_invalid INTEGER NOT NULL DEFAULT 0,
    records_filtered_out INTEGER NOT NULL DEFAULT 0,
    prospects_matched INTEGER NOT NULL DEFAULT 0,
    prospects_new INTEGER NOT NULL DEFAULT 0,
    prospects_eligible INTEGER NOT NULL DEFAULT 0,
    observations INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS query_attempt (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES search_run(id),
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_invalid INTEGER NOT NULL DEFAULT 0,
    records_filtered_out INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prospect (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    external_id_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    country TEXT,
    city TEXT,
    address_public TEXT,
    phone_public TEXT,
    website_url TEXT,
    business_status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS observation (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospect(id),
    run_id TEXT NOT NULL REFERENCES search_run(id),
    query TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    rating REAL,
    review_count INTEGER,
    business_status TEXT NOT NULL,
    website_url TEXT,
    website_state TEXT NOT NULL,
    phone_public TEXT,
    address_public TEXT,
    reputation_state TEXT NOT NULL,
    -- A pesar del nombre historico, solo contiene PERSISTED_FIELDS.
    raw_json TEXT NOT NULL,
    UNIQUE(prospect_id, run_id)
);

CREATE TABLE IF NOT EXISTS website_review (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospect(id),
    status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    official_url TEXT,
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    queries_json TEXT NOT NULL DEFAULT '[]',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_candidate (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospect(id),
    platform TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    signals TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ranking_snapshot (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospect(id),
    score REAL,
    eligible INTEGER NOT NULL DEFAULT 0,
    ready_for_contact INTEGER NOT NULL DEFAULT 0,
    rule_version TEXT NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    missing_data_json TEXT NOT NULL DEFAULT '[]',
    calculated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_query_attempt_run ON query_attempt(run_id);
CREATE INDEX IF NOT EXISTS idx_observation_prospect ON observation(prospect_id);
CREATE INDEX IF NOT EXISTS idx_search_run_started ON search_run(started_at);
CREATE INDEX IF NOT EXISTS idx_website_review_prospect ON website_review(prospect_id);
CREATE INDEX IF NOT EXISTS idx_social_candidate_prospect ON social_candidate(prospect_id);
CREATE INDEX IF NOT EXISTS idx_ranking_snapshot_prospect ON ranking_snapshot(prospect_id);
"""


class CaptacionWebStore:
    def __init__(self, db_path: Path, *, read_only: bool = False, existing_only: bool = False) -> None:
        self.db_path = validate_db_target(db_path)
        if read_only or existing_only:
            mode = "ro" if read_only else "rw"
            self.conn = sqlite3.connect(self.db_path.as_uri() + f"?mode={mode}", uri=True)
            self.conn.row_factory = sqlite3.Row
            if not read_only:
                self.conn.execute("PRAGMA foreign_keys = ON")
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CaptacionWebStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start_run(self, filters: SearchFilters, source: str) -> str:
        run_id = uuid.uuid4().hex
        self.conn.execute(
            """
            INSERT INTO search_run (
                id, started_at, status, source, country, city, category, keywords,
                location, min_rating, min_reviews
            ) VALUES (?, ?, 'EN_CURSO', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                utc_now_iso(),
                source,
                filters.country,
                filters.city,
                filters.category,
                json.dumps(list(filters.keywords), ensure_ascii=False),
                filters.location,
                filters.min_rating,
                filters.min_reviews,
            ),
        )
        self.conn.commit()
        return run_id

    def add_attempt(
        self,
        run_id: str,
        query: str,
        status: str,
        records_seen: int,
        records_invalid: int,
        records_filtered_out: int,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO query_attempt (
                id, run_id, query, status, records_seen, records_invalid,
                records_filtered_out, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                run_id,
                query,
                status,
                records_seen,
                records_invalid,
                records_filtered_out,
                error,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def upsert_prospect(self, record: ObservedBusiness, seen_at: str) -> tuple[str, bool]:
        prospect_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"captacion-web:{record.source}:{record.external_id}",
            )
        )
        existing = self.conn.execute(
            "SELECT id FROM prospect WHERE source = ? AND external_id = ?",
            (record.source, record.external_id),
        ).fetchone()

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prospect_id,
                    record.source,
                    record.external_id,
                    record.external_id_kind,
                    record.name,
                    record.category,
                    record.country,
                    record.city,
                    record.address,
                    record.phone,
                    record.website_url,
                    record.business_status,
                    seen_at,
                    seen_at,
                ),
            )
            self.conn.commit()
            return prospect_id, True

        prospect_id = existing["id"]
        self.conn.execute(
            """
            UPDATE prospect SET
                name = ?, category = ?, country = ?, city = ?, address_public = ?,
                phone_public = ?, website_url = ?, business_status = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                record.name,
                record.category,
                record.country,
                record.city,
                record.address,
                record.phone,
                record.website_url,
                record.business_status,
                seen_at,
                prospect_id,
            ),
        )
        self.conn.commit()
        return prospect_id, False

    def add_observation(
        self,
        prospect_id: str,
        run_id: str,
        query: str,
        record: ObservedBusiness,
        reputation: str,
        website_state: str,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO observation (
                id, prospect_id, run_id, query, source, observed_at, rating,
                review_count, business_status, website_url, website_state,
                phone_public, address_public, reputation_state, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prospect_id, run_id) DO NOTHING
            """,
            (
                uuid.uuid4().hex,
                prospect_id,
                run_id,
                query,
                record.source,
                record.observed_at,
                record.rating,
                record.review_count,
                record.business_status,
                record.website_url,
                website_state,
                record.phone,
                record.address,
                reputation,
                json.dumps(record.persisted, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def finish_run(self, run_id: str, status: str, counts: dict[str, int], error: str = "") -> None:
        self.conn.execute(
            """
            UPDATE search_run SET
                finished_at = ?, status = ?, queries_total = ?, queries_failed = ?,
                records_seen = ?, records_invalid = ?, records_filtered_out = ?,
                prospects_matched = ?, prospects_new = ?, prospects_eligible = ?,
                observations = ?, error = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                status,
                counts.get("queries_total", 0),
                counts.get("queries_failed", 0),
                counts.get("records_seen", 0),
                counts.get("records_invalid", 0),
                counts.get("records_filtered_out", 0),
                counts.get("prospects_matched", 0),
                counts.get("prospects_new", 0),
                counts.get("prospects_eligible", 0),
                counts.get("observations", 0),
                error,
                run_id,
            ),
        )
        self.conn.commit()

    def mark_report_error(self, run_id: str, message: str) -> None:
        self.conn.execute(
            "UPDATE search_run SET status = 'PARCIAL', error = ?, finished_at = ? "
            "WHERE id = ? AND status = 'COMPLETA'",
            (message, utc_now_iso(), run_id),
        )
        self.conn.commit()

    def add_website_review(self, prospect_id: str, review: Any, *, commit: bool = True) -> str:
        review_id = uuid.uuid4().hex
        status = review.status.value if hasattr(review.status, "value") else str(review.status)
        confidence = review.confidence.value if hasattr(review.confidence, "value") else str(review.confidence)

        queries_data = []
        for q in getattr(review, "queries", []):
            q_res = q.resultado.value if hasattr(q.resultado, "value") else str(q.resultado)
            queries_data.append(
                {
                    "consulta": q.consulta,
                    "resultado": q_res,
                    "evidence_ids": list(q.evidence_ids),
                    "error": getattr(q, "error", ""),
                }
            )

        candidates_data = []
        for c in getattr(review, "candidates", []):
            c_tipo = c.tipo.value if hasattr(c.tipo, "value") else str(c.tipo)
            c_id = c.identidad.value if hasattr(c.identidad, "value") else str(c.identidad)
            candidates_data.append(
                {
                    "url": c.url,
                    "tipo": c_tipo,
                    "identidad": c_id,
                    "evidence_ids": list(c.evidence_ids),
                    "access_error": getattr(c, "access_error", ""),
                }
            )

        evidence_ids = list(getattr(review, "evidence_ids", []))
        reasons = list(getattr(review, "reasons", []))
        now = utc_now_iso()

        self.conn.execute(
            """
            INSERT INTO website_review (
                id, prospect_id, status, confidence, official_url, reviewer,
                reviewed_at, notes, evidence_json, queries_json, candidates_json,
                reasons_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                prospect_id,
                status,
                confidence,
                review.official_url,
                review.reviewer,
                review.reviewed_at or now,
                review.notes or "",
                json.dumps(evidence_ids, ensure_ascii=False),
                json.dumps(queries_data, ensure_ascii=False),
                json.dumps(candidates_data, ensure_ascii=False),
                json.dumps(reasons, ensure_ascii=False),
                now,
            ),
        )
        if commit:
            self.conn.commit()
        return review_id

    def get_website_reviews(self, prospect_id: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT * FROM website_review WHERE prospect_id = ? ORDER BY reviewed_at ASC, created_at ASC, rowid ASC",
            (prospect_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_latest_website_review(self, prospect_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            "SELECT * FROM website_review WHERE prospect_id = ? ORDER BY reviewed_at DESC, created_at DESC, rowid DESC LIMIT 1",
            (prospect_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def add_social_candidate(self, prospect_id: str, candidate: Any, *, commit: bool = True) -> str:
        cand_id = uuid.uuid4().hex
        platform = candidate.platform.value if hasattr(candidate.platform, "value") else str(candidate.platform)
        status = candidate.status.value if hasattr(candidate.status, "value") else str(candidate.status)
        confidence = (
            candidate.confidence.value if hasattr(candidate.confidence, "value") else str(candidate.confidence)
        )
        now = utc_now_iso()

        self.conn.execute(
            """
            INSERT INTO social_candidate (
                id, prospect_id, platform, url, status, confidence, signals,
                reviewer, reviewed_at, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cand_id,
                prospect_id,
                platform,
                candidate.url,
                status,
                confidence,
                candidate.signals or "",
                candidate.reviewer or "OPERADOR",
                candidate.reviewed_at or now,
                json.dumps(list(candidate.evidence_ids), ensure_ascii=False),
                now,
            ),
        )
        if commit:
            self.conn.commit()
        return cand_id

    def get_social_candidates(self, prospect_id: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT * FROM social_candidate WHERE prospect_id = ? ORDER BY created_at ASC, rowid ASC",
            (prospect_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_latest_social_candidates(self, prospect_id: str) -> list[dict[str, Any]]:
        candidates = self.get_social_candidates(prospect_id)
        vigente: dict[tuple[str, str], dict[str, Any]] = {}
        for c in candidates:
            plat = c["platform"]
            clean_url = (c.get("url") or "").strip().lower().rstrip("/")
            key = (plat, clean_url or plat)
            vigente[key] = c
        return list(vigente.values())

    def get_prospect(self, prospect_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute("SELECT * FROM prospect WHERE id = ?", (prospect_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def find_prospect(self, identifier: str) -> dict[str, Any] | None:
        """Resolve an internal ID first; never select an ambiguous external ID."""
        prospect = self.get_prospect(identifier)
        if prospect is not None:
            return prospect
        matches = self.conn.execute(
            "SELECT * FROM prospect WHERE external_id = ? LIMIT 2", (identifier,)
        ).fetchall()
        return dict(matches[0]) if len(matches) == 1 else None

    def add_decision_maker_review(
        self, prospect_id: str, *, status: str, name: str | None, role: str | None,
        source_url: str | None, reviewer: str, reviewed_at: str,
    ) -> None:
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS decision_maker_review (
                    id INTEGER PRIMARY KEY,
                    prospect_id TEXT NOT NULL REFERENCES prospect(id),
                    status TEXT NOT NULL, name TEXT, role TEXT, source_url TEXT,
                    reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                """INSERT INTO decision_maker_review
                   (prospect_id, status, name, role, source_url, reviewer, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (prospect_id, status, name, role, source_url, reviewer, reviewed_at),
            )

    def get_decision_maker_reviews(self, prospect_id: str) -> list[dict[str, Any]]:
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decision_maker_review'"
        ).fetchone():
            return []
        rows = self.conn.execute(
            "SELECT * FROM decision_maker_review WHERE prospect_id = ? ORDER BY reviewed_at DESC, id DESC",
            (prospect_id,),
        )
        return [dict(row) for row in rows]

    def add_ranking_snapshot(self, prospect_id: str, ranking: Any) -> str:
        snapshot_id = uuid.uuid4().hex
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO ranking_snapshot (
                id, prospect_id, score, eligible, ready_for_contact, rule_version,
                breakdown_json, reasons_json, missing_data_json, calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                prospect_id,
                ranking.score,
                1 if ranking.eligible else 0,
                1 if ranking.ready_for_contact else 0,
                ranking.rule_version,
                json.dumps(ranking.breakdown, ensure_ascii=False),
                json.dumps(ranking.reasons, ensure_ascii=False),
                json.dumps(ranking.missing_data, ensure_ascii=False),
                now,
            ),
        )
        self.conn.commit()
        return snapshot_id

    def get_latest_ranking_snapshot(self, prospect_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            "SELECT * FROM ranking_snapshot WHERE prospect_id = ? ORDER BY calculated_at DESC, rowid DESC LIMIT 1",
            (prospect_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

"""CLI y orquestacion de la primera vertical de Captacion Web."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config
from .models import (
    ATTEMPT_COMPLETA,
    ATTEMPT_FALLIDA,
    EXTERNAL_ID_REVISION,
    REPUTATION_CUMPLE,
    RUN_COMPLETA,
    RUN_FALLIDA,
    RUN_PARCIAL,
    WEBSITE_PENDIENTE,
    DatabaseRejectedError,
    ObservedBusiness,
    ProviderError,
    QueryOutcome,
    RunReport,
    SearchFilters,
    reputation_state,
    utc_now_iso,
)
from .providers import FixtureProvider
from .ranking import RankingConfig, calculate_commercial_ranking
from .storage import CaptacionWebStore
from .verification import (
    ConfidenceLevel,
    QueryOutcomeStatus,
    SocialCandidate,
    SocialPlatform,
    SocialVerificationStatus,
    WebQuery,
    WebsiteReview,
    WebsiteStatus,
    is_valid_web_url,
    classify_url_domain,
    evaluate_website_review,
    IdentityMatch,
    SOCIAL_DOMAINS,
    WebCandidate,
    WebCandidateType,
)

EXIT_CODES = {RUN_COMPLETA: 0, RUN_FALLIDA: 1, RUN_PARCIAL: 3}
EXIT_DB_REJECTED = 4


def _parse_keywords(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m prospector.captacion_web",
        description=(
            "Captacion Web en modo aislado. Solo acepta un JSON local sintetico o "
            "una importacion autorizada; no consulta Maps, redes ni servicios externos."
        ),
        epilog=(
            "Codigos de salida: 0 = COMPLETA, 3 = PARCIAL, 1 = FALLIDA, "
            "4 = base rechazada por seguridad, 2 = error de uso. "
            "La base y los reportes se guardan fuera de data/leads.db."
        ),
    )
    parser.add_argument("--input", required=True, type=Path, help="JSON local con 'records'")
    parser.add_argument(
        "--db",
        type=Path,
        default=config.DEFAULT_DB_PATH,
        help=f"SQLite propia de Captacion Web (por defecto: {config.DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=config.DEFAULT_OUTPUT_DIR,
        help=f"Directorio de reportes JSON (por defecto: {config.DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--source", help="Sobrescribe el nombre de fuente declarado en el JSON")
    parser.add_argument("--country", required=True, help="Pais a filtrar")
    parser.add_argument("--city", required=True, help="Ciudad a filtrar")
    parser.add_argument("--category", required=True, help="Categoria exacta a filtrar")
    parser.add_argument("--keywords", default="", help="Palabras clave separadas por coma")
    parser.add_argument("--location", default="", help="Texto que debe aparecer en la direccion")
    parser.add_argument("--min-rating", type=float, default=4.5, help="Calificacion minima inclusiva")
    parser.add_argument("--min-reviews", type=int, default=30, help="Resenas minimas inclusivas")
    parser.add_argument(
        "--rank",
        action="store_true",
        help="Calcula y persiste snapshot de ranking comercial (Etapa 2)",
    )
    return parser


def run_search(
    filters: SearchFilters,
    provider: FixtureProvider,
    db_path: Path,
    output_dir: Path,
    with_ranking: bool = False,
) -> RunReport:
    """Ejecuta una corrida y conserva su estado aunque la fuente falle."""
    started_at = utc_now_iso()
    store = CaptacionWebStore(db_path)
    try:
        errors: list[str] = []
        try:
            source = provider.source
        except ProviderError:
            source = "no-disponible"
        except Exception as exc:  # una fuente local rota no debe tumbar la corrida
            source = "no-disponible"
            errors.append(f"{type(exc).__name__}: {exc}")

        run_id = store.start_run(filters, source)
        outcomes: list[QueryOutcome] = []

        try:
            outcome = provider.search(filters)
            source = outcome.source
            outcomes = outcome.outcomes
        except ProviderError as exc:
            errors.append(str(exc))
        except Exception as exc:  # se registra como FALLIDA, nunca como exito vacio
            errors.append(f"{type(exc).__name__}: {exc}")

        for attempt in outcomes:
            store.add_attempt(
                run_id=run_id,
                query=attempt.query,
                status=attempt.status,
                records_seen=attempt.records_seen,
                records_invalid=attempt.records_invalid,
                records_filtered_out=attempt.records_filtered_out,
                error=attempt.error,
            )
            if attempt.error:
                errors.append(f"{attempt.query}: {attempt.error}")

        unique: dict[str, tuple[ObservedBusiness, str]] = {}
        for attempt in outcomes:
            for record in attempt.records:
                unique.setdefault(record.identity_key, (record, attempt.query))

        prospects: list[dict[str, Any]] = []
        new_count = 0
        eligible = 0
        observations = 0
        for record, query in unique.values():
            prospect_id, is_new = store.upsert_prospect(record, seen_at=started_at)
            reputation = reputation_state(
                record.rating,
                record.review_count,
                filters.min_rating,
                filters.min_reviews,
            )
            if store.add_observation(
                prospect_id=prospect_id,
                run_id=run_id,
                query=query,
                record=record,
                reputation=reputation,
                website_state=WEBSITE_PENDIENTE,
            ):
                observations += 1
            if is_new:
                new_count += 1
            if reputation == REPUTATION_CUMPLE:
                eligible += 1
            ranking_info = None
            if with_ranking:
                latest_review_dict = store.get_latest_website_review(prospect_id)
                latest_review = None
                if latest_review_dict:
                    latest_review = WebsiteReview(
                        status=WebsiteStatus(latest_review_dict["status"]),
                        confidence=ConfidenceLevel(latest_review_dict["confidence"]),
                        official_url=latest_review_dict.get("official_url"),
                        reviewer=latest_review_dict.get("reviewer", ""),
                        reviewed_at=latest_review_dict.get("reviewed_at", ""),
                    )
                social_candidates_dicts = store.get_social_candidates(prospect_id)
                social_candidates = [
                    SocialCandidate(
                        platform=SocialPlatform(s["platform"]),
                        url=s["url"],
                        status=SocialVerificationStatus(s["status"]),
                        confidence=ConfidenceLevel(s["confidence"]),
                        signals=s.get("signals", ""),
                        reviewed_at=s.get("reviewed_at", ""),
                        reviewer=s.get("reviewer", ""),
                    )
                    for s in social_candidates_dicts
                ]
                prospect_dict = dict(record.persisted)
                prospect_dict["external_id_kind"] = record.external_id_kind

                ranking_cfg = RankingConfig(
                    min_rating=filters.min_rating,
                    min_reviews=filters.min_reviews,
                )
                ranking_res = calculate_commercial_ranking(
                    prospect_data=prospect_dict,
                    website_review=latest_review,
                    social_profiles=social_candidates,
                    config=ranking_cfg,
                )
                store.add_ranking_snapshot(prospect_id, ranking_res)
                ranking_info = {
                    "score": ranking_res.score,
                    "eligible": ranking_res.eligible,
                    "ready_for_contact": ranking_res.ready_for_contact,
                    "preliminary_commercial_fit": getattr(ranking_res, "preliminary_commercial_fit", False),
                    "rule_version": ranking_res.rule_version,
                    "breakdown": ranking_res.breakdown,
                }

            prospect_entry = {
                "id": prospect_id,
                "external_id": record.external_id,
                "external_id_kind": record.external_id_kind,
                "identity_state": (
                    "REVISION" if record.external_id_kind == EXTERNAL_ID_REVISION else "CONFIRMADA"
                ),
                "name": record.name,
                "rating": record.rating,
                "review_count": record.review_count,
                "reputation_state": reputation,
                "website_state": WEBSITE_PENDIENTE,
            }
            if ranking_info:
                prospect_entry["ranking"] = ranking_info
            prospects.append(prospect_entry)

        counts = {
            "queries_total": len(outcomes),
            "queries_failed": sum(1 for attempt in outcomes if attempt.status == ATTEMPT_FALLIDA),
            "records_seen": sum(attempt.records_seen for attempt in outcomes),
            "records_invalid": sum(attempt.records_invalid for attempt in outcomes),
            "records_filtered_out": sum(attempt.records_filtered_out for attempt in outcomes),
            "prospects_matched": len(unique),
            "prospects_new": new_count,
            "prospects_eligible": eligible,
            "observations": observations,
        }
        status = _run_status(outcomes)
        if status == RUN_FALLIDA and not errors:
            errors.append("la fuente no devolvio consultas ejecutables")

        store.finish_run(run_id, status, counts, error="; ".join(errors))
        report_data = _report_data(
            run_id=run_id,
            status=status,
            source=source,
            filters=filters,
            started_at=started_at,
            counts=counts,
            errors=errors,
            outcomes=outcomes,
            prospects=prospects,
        )

        report_path: Path | None = None
        try:
            report_path = _write_report(output_dir, run_id, report_data)
        except OSError as exc:
            message = f"REPORT_WRITE_FAILED: {exc}"
            errors.append(message)
            if status != RUN_FALLIDA:
                status = RUN_PARCIAL
            store.mark_report_error(run_id, message)
            report_data["status"] = status
            report_data["errors"] = errors

        return RunReport(
            run_id=run_id,
            status=status,
            source=source,
            filters=filters,
            counts=counts,
            errors=errors,
            queries=[
                {
                    "query": attempt.query,
                    "status": attempt.status,
                    "records_seen": attempt.records_seen,
                    "records_invalid": attempt.records_invalid,
                    "records_filtered_out": attempt.records_filtered_out,
                    "error": attempt.error,
                }
                for attempt in outcomes
            ],
            prospects=prospects,
            report_path=report_path,
        )
    finally:
        store.close()


def _run_status(outcomes: list[QueryOutcome]) -> str:
    if not outcomes:
        return RUN_FALLIDA
    if all(attempt.status == ATTEMPT_FALLIDA for attempt in outcomes):
        return RUN_FALLIDA
    if any(attempt.status != ATTEMPT_COMPLETA for attempt in outcomes):
        return RUN_PARCIAL
    return RUN_COMPLETA


def _report_data(
    *,
    run_id: str,
    status: str,
    source: str,
    filters: SearchFilters,
    started_at: str,
    counts: dict[str, int],
    errors: list[str],
    outcomes: list[QueryOutcome],
    prospects: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "source": source,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "filters": filters.as_dict(),
        "counts": counts,
        "errors": errors,
        "queries": [
            {
                "query": attempt.query,
                "status": attempt.status,
                "records_seen": attempt.records_seen,
                "records_invalid": attempt.records_invalid,
                "records_filtered_out": attempt.records_filtered_out,
                "error": attempt.error,
            }
            for attempt in outcomes
        ],
        "prospects": prospects,
    }


def _write_report(output_dir: Path, run_id: str, data: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_id}.json"
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _print_report(report: RunReport) -> None:
    counts = report.counts
    print(f"Captacion Web · corrida {report.run_id}")
    print(f"estado: {report.status}")
    print(f"fuente: {report.source}")
    print(f"consultas: {counts['queries_total']} ({counts['queries_failed']} fallidas)")
    print(
        f"prospectos: {counts['prospects_matched']} "
        f"(nuevos {counts['prospects_new']}, elegibles por reputacion {counts['prospects_eligible']})"
    )
    print(f"observaciones: {counts['observations']}")
    revision = sum(1 for prospect in report.prospects if prospect.get("identity_state") == "REVISION")
    if revision:
        print(f"identidad en revision: {revision}")
    ranked_count = sum(1 for prospect in report.prospects if "ranking" in prospect)
    if ranked_count:
        favorable = sum(1 for p in report.prospects if p.get("ranking", {}).get("preliminary_commercial_fit"))
        ready = sum(1 for p in report.prospects if p.get("ranking", {}).get("ready_for_contact"))
        print(f"ranking: snapshots generados {ranked_count}, aptos preliminares {favorable}, listos para contacto {ready}")
    for error in report.errors:
        print(f"error: {error}")
    if report.report_path is not None:
        print(f"reporte: {report.report_path}")


SUBCOMMANDS = {"review-web", "review-social", "show-reviews", "import-reviews"}


def build_review_web_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m prospector.captacion_web review-web",
        description="Registra una revisión manual de sitio web para un prospecto (RF-03).",
    )
    p.add_argument("--db", type=Path, default=config.DEFAULT_DB_PATH, help="Ruta de la base SQLite")
    p.add_argument("--prospect-id", required=True, help="ID interno del prospecto (o ID externo no ambiguo)")
    p.add_argument(
        "--status",
        required=True,
        choices=[s.value for s in WebsiteStatus],
        help="Estado verificado del sitio web",
    )
    p.add_argument(
        "--confidence",
        required=True,
        choices=["alta", "media", "baja"],
        help="Nivel de confianza de la revisión",
    )
    p.add_argument("--reviewer", required=True, help="Identificador del revisor u operador")
    p.add_argument("--official-url", default=None, help="URL oficial si fue encontrada o inaccesible")
    p.add_argument("--evidence-ids", default="", help="IDs de evidencia separados por coma")
    p.add_argument("--notes", default="", help="Notas u observaciones de la revisión")
    p.add_argument("--reasons", default="", help="Motivos o justificaciones separadas por coma")
    p.add_argument("--query", default="", help="Texto de consulta de búsqueda efectuada")
    p.add_argument("--query-status", choices=[v.value for v in QueryOutcomeStatus], default="COMPLETA")
    p.add_argument("--aggregator-url", help="URL del agregador corroborado")
    p.add_argument("--access-error", default="", help="Error observado en un sitio oficial inaccesible")
    return p



def _review_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Se esperaba texto.")
    return value.strip()


def _review_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Se esperaba una lista de textos.")
    return [text for entry in value if (text := _review_text(entry))]


def _reviewed_at(data: dict[str, Any]) -> str:
    if "reviewed_at" not in data:
        return utc_now_iso()
    timestamp = datetime.fromisoformat(_review_text(data["reviewed_at"]))
    if timestamp.utcoffset() is None:
        raise ValueError("La fecha de revisión requiere zona horaria.")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")


_CORROBORATIONS = ("ENLACE_OFICIAL", "NOMBRE_SEDE_TELEFONO", "NOMBRE_SEDE_DIRECCION")


def _social_review(data: dict[str, Any]) -> SocialCandidate:
    platform = SocialPlatform(data["platform"])
    status = SocialVerificationStatus(data["status"])
    url = _review_text(data["url"])
    reviewer = _review_text(data.get("reviewer", ""))
    refs = _review_list(data.get("evidence_ids", []))
    signals = _review_text(data.get("signals", ""))
    corroboration = _review_text(data.get("corroboration", ""))
    if not reviewer or not is_valid_web_url(url):
        raise ValueError("Se exige revisor y URL HTTP(S) válida.")
    if status == SocialVerificationStatus.CONFIRMADO:
        hostname = (urlparse(url).hostname or "").lower()
        matches_platform = any(
            platform == expected and (hostname == domain or hostname.endswith("." + domain))
            for domain, expected in SOCIAL_DOMAINS.items()
        )
        if not refs or not signals or corroboration not in _CORROBORATIONS or (
            platform != SocialPlatform.OTRA and not matches_platform
        ):
            raise ValueError("Para confirmar, indica plataforma concordante, corroboración, señales y evidencia.")
        signals = f"[{corroboration}] {signals}"
    return SocialCandidate(
        platform, url, status, ConfidenceLevel(data.get("confidence", "media")),
        signals=signals, evidence_ids=refs, reviewer=reviewer, reviewed_at=_reviewed_at(data),
    )


def _website_review(data: dict[str, Any], social_profiles: list[dict[str, Any]]) -> WebsiteReview:
    status = WebsiteStatus(data["status"])
    reviewer = _review_text(data.get("reviewer", ""))
    refs = _review_list(data.get("evidence_ids", []))
    query = _review_text(data.get("query", ""))
    official_url = _review_text(data.get("official_url") or "")
    aggregator_url = _review_text(data.get("aggregator_url") or "")
    reasons = _review_list(data.get("reasons", []))
    if not reviewer:
        raise ValueError("Se exige revisor.")
    queries = [WebQuery(query, QueryOutcomeStatus(data.get("query_status", "COMPLETA")), refs)] if query else []
    candidates = []
    if official_url:
        if not refs or not is_valid_web_url(official_url) or classify_url_domain(official_url)[0] != WebCandidateType.OFICIAL:
            raise ValueError("Se exige URL oficial HTTP(S) y evidencia.")
        access_error = _review_text(data.get("access_error", ""))
        if (status == WebsiteStatus.OFICIAL_NO_DISPONIBLE) != bool(access_error):
            raise ValueError("La indisponibilidad oficial requiere indicar el error de acceso.")
        candidates.append(WebCandidate(official_url, WebCandidateType.OFICIAL, IdentityMatch.COINCIDE, refs, access_error))
    if aggregator_url:
        if not refs or not is_valid_web_url(aggregator_url) or classify_url_domain(aggregator_url)[0] != WebCandidateType.AGREGADOR:
            raise ValueError("Se exige una URL HTTP(S) de un agregador reconocido y evidencia.")
        candidates.append(WebCandidate(aggregator_url, WebCandidateType.AGREGADOR, IdentityMatch.COINCIDE, refs))
    confirmed = [
        SocialCandidate(SocialPlatform(s["platform"]), s["url"], SocialVerificationStatus.CONFIRMADO,
                        ConfidenceLevel(s["confidence"]))
        for s in social_profiles
        if s["status"] == SocialVerificationStatus.CONFIRMADO.value
        and any(s["signals"].startswith(f"[{kind}] ") for kind in _CORROBORATIONS)
        and is_valid_web_url(s["url"])
        and json.loads(s["evidence_json"])
    ]
    if status != WebsiteStatus.PENDIENTE_VERIFICACION:
        evaluated, _, _, explanation = evaluate_website_review(queries, candidates, confirmed)
        if evaluated != status:
            raise ValueError(f"La evidencia no respalda el estado solicitado; resultado: {evaluated.value}.")
        reasons.extend(explanation)
    elif official_url:
        raise ValueError("Una URL oficial corroborada requiere registrar su estado de acceso.")
    return WebsiteReview(
        status, ConfidenceLevel(data.get("confidence", "media")), official_url or None,
        reviewer, _reviewed_at(data), queries=queries, candidates=candidates, evidence_ids=refs,
        notes=_review_text(data.get("notes", "")), reasons=reasons,
    )


def _command_data(parsed: argparse.Namespace) -> dict[str, Any]:
    data = vars(parsed).copy()
    for field in ("evidence_ids", "reasons"):
        if field in data:
            data[field] = data[field].split(",")
    return data


def _save_manual_review(parsed: argparse.Namespace, kind: str) -> int:
    try:
        with CaptacionWebStore(parsed.db) as store:
            prospect = store.find_prospect(parsed.prospect_id)
            if prospect is None:
                raise ValueError("Prospecto no encontrado o ambiguo; indica el ID interno.")
            pid = prospect["id"]
            if kind == "web":
                review = _website_review(_command_data(parsed), store.get_latest_social_candidates(pid))
                review_id = store.add_website_review(pid, review)
                print(f"Revisión web registrada: id={review_id}, prospecto={pid}, status={review.status.value}")
            else:
                candidate = _social_review(_command_data(parsed))
                review_id = store.add_social_candidate(pid, candidate)
                print(f"Candidato social registrado: id={review_id}, prospecto={pid}, "
                      f"platform={candidate.platform.value}, status={candidate.status.value}")
    except DatabaseRejectedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_REJECTED
    except (ValueError, KeyError, TypeError, OverflowError, sqlite3.Error):
        print("ERROR: Revisión inválida o prospecto no encontrado o ambiguo. "
              "Comprueba ID interno, URL y evidencia de la revisión.", file=sys.stderr)
        return 1
    return 0


def handle_review_web(args: list[str]) -> int:
    return _save_manual_review(build_review_web_parser().parse_args(args), "web")


def build_review_social_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m prospector.captacion_web review-social",
        description="Registra un candidato de red social para un prospecto (RF-04).",
    )
    p.add_argument("--db", type=Path, default=config.DEFAULT_DB_PATH, help="Ruta de la base SQLite")
    p.add_argument("--prospect-id", required=True, help="ID interno del prospecto (o ID externo no ambiguo)")
    p.add_argument(
        "--platform",
        required=True,
        choices=[pl.value for pl in SocialPlatform],
        help="Plataforma de red social",
    )
    p.add_argument("--url", required=True, help="URL del perfil en la plataforma")
    p.add_argument(
        "--status",
        required=True,
        choices=[st.value for st in SocialVerificationStatus],
        help="Estado de verificación del perfil",
    )
    p.add_argument(
        "--confidence",
        required=True,
        choices=["alta", "media", "baja"],
        help="Nivel de confianza de la verificación",
    )
    p.add_argument("--reviewer", required=True, help="Identificador del revisor u operador")
    p.add_argument("--signals", default="", help="Señales de identidad observadas")
    p.add_argument('--corroboration', choices=_CORROBORATIONS, default='', help='Tipo de corroboración manual; exige referencias y señales para confirmar')
    p.add_argument("--evidence-ids", default="", help="IDs de evidencia separados por coma")
    return p


def handle_review_social(args: list[str]) -> int:
    return _save_manual_review(build_review_social_parser().parse_args(args), 'social')


def build_show_reviews_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m prospector.captacion_web show-reviews",
        description="Consulta las revisiones web y candidatos sociales de un prospecto.",
    )
    p.add_argument("--db", type=Path, default=config.DEFAULT_DB_PATH, help="Ruta de la base SQLite")
    p.add_argument("--prospect-id", required=True, help="ID interno del prospecto (o ID externo no ambiguo)")
    p.add_argument("--json", action="store_true", help="Salida en formato JSON estructurado")
    return p


def handle_show_reviews(args: list[str]) -> int:
    parser = build_show_reviews_parser()
    parsed = parser.parse_args(args)

    try:
        store = CaptacionWebStore(parsed.db)
    except DatabaseRejectedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_REJECTED

    try:
        prospect = store.find_prospect(parsed.prospect_id)
        if prospect is None:
            print(f"ERROR: Prospecto '{parsed.prospect_id}' no encontrado o ambiguo; indica el ID interno.", file=sys.stderr)
            return 1

        pid = prospect["id"]
        web_reviews = store.get_website_reviews(pid)
        latest_web = store.get_latest_website_review(pid)
        socials = store.get_social_candidates(pid)
        latest_socials = store.get_latest_social_candidates(pid)
        latest_rank = store.get_latest_ranking_snapshot(pid)

        if parsed.json:
            data = {
                "prospect": prospect,
                "website_reviews": web_reviews,
                "latest_website_review": latest_web,
                "social_candidates": socials,
                "latest_social_candidates": latest_socials,
                "latest_ranking_snapshot": latest_rank,
            }
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

        print(f"Prospecto: {prospect['name']} (ID: {pid})")
        print(f"Ubicación: {prospect.get('city')}, {prospect.get('country')} | Dirección: {prospect.get('address_public')}")
        print(f"Identidad: {prospect['external_id_kind']} ({prospect['external_id']})")
        print(f"Revisiones Web ({len(web_reviews)}):")
        if not web_reviews:
            print("  (sin revisiones registradas)")
        for r in web_reviews:
            print(f"  - [{r['reviewed_at']}] {r['status']} ({r['confidence']}) por {r['reviewer']}: url={r.get('official_url')}")
            if r.get("notes"):
                print(f"    notas: {r['notes']}")
        print(f"Candidatos Sociales ({len(socials)}, vigentes: {len(latest_socials)}):")
        if not socials:
            print("  (sin candidatos registrados)")
        for s in socials:
            print(f"  - [{s['reviewed_at']}] {s['platform']}: {s['url']} [{s['status']}] ({s['confidence']}) por {s['reviewer']}")
            if s.get("signals"):
                print(f"    señales: {s['signals']}")
        if latest_rank:
            print(f"Último Ranking:")
            print(f"  score: {latest_rank.get('score')} | eligible: {bool(latest_rank.get('eligible'))} | ready_for_contact: {bool(latest_rank.get('ready_for_contact'))}")
        return 0
    finally:
        store.close()


def handle_import_reviews(args: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="python -m prospector.captacion_web import-reviews",
        description="Importa un lote estructurado de revisiones manuales en formato JSON.",
    )
    p.add_argument("--db", type=Path, default=config.DEFAULT_DB_PATH, help="Ruta de la base SQLite")
    p.add_argument("--input", required=True, type=Path, help="Archivo JSON local con revisiones")
    parsed = p.parse_args(args)

    if not parsed.input.exists():
        print(f"ERROR: Archivo no encontrado: {parsed.input}", file=sys.stderr)
        return 1

    try:
        data = json.loads(parsed.input.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        items = []
        for field, default_kind in (("website_reviews", "web"), ("social_candidates", "social"), ("reviews", None)):
            group = data.get(field, [])
            if not isinstance(group, list):
                raise ValueError
            for item in group:
                if not isinstance(item, dict):
                    raise ValueError
                kind = default_kind or item.get("type")
                if kind not in ("web", "social"):
                    raise ValueError
                items.append((kind, item))
        if not items:
            raise ValueError
    except (OSError, ValueError):
        print("ERROR: JSON de revisiones inválido o vacío.", file=sys.stderr)
        return 1

    index = 1
    try:
        with CaptacionWebStore(parsed.db) as store, store.conn:
            web_count = social_count = 0
            for index, (kind, item) in sorted(enumerate(items, start=1), key=lambda pair: pair[1][0] == "web"):
                prospect = store.find_prospect(_review_text(item["prospect_id"]))
                if prospect is None:
                    raise ValueError
                pid = prospect["id"]
                if kind == "web":
                    review = _website_review(item, store.get_latest_social_candidates(pid))
                    store.add_website_review(pid, review, commit=False)
                    web_count += 1
                else:
                    candidate = _social_review(item)
                    store.add_social_candidate(pid, candidate, commit=False)
                    social_count += 1
        print(f"Importación completada: {web_count} revisiones web, {social_count} candidatos sociales.")
        return 0
    except DatabaseRejectedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_REJECTED
    except (ValueError, KeyError, TypeError, OverflowError, sqlite3.Error):
        print(f"ERROR: Lote rechazado en revisión {index}; no se guardaron revisiones.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] in SUBCOMMANDS:
        cmd = argv[0]
        cmd_args = argv[1:]
        if cmd == "review-web":
            return handle_review_web(cmd_args)
        elif cmd == "review-social":
            return handle_review_social(cmd_args)
        elif cmd == "show-reviews":
            return handle_show_reviews(cmd_args)
        elif cmd == "import-reviews":
            return handle_import_reviews(cmd_args)

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        filters = SearchFilters(
            country=args.country,
            city=args.city,
            category=args.category,
            keywords=_parse_keywords(args.keywords),
            location=args.location,
            min_rating=args.min_rating,
            min_reviews=args.min_reviews,
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        report = run_search(
            filters,
            FixtureProvider(args.input, source=args.source),
            db_path=args.db,
            output_dir=args.output_dir,
            with_ranking=args.rank,
        )
    except DatabaseRejectedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_DB_REJECTED
    _print_report(report)
    return EXIT_CODES[report.status]

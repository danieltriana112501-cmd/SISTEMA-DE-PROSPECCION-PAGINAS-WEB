"""Pruebas de regresión para las correcciones de la Etapa 2 de Captación Web.

Cubre:
1. Identidad en revisión y preparación para contacto (ready_for_contact permanece False).
2. Evidencia suficiente para clasificar la web (consultas vacías / sin evidencia no producen NO_ENCONTRADA; URL inválida).
3. Corroboración estricta de candidatos de redes sociales (no confirmar solo por nombre+ciudad o teléfono con nombre contradictorio).
4. Umbrales efectivos de filtros y estado social vigente en el ranking.
5. Flujo operativo de revisión manual vía CLI (review-web, review-social, show-reviews).
6. Compatibilidad de base de datos SQLite sintética de Etapa 1 y aperturas repetidas.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from prospector.captacion_web import cli
from prospector.captacion_web.models import (
    EXTERNAL_ID_LOCAL,
    EXTERNAL_ID_REVISION,
    ObservedBusiness,
    SearchFilters,
)
from prospector.captacion_web.providers import FixtureProvider
from prospector.captacion_web.ranking import (
    RankingConfig,
    RankingResult,
    calculate_commercial_ranking,
)
from prospector.captacion_web.storage import CaptacionWebStore
from prospector.captacion_web.verification import (
    ConfidenceLevel,
    IdentityMatch,
    QueryOutcomeStatus,
    SocialCandidate,
    SocialPlatform,
    SocialVerificationStatus,
    WebCandidate,
    WebCandidateType,
    WebQuery,
    WebsiteReview,
    WebsiteStatus,
    evaluate_social_candidate,
    evaluate_website_review,
)


class TestStage2Corrections(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "corrections_test.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -------------------------------------------------------------------------
    # 1. Identidad en revisión y preparación para contacto
    # -------------------------------------------------------------------------
    def test_insufficient_identity_is_never_eligible_and_ready_for_contact_is_always_false_in_stage2(self) -> None:
        # A. Prospecto con identidad en REVISIÓN
        prospect_revision = {
            "name": "Local Sin Dirección",
            "category": "Restaurante",
            "country": "Colombia",
            "city": "Bogota",
            "address": None,
            "phone": "+573001112233",
            "rating": 4.9,
            "review_count": 100,
            "business_status": "ACTIVO",
            "external_id_kind": EXTERNAL_ID_REVISION,
        }
        review_favorable = WebsiteReview(
            status=WebsiteStatus.SOLO_REDES_SOCIALES,
            confidence=ConfidenceLevel.ALTA,
            official_url=None,
            reviewer="OP1",
            reviewed_at="2026-11-21T20:00:00+00:00",
            queries=[WebQuery("test", QueryOutcomeStatus.COMPLETA, ["E1"])],
        )

        res_rev = calculate_commercial_ranking(prospect_revision, review_favorable, [])
        self.assertFalse(res_rev.eligible, "Identidad en REVISIÓN no debe ser elegible")
        self.assertFalse(res_rev.ready_for_contact, "Identidad en REVISIÓN no debe estar lista para contacto")
        self.assertTrue(any("revisión" in r.lower() for r in res_rev.reasons))

        # B. Prospecto plenamente confirmado y elegible
        prospect_ok = {
            "name": "Panadería El Rosario",
            "category": "Panadería",
            "country": "Colombia",
            "city": "Bogota",
            "address": "Carrera 7 #45-10",
            "phone": "+573001112233",
            "rating": 4.8,
            "review_count": 80,
            "business_status": "ACTIVO",
            "external_id_kind": EXTERNAL_ID_LOCAL,
        }
        res_ok = calculate_commercial_ranking(prospect_ok, review_favorable, [])
        self.assertTrue(res_ok.eligible, "Prospecto con identidad confirmada y web de oportunidad debe ser elegible")
        # En Etapa 2, ready_for_contact DEBE SER FALSE porque no hay decisor, canal verificado ni aprobación humana
        self.assertFalse(
            res_ok.ready_for_contact,
            "ready_for_contact debe ser estrictamente False en Etapa 2",
        )
        # Se expone ajuste comercial preliminar favorable sin implicar autorización de contacto
        self.assertTrue(
            getattr(res_ok, "preliminary_commercial_fit", False),
            "Debe indicar preliminary_commercial_fit como True cuando es comercialmente viable",
        )

    def test_cli_passes_external_id_kind_to_ranking(self) -> None:
        fixture_data = {
            "source": "test-src",
            "records": [
                {
                    "name": "Cafe Sin Direccion",
                    "category": "Cafeteria",
                    "country": "Colombia",
                    "city": "Bogota",
                    "address": None,
                    "phone": "+573009998877",
                    "rating": 4.8,
                    "review_count": 50,
                    "business_status": "ACTIVO",
                }
            ],
        }
        fix_path = self.root / "fixture_revision.json"
        fix_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        filters = SearchFilters("Colombia", "Bogota", "Cafeteria")
        # Primera corrida: registra el prospecto
        report1 = cli.run_search(
            filters=filters,
            provider=FixtureProvider(fix_path),
            db_path=self.db_path,
            output_dir=self.root / "reports",
            with_ranking=False,
        )
        pid = report1.prospects[0]["id"]

        # Agregamos una revisión web altamente favorable a ese prospecto
        with CaptacionWebStore(self.db_path) as store:
            store.add_website_review(
                pid,
                WebsiteReview(
                    status=WebsiteStatus.SOLO_REDES_SOCIALES,
                    confidence=ConfidenceLevel.ALTA,
                    official_url=None,
                    reviewer="OP_FAVORABLE",
                    reviewed_at="2026-11-21T11:00:00+00:00",
                ),
            )

        # Segunda corrida con ranking:
        # Si cli.py NO pasa external_id_kind a calculate_commercial_ranking,
        # como la web es favorable y la reputación cumple, ¡eligible saldría True por error!
        report2 = cli.run_search(
            filters=filters,
            provider=FixtureProvider(fix_path),
            db_path=self.db_path,
            output_dir=self.root / "reports",
            with_ranking=True,
        )

        self.assertEqual(len(report2.prospects), 1)
        p = report2.prospects[0]
        self.assertEqual(p["external_id_kind"], EXTERNAL_ID_REVISION)
        self.assertEqual(p["identity_state"], "REVISION")
        self.assertIn("ranking", p)
        # El ranking debe haber recibido external_id_kind y marcado eligible=False
        self.assertFalse(
            p["ranking"]["eligible"],
            "El ranking en CLI no debe declarar elegible a un prospecto en REVISIÓN aunque su web sea favorable",
        )
        self.assertFalse(p["ranking"]["ready_for_contact"])

    # -------------------------------------------------------------------------
    # 2. Evidencia suficiente para clasificar la web
    # -------------------------------------------------------------------------
    def test_empty_or_undocumented_query_does_not_produce_no_encontrada(self) -> None:
        # Consulta vacía con status COMPLETA y sin evidencia
        q_empty = WebQuery(consulta="", resultado=QueryOutcomeStatus.COMPLETA, evidence_ids=[])
        status, conf, url, reasons = evaluate_website_review([q_empty], [])
        self.assertEqual(
            status,
            WebsiteStatus.PENDIENTE_VERIFICACION,
            "Consulta con texto vacío no es suficiente; debe quedar PENDIENTE_VERIFICACION",
        )

        # Consulta con texto pero sin evidence_ids
        q_no_ev = WebQuery(consulta="cafe bogota", resultado=QueryOutcomeStatus.COMPLETA, evidence_ids=[])
        status2, _, _, _ = evaluate_website_review([q_no_ev], [])
        self.assertEqual(
            status2,
            WebsiteStatus.PENDIENTE_VERIFICACION,
            "Consulta sin evidence_ids no está documentada; debe quedar PENDIENTE_VERIFICACION",
        )

        # Consulta con espacios en blanco
        q_whitespace = WebQuery(consulta="   ", resultado=QueryOutcomeStatus.COMPLETA, evidence_ids=["E1"])
        status3, _, _, _ = evaluate_website_review([q_whitespace], [])
        self.assertEqual(
            status3,
            WebsiteStatus.PENDIENTE_VERIFICACION,
            "Consulta con espacios no es suficiente; debe quedar PENDIENTE_VERIFICACION",
        )

        # Consulta suficiente: texto válido, COMPLETA y con evidencia
        q_valid = WebQuery(consulta="Cafe El Roble Bogota", resultado=QueryOutcomeStatus.COMPLETA, evidence_ids=["E1"])
        status4, _, _, _ = evaluate_website_review([q_valid], [])
        self.assertEqual(status4, WebsiteStatus.NO_ENCONTRADA)

    def test_invalid_or_empty_url_cannot_sustain_oficial_encontrada(self) -> None:
        for invalid_url in ["", "   ", "not-a-valid-url", "ftp://example.com"]:
            c_invalid = WebCandidate(
                url=invalid_url,
                tipo=WebCandidateType.OFICIAL,
                identidad=IdentityMatch.COINCIDE,
                evidence_ids=["E1"],
            )
            status, conf, url, reasons = evaluate_website_review(
                queries=[WebQuery("test", QueryOutcomeStatus.COMPLETA, ["E1"])],
                candidates=[c_invalid],
            )
            self.assertEqual(
                status,
                WebsiteStatus.PENDIENTE_VERIFICACION,
                f"URL '{invalid_url}' no puede sustentar OFICIAL_ENCONTRADA",
            )

    # -------------------------------------------------------------------------
    # 3. Corroboración de candidatos de redes sociales
    # -------------------------------------------------------------------------
    def test_social_candidate_strict_corroboration_rules(self) -> None:
        # A. Nombre + ciudad solos SIN teléfono/dirección ni enlace oficial -> CANDIDATO (no CONFIRMADO)
        status, conf, reason = evaluate_social_candidate(
            business_name="Pastelería Dulce Sabor",
            business_city="Bogota",
            business_phone=None,
            candidate_url="https://instagram.com/dulcesabor",
            profile_name="Pastelería Dulce Sabor",
            profile_city="Bogota",
            profile_phone=None,
        )
        self.assertEqual(
            status,
            SocialVerificationStatus.CANDIDATO,
            "Nombre y ciudad solos no deben confirmar; deben quedar como CANDIDATO",
        )

        # B. Teléfono coincide pero el nombre de perfil es de un negocio contradictorio
        status_contra, _, reason_contra = evaluate_social_candidate(
            business_name="Pastelería Dulce Sabor",
            business_city="Bogota",
            business_phone="+573001112233",
            candidate_url="https://instagram.com/tallermecanico",
            profile_name="Taller Mecánico Los Andes",
            profile_city="Bogota",
            profile_phone="+573001112233",
        )
        self.assertNotEqual(
            status_contra,
            SocialVerificationStatus.CONFIRMADO,
            "No se debe confirmar puramente por teléfono con nombre de negocio incompatible",
        )

        # C. Corroboración fuerte: Nombre coincidente + Teléfono verificado coincidente -> CONFIRMADO
        status_ok, conf_ok, _ = evaluate_social_candidate(
            business_name="Pastelería Dulce Sabor",
            business_city="Bogota",
            business_phone="+573001112233",
            candidate_url="https://instagram.com/dulcesaborbogota",
            profile_name="Pastelería Dulce Sabor",
            profile_city="Bogota",
            profile_phone="+573001112233",
        )
        self.assertEqual(status_ok, SocialVerificationStatus.CONFIRMADO)
        self.assertEqual(conf_ok, ConfidenceLevel.ALTA)

    # -------------------------------------------------------------------------
    # 4. Umbrales efectivos y estado social vigente en el ranking
    # -------------------------------------------------------------------------
    def test_cli_respects_custom_filter_thresholds_in_ranking(self) -> None:
        # Negocio con rating 4.2 y 20 reseñas
        fixture_data = {
            "source": "test-src",
            "records": [
                {
                    "name": "Pizzería Nápoles",
                    "category": "Pizzeria",
                    "country": "Colombia",
                    "city": "Bogota",
                    "address": "Calle 50 #10-20",
                    "phone": "+573005556677",
                    "rating": 4.2,
                    "review_count": 20,
                    "business_status": "ACTIVO",
                }
            ],
        }
        fix_path = self.root / "fixture_thresholds.json"
        fix_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        filters = SearchFilters(
            country="Colombia",
            city="Bogota",
            category="Pizzeria",
            min_rating=4.0,
            min_reviews=15,
        )

        # 1. Primera pasada para registrar el prospecto
        report1 = cli.run_search(
            filters=filters,
            provider=FixtureProvider(fix_path),
            db_path=self.db_path,
            output_dir=self.root / "reports",
            with_ranking=False,
        )
        pid = report1.prospects[0]["id"]

        # Agregamos revisión web favorable (SOLO_REDES_SOCIALES)
        with CaptacionWebStore(self.db_path) as store:
            store.add_website_review(
                pid,
                WebsiteReview(
                    status=WebsiteStatus.SOLO_REDES_SOCIALES,
                    confidence=ConfidenceLevel.ALTA,
                    official_url=None,
                    reviewer="OP_NAPOLES",
                    reviewed_at="2026-11-21T10:00:00+00:00",
                ),
            )

        # 2. Segunda pasada con ranking
        report2 = cli.run_search(
            filters=filters,
            provider=FixtureProvider(fix_path),
            db_path=self.db_path,
            output_dir=self.root / "reports",
            with_ranking=True,
        )

        p = report2.prospects[0]
        self.assertIn("ranking", p)
        # Con min_rating=4.0 y min_reviews=15, rating (4.2) y reviews (20) cumplen el umbral
        # Si hubiera usado el default (4.5 y 30), eligible sería False
        self.assertTrue(
            p["ranking"]["eligible"],
            "El ranking debe respetar los umbrales efectivos pasados por el CLI (4.0, 15)",
        )
        self.assertFalse(p["ranking"]["ready_for_contact"])
        self.assertTrue(p["ranking"]["preliminary_commercial_fit"])

    def test_ranking_evaluates_only_latest_vigente_social_status(self) -> None:
        with CaptacionWebStore(self.db_path) as store:
            pid = "prospect-social-test"
            store.conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, 'src', 'local:v4:p1', 'LOCAL', 'Cafe Bar', 'Bar',
                          'Colombia', 'Bogota', 'Calle 10 #5-2', '+573001234567', NULL,
                          'ACTIVO', '2026-11-21T10:00:00+00:00', '2026-11-21T10:00:00+00:00')
                """,
                (pid,),
            )
            store.conn.commit()

            # Revisión 1: Instagram marcado como CONFIRMADO a las 10:00
            cand_v1 = SocialCandidate(
                platform=SocialPlatform.INSTAGRAM,
                url="https://instagram.com/cafebar",
                status=SocialVerificationStatus.CONFIRMADO,
                confidence=ConfidenceLevel.ALTA,
                reviewed_at="2026-11-21T10:00:00+00:00",
                reviewer="OPERADOR_1",
            )
            store.add_social_candidate(pid, cand_v1)

            # Revisión 2: Mismo Instagram corregido a DESCARTADO a las 11:00 (se descubrió homónimo)
            cand_v2 = SocialCandidate(
                platform=SocialPlatform.INSTAGRAM,
                url="https://instagram.com/cafebar",
                status=SocialVerificationStatus.DESCARTADO,
                confidence=ConfidenceLevel.ALTA,
                reviewed_at="2026-11-21T11:00:00+00:00",
                reviewer="OPERADOR_2",
            )
            store.add_social_candidate(pid, cand_v2)

            # Obtener candidatos vigentes y calcular ranking
            all_candidates = [
                SocialCandidate(
                    platform=SocialPlatform(c["platform"]),
                    url=c["url"],
                    status=SocialVerificationStatus(c["status"]),
                    confidence=ConfidenceLevel(c["confidence"]),
                    reviewed_at=c.get("reviewed_at", ""),
                )
                for c in store.get_social_candidates(pid)
            ]

            res = calculate_commercial_ranking(
                prospect_data={
                    "name": "Cafe Bar",
                    "category": "Bar",
                    "country": "Colombia",
                    "city": "Bogota",
                    "address": "Calle 10 #5-2",
                    "phone": "+573001234567",
                    "rating": 4.8,
                    "review_count": 50,
                    "business_status": "ACTIVO",
                    "external_id_kind": EXTERNAL_ID_LOCAL,
                },
                website_review=WebsiteReview(
                    status=WebsiteStatus.SOLO_AGREGADOR,
                    confidence=ConfidenceLevel.ALTA,
                    official_url=None,
                    reviewer="OP",
                    reviewed_at="2026-11-21T11:00:00+00:00",
                ),
                social_profiles=all_candidates,
            )

            # El score social debe ser 0 porque el estado vigente del perfil es DESCARTADO
            self.assertEqual(
                res.breakdown["social"]["confirmed_count"],
                0,
                "El perfil de Instagram descartado posteriormente no debe contarse como confirmado",
            )
            self.assertEqual(res.breakdown["social"]["points"], 0.0)

    # -------------------------------------------------------------------------
    # 5. Flujo operativo de revisión manual vía CLI
    # -------------------------------------------------------------------------
    def test_ambiguous_external_id_rejects_manual_review(self) -> None:
        with CaptacionWebStore(self.db_path) as store:
            for pid, source in (("internal-a", "source-a"), ("internal-b", "source-b")):
                store.conn.execute(
                    """INSERT INTO prospect
                       (id, source, external_id, external_id_kind, name, category,
                        country, city, business_status, first_seen_at, last_seen_at)
                       VALUES (?, ?, 'shared', 'PROVIDER', 'Cafe', 'Cafe',
                               'Colombia', 'Bogota', 'ACTIVO', '2026-11-21', '2026-11-21')""",
                    (pid, source),
                )
            store.conn.commit()
        args = ["review-web", "--db", str(self.db_path), "--status",
                "PENDIENTE_VERIFICACION", "--confidence", "baja", "--reviewer", "REVISOR"]
        self.assertNotEqual(cli.main([*args, "--prospect-id", "shared"]), 0)
        with CaptacionWebStore(self.db_path) as store:
            self.assertEqual(store.get_website_reviews("internal-a"), [])
            self.assertEqual(store.get_website_reviews("internal-b"), [])
        self.assertEqual(cli.main([*args, "--prospect-id", "internal-b"]), 0)
        with CaptacionWebStore(self.db_path) as store:
            self.assertEqual(len(store.get_website_reviews("internal-b")), 1)

    def test_web_review_ties_use_last_inserted_and_keep_history(self) -> None:
        with CaptacionWebStore(self.db_path) as store:
            store.conn.execute(
                """INSERT INTO prospect
                   (id, source, external_id, external_id_kind, name, category,
                    country, city, business_status, first_seen_at, last_seen_at)
                   VALUES ('internal', 'source', 'external', 'PROVIDER', 'Cafe', 'Cafe',
                           'Colombia', 'Bogota', 'ACTIVO', '2026-11-21', '2026-11-21')"""
            )
            store.conn.commit()
            args = dict(confidence=ConfidenceLevel.MEDIA, reviewer="REVISOR",
                        reviewed_at="2026-11-21T10:00:00+00:00")
            from unittest.mock import patch
            with patch("prospector.captacion_web.storage.utc_now_iso", return_value=args["reviewed_at"]):
                first = store.add_website_review("internal", WebsiteReview(
                    status=WebsiteStatus.NO_ENCONTRADA, official_url=None, **args))
                last = store.add_website_review("internal", WebsiteReview(
                    status=WebsiteStatus.OFICIAL_ENCONTRADA, official_url="https://example.test", **args))
            self.assertEqual(store.get_latest_website_review("internal")["id"], last)
            self.assertEqual([r["id"] for r in store.get_website_reviews("internal")], [first, last])

    def test_ranking_does_not_treat_observed_phone_as_verified(self) -> None:
        review = WebsiteReview(
            status=WebsiteStatus.NO_ENCONTRADA, confidence=ConfidenceLevel.MEDIA,
            official_url=None, reviewer="REVISOR", reviewed_at="2026-11-21T10:00:00+00:00",
        )
        result = calculate_commercial_ranking({"phone": "+573001112233"}, review, [])
        channel = result.breakdown["public_channel"]
        self.assertTrue(channel["phone_present"])
        self.assertEqual(channel["points"], 0)

    def test_ranking_does_not_claim_social_activity_without_evidence(self) -> None:
        review = WebsiteReview(
            status=WebsiteStatus.SOLO_REDES_SOCIALES, confidence=ConfidenceLevel.MEDIA,
            official_url=None, reviewer="REVISOR", reviewed_at="2026-11-21T10:00:00+00:00",
        )
        result = calculate_commercial_ranking({}, review, [])
        self.assertGreater(result.breakdown["web"]["points"], 0)
        self.assertFalse(any("presencia activa" in reason for reason in result.reasons))

    def test_public_review_validation_and_atomic_import(self) -> None:
        with CaptacionWebStore(self.db_path) as store:
            store.conn.execute(
                """INSERT INTO prospect (id, source, external_id, external_id_kind, name,
                   category, country, city, business_status, first_seen_at, last_seen_at)
                   VALUES ('p', 'fixture', 'e', 'PROVIDER', 'Cafe', 'Cafe', 'Colombia',
                           'Bogota', 'ACTIVO', '2026-11-21', '2026-11-21')"""
            )
            store.conn.commit()
        common = ["--db", str(self.db_path), "--prospect-id", "p", "--confidence", "media",
                  "--reviewer", "OPERADOR"]
        self.assertEqual(cli.main(["review-web", *common, "--status", "NO_ENCONTRADA",
                                   "--query", "Cafe Bogota"]), 1)
        self.assertEqual(cli.main(["review-social", *common, "--platform", "INSTAGRAM",
                                   "--url", "garbage", "--status", "CONFIRMADO",
                                   "--signals", "solo nombre"]), 1)
        batch = self.db_path.with_suffix(".json")
        good = {"prospect_id": "p", "reviewer": "OPERADOR", "status": "NO_ENCONTRADA",
                "query": "Cafe Bogota sitio oficial", "evidence_ids": ["E01"]}
        batch.write_text(json.dumps({"website_reviews": [good, {**good, "status": "INVALIDO"}]}),
                         encoding="utf-8")
        cmd = ["import-reviews", "--db", str(self.db_path), "--input", str(batch)]
        self.assertEqual(cli.main(cmd), 1)
        with CaptacionWebStore(self.db_path) as store:
            self.assertEqual(store.get_website_reviews("p"), [])
            self.assertEqual(store.get_social_candidates("p"), [])
        batch.write_text(json.dumps({"website_reviews": [good]}), encoding="utf-8")
        self.assertEqual(cli.main(cmd), 0)
        with CaptacionWebStore(self.db_path) as store:
            self.assertEqual(len(store.get_website_reviews("p")), 1)

        weak_social = {"type": "social", "prospect_id": "p", "reviewer": "OPERADOR",
                       "platform": "INSTAGRAM", "url": "https://instagram.com/cafe",
                       "status": "CONFIRMADO", "signals": "solo nombre", "evidence_ids": ["E02"]}
        batch.write_text(json.dumps({"reviews": [weak_social]}), encoding="utf-8")
        self.assertEqual(cli.main(cmd), 1)
        with CaptacionWebStore(self.db_path) as store:
            self.assertEqual(store.get_social_candidates("p"), [])

    def test_ranking_snapshot_ties_return_last_inserted(self) -> None:
        from unittest.mock import patch

        with CaptacionWebStore(self.db_path) as store:
            store.conn.execute(
                """INSERT INTO prospect (id, source, external_id, external_id_kind, name,
                   category, country, city, business_status, first_seen_at, last_seen_at)
                   VALUES ('p', 'fixture', 'e', 'PROVIDER', 'Cafe', 'Cafe', 'Colombia',
                           'Bogota', 'ACTIVO', '2026-11-21', '2026-11-21')"""
            )
            store.conn.commit()
            ranking = calculate_commercial_ranking({}, None, [])
            with patch("prospector.captacion_web.storage.utc_now_iso", return_value="2026-11-21T10:00:00+00:00"):
                store.add_ranking_snapshot("p", ranking)
                last = store.add_ranking_snapshot("p", ranking)
            self.assertEqual(store.get_latest_ranking_snapshot("p")["id"], last)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM ranking_snapshot").fetchone()[0], 2)

    def test_cli_manual_review_workflow(self) -> None:
        # Crear prospecto inicial en la base
        pid = "prospect-review-cli"
        with CaptacionWebStore(self.db_path) as store:
            store.conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, 'test-src', 'local:v4:p_rev', 'LOCAL', 'Arepas Dona Luz', 'Restaurante',
                          'Colombia', 'Bogota', 'Cra 15 #80-10', '+573101112233', NULL,
                          'ACTIVO', '2026-11-21T10:00:00+00:00', '2026-11-21T10:00:00+00:00')
                """,
                (pid,),
            )
            store.conn.commit()

        # 1. Registrar revisión web vía CLI
        web_code = cli.main(
            [
                "review-web",
                "--db",
                str(self.db_path),
                "--prospect-id",
                pid,
                "--status",
                "NO_ENCONTRADA",
                "--confidence",
                "alta",
                "--reviewer",
                "REVISOR_OPERATIVO",
                "--evidence-ids",
                "E101,E102",
                "--query",
                "Arepas Dona Luz Bogota sitio oficial",
                "--notes",
                "Búsqueda exhaustiva sin web propia",
            ]
        )
        self.assertEqual(web_code, 0, "review-web debe salir con código 0")

        # 2. Registrar revisión de red social vía CLI
        social_code = cli.main(
            [
                "review-social",
                "--db",
                str(self.db_path),
                "--prospect-id",
                pid,
                "--platform",
                "INSTAGRAM",
                "--url",
                "https://instagram.com/arepasdonaluz",
                "--status",
                "CONFIRMADO",
                "--confidence",
                "alta",
                "--reviewer",
                "REVISOR_OPERATIVO",
                "--signals",
                "Teléfono y dirección coinciden con sede",
                "--corroboration",
                "NOMBRE_SEDE_DIRECCION",
                "--evidence-ids",
                "E102",
            ]
        )
        self.assertEqual(social_code, 0, "review-social debe salir con código 0")

        # 3. Consultar revisiones vía CLI
        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            show_code = cli.main(
                [
                    "show-reviews",
                    "--db",
                    str(self.db_path),
                    "--prospect-id",
                    pid,
                ]
            )
        finally:
            sys.stdout = old_stdout

        self.assertEqual(show_code, 0, "show-reviews debe salir con código 0")
        output = captured.getvalue()
        self.assertIn("NO_ENCONTRADA", output)
        self.assertIn("https://instagram.com/arepasdonaluz", output)
        self.assertIn("REVISOR_OPERATIVO", output)

    # -------------------------------------------------------------------------
    # 6. Compatibilidad con base SQLite sintética de Etapa 1
    # -------------------------------------------------------------------------
    def test_sqlite_stage1_compatibility_and_reopen(self) -> None:
        # 1. Crear una base SQLite con esquema de Etapa 1 exclusivamente
        stage1_schema = """
        CREATE TABLE search_run (
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

        CREATE TABLE query_attempt (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES search_run(id),
            query TEXT NOT NULL,
            status TEXT NOT NULL,
            records_seen INTEGER NOT NULL,
            records_invalid INTEGER NOT NULL,
            records_filtered_out INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE prospect (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            external_id_kind TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            country TEXT NOT NULL,
            city TEXT NOT NULL,
            address_public TEXT,
            phone_public TEXT,
            website_url TEXT,
            business_status TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(source, external_id)
        );

        CREATE TABLE observation (
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
            raw_json TEXT NOT NULL,
            UNIQUE(prospect_id, run_id)
        );
        """
        conn = sqlite3.connect(self.db_path)
        conn.executescript(stage1_schema)

        # Insertar registros existentes con claves históricas local:, local:v2:, local:v3:, local:v4:
        records_to_insert = [
            ("p-01", "src1", "local:cafe_norte|calle_1", "LOCAL", "Cafe Norte 1"),
            ("p-02", "src1", "local:v2:restaurante_bogota_co", "LOCAL", "Restaurante Bogota"),
            ("p-03", "src1", 'local:v3:{"name":"el_nino","city":"bogota"}', "LOCAL", "El Niño v3"),
            ("p-04", "src1", 'local:v4:{"city":"bogota","country":"colombia","name":"el nino"}', "LOCAL", "El Niño v4"),
        ]
        for pid, src, ext_id, kind, name in records_to_insert:
            conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, 'Cafeteria', 'Colombia', 'Bogota',
                          'Calle 1', '+573001112233', NULL, 'ACTIVO',
                          '2026-11-20T10:00:00+00:00', '2026-11-20T10:00:00+00:00')
                """,
                (pid, src, ext_id, kind, name),
            )
        conn.commit()
        conn.close()

        # 2. Abrir con CaptacionWebStore (Etapa 2) y verificar que las tablas se agregan sin borrar filas
        with CaptacionWebStore(self.db_path) as store:
            count = store.conn.execute("SELECT COUNT(*) FROM prospect").fetchone()[0]
            self.assertEqual(count, 4, "Todas las filas preexistentes deben conservarse")

            # Verificar que las nuevas tablas de Etapa 2 existen
            tables = [
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            self.assertIn("website_review", tables)
            self.assertIn("social_candidate", tables)
            self.assertIn("ranking_snapshot", tables)

            # Insertar una revisión web en uno de los registros existentes
            store.add_website_review(
                "p-01",
                WebsiteReview(
                    status=WebsiteStatus.SOLO_REDES_SOCIALES,
                    confidence=ConfidenceLevel.ALTA,
                    official_url=None,
                    reviewer="OP_STAGE2",
                    reviewed_at="2026-11-21T21:00:00+00:00",
                ),
            )

        # 3. Reabrir por segunda vez para verificar idempotencia
        with CaptacionWebStore(self.db_path) as store2:
            count2 = store2.conn.execute("SELECT COUNT(*) FROM prospect").fetchone()[0]
            self.assertEqual(count2, 4)
            reviews = store2.get_website_reviews("p-01")
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]["reviewer"], "OP_STAGE2")


if __name__ == "__main__":
    unittest.main()

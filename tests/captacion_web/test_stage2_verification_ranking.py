"""Pruebas de la Etapa 2: Verificación web manual, redes sociales y ranking comercial.

Solo usa datos sintéticos y bases temporales. No accede a la base operativa,
ni a credenciales, ni a servicios externos.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from prospector.captacion_web.models import (
    EXTERNAL_ID_LOCAL,
    EXTERNAL_ID_REVISION,
    SearchFilters,
)
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

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STAGE2_FIXTURE = FIXTURES / "stage2_synthetic_cases.json"


def _load_cases() -> dict[str, dict]:
    data = json.loads(STAGE2_FIXTURE.read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


class _TemporaryWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "stage2_test.db"
        self.cases = _load_cases()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestWebsiteManualVerification(_TemporaryWorkspace):
    def test_official_found_even_if_source_url_is_none(self) -> None:
        case = self.cases["case-01-web-corroborada"]
        self.assertIsNone(case["website_url"])

        queries = [
            WebQuery(
                consulta=q["consulta"],
                resultado=QueryOutcomeStatus(q["resultado"]),
                evidence_ids=q["evidence_ids"],
            )
            for q in case["manual_evidence"]["queries"]
        ]
        candidates = [
            WebCandidate(
                url=c["url"],
                tipo=WebCandidateType(c["tipo"]),
                identidad=IdentityMatch(c["identidad"]),
                evidence_ids=c["evidence_ids"],
            )
            for c in case["manual_evidence"]["candidates"]
        ]

        status, confidence, official_url, reasons = evaluate_website_review(
            queries=queries,
            candidates=candidates,
        )

        self.assertEqual(status, WebsiteStatus.OFICIAL_ENCONTRADA)
        self.assertEqual(confidence, ConfidenceLevel.ALTA)
        self.assertEqual(official_url, "https://eltrigodorado.wixsite.com/panaderia")
        self.assertTrue(any("constructor" in r.lower() or "oficial" in r.lower() for r in reasons))

    def test_solo_red_social_vs_solo_agregador_precedence(self) -> None:
        # Case 2: Solo red social confirmada
        c2 = self.cases["case-02-solo-red-social"]
        status2, conf2, url2, _ = evaluate_website_review(
            queries=[WebQuery(c2["manual_evidence"]["queries"][0]["consulta"], QueryOutcomeStatus.COMPLETA, ["E10"])],
            candidates=[WebCandidate(c2["manual_evidence"]["candidates"][0]["url"], WebCandidateType.RED_SOCIAL, IdentityMatch.COINCIDE, ["E11"])],
            social_profiles=[
                SocialCandidate(
                    platform=SocialPlatform.INSTAGRAM,
                    url=c2["manual_evidence"]["social_profiles"][0]["url"],
                    status=SocialVerificationStatus.CONFIRMADO,
                    confidence=ConfidenceLevel.ALTA,
                    signals="coincide direccion",
                    evidence_ids=["E11"],
                )
            ],
        )
        self.assertEqual(status2, WebsiteStatus.SOLO_REDES_SOCIALES)
        self.assertIsNone(url2)

        # Case 3: Solo agregador
        c3 = self.cases["case-03-solo-agregador"]
        status3, conf3, url3, _ = evaluate_website_review(
            queries=[WebQuery(c3["manual_evidence"]["queries"][0]["consulta"], QueryOutcomeStatus.COMPLETA, ["E20"])],
            candidates=[WebCandidate(c3["manual_evidence"]["candidates"][0]["url"], WebCandidateType.AGREGADOR, IdentityMatch.COINCIDE, ["E21"])],
        )
        self.assertEqual(status3, WebsiteStatus.SOLO_AGREGADOR)
        self.assertIsNone(url3)

        # Case 4: Red social confirmada Y agregador -> Precedencia: SOLO_REDES_SOCIALES
        c4 = self.cases["case-04-red-y-agregador"]
        status4, conf4, url4, reasons4 = evaluate_website_review(
            queries=[WebQuery(c4["manual_evidence"]["queries"][0]["consulta"], QueryOutcomeStatus.COMPLETA, ["E30"])],
            candidates=[
                WebCandidate(c4["manual_evidence"]["candidates"][0]["url"], WebCandidateType.RED_SOCIAL, IdentityMatch.COINCIDE, ["E31"]),
                WebCandidate(c4["manual_evidence"]["candidates"][1]["url"], WebCandidateType.AGREGADOR, IdentityMatch.COINCIDE, ["E32"]),
            ],
            social_profiles=[
                SocialCandidate(
                    platform=SocialPlatform.FACEBOOK,
                    url=c4["manual_evidence"]["social_profiles"][0]["url"],
                    status=SocialVerificationStatus.CONFIRMADO,
                    confidence=ConfidenceLevel.ALTA,
                    signals="coincide telefono",
                    evidence_ids=["E31"],
                )
            ],
        )
        self.assertEqual(status4, WebsiteStatus.SOLO_REDES_SOCIALES)
        self.assertTrue(any("agregador" in r.lower() for r in reasons4))

    def test_official_unavailable_and_blocked_queries(self) -> None:
        # Case 5: Sitio oficial corroborado pero inaccesible -> OFICIAL_NO_DISPONIBLE
        c5 = self.cases["case-05-oficial-inaccesible"]
        status5, _, official_url5, reasons5 = evaluate_website_review(
            queries=[WebQuery(c5["manual_evidence"]["queries"][0]["consulta"], QueryOutcomeStatus.COMPLETA, ["E40"])],
            candidates=[
                WebCandidate(
                    url=c5["manual_evidence"]["candidates"][0]["url"],
                    tipo=WebCandidateType.OFICIAL,
                    identidad=IdentityMatch.COINCIDE,
                    evidence_ids=["E41"],
                    access_error="HTTP 503",
                )
            ],
        )
        self.assertEqual(status5, WebsiteStatus.OFICIAL_NO_DISPONIBLE)
        self.assertEqual(official_url5, "https://bistrolamontana.example.invalid")
        self.assertTrue(any("inaccesible" in r.lower() or "503" in r for r in reasons5))

        # Case 6: Búsqueda bloqueada o parcial -> PENDIENTE_VERIFICACION
        c6 = self.cases["case-06-busqueda-bloqueada"]
        status6, _, _, reasons6 = evaluate_website_review(
            queries=[
                WebQuery(
                    consulta=c6["manual_evidence"]["queries"][0]["consulta"],
                    resultado=QueryOutcomeStatus.BLOQUEADA,
                    evidence_ids=["E50"],
                    error=c6["manual_evidence"]["queries"][0]["error"],
                )
            ],
            candidates=[],
        )
        self.assertEqual(status6, WebsiteStatus.PENDIENTE_VERIFICACION)
        self.assertTrue(any("bloqueada" in r.lower() or "incompleta" in r.lower() for r in reasons6))

        # Búsqueda suficiente vacía sin candidatos -> NO_ENCONTRADA
        status_empty, _, _, reasons_empty = evaluate_website_review(
            queries=[WebQuery("Restaurante Bogota", QueryOutcomeStatus.COMPLETA, ["E99"])],
            candidates=[],
        )
        self.assertEqual(status_empty, WebsiteStatus.NO_ENCONTRADA)


class TestSocialCandidates(_TemporaryWorkspace):
    def test_homonyms_handling(self) -> None:
        # Perfil homónimo 1 con teléfono y ciudad concordantes -> CONFIRMADO
        status1, conf1, notes1 = evaluate_social_candidate(
            business_name="Café Central",
            business_city="Bogota",
            business_phone="+573001112233",
            candidate_url="https://instagram.com/cafecentralbogota",
            profile_name="Café Central",
            profile_bio="Especialidad en Bogotá. Tel: +573001112233",
            profile_city="Bogota",
            profile_phone="+573001112233",
        )
        self.assertEqual(status1, SocialVerificationStatus.CONFIRMADO)
        self.assertEqual(conf1, ConfidenceLevel.ALTA)

        # Perfil homónimo 2 en ciudad distinta (Madrid) y teléfono discordante -> DESCARTADO
        status2, conf2, notes2 = evaluate_social_candidate(
            business_name="Café Central",
            business_city="Bogota",
            business_phone="+573001112233",
            candidate_url="https://instagram.com/cafecentralmadrid",
            profile_name="Café Central Madrid",
            profile_bio="El mejor café de Madrid, España",
            profile_city="Madrid",
            profile_phone="+34910000000",
        )
        self.assertEqual(status2, SocialVerificationStatus.DESCARTADO)

        # Perfil homónimo 3 sin datos de contacto ni ciudad -> CANDIDATO (no se confirma solo por nombre)
        status3, conf3, notes3 = evaluate_social_candidate(
            business_name="Café Central",
            business_city="Bogota",
            business_phone="+573001112233",
            candidate_url="https://instagram.com/cafecentral",
            profile_name="Café Central",
            profile_bio="Amamos el buen café",
            profile_city=None,
            profile_phone=None,
        )
        self.assertEqual(status3, SocialVerificationStatus.CANDIDATO)
        self.assertEqual(conf3, ConfidenceLevel.BAJA)
        self.assertTrue(any("homónimo" in n.lower() or "insuficiente" in n.lower() for n in [notes3]))


class TestCommercialRanking(_TemporaryWorkspace):
    def test_ranking_deterministic_and_breakdown(self) -> None:
        c2 = self.cases["case-02-solo-red-social"]
        prospect_data = {
            "name": c2["name"],
            "category": c2["category"],
            "country": c2["country"],
            "city": c2["city"],
            "address": c2["address"],
            "phone": c2["phone"],
            "rating": c2["rating"],
            "review_count": c2["review_count"],
            "business_status": c2["business_status"],
            "external_id_kind": EXTERNAL_ID_LOCAL,
        }
        review = WebsiteReview(
            status=WebsiteStatus.SOLO_REDES_SOCIALES,
            confidence=ConfidenceLevel.ALTA,
            official_url=None,
            reviewer="REVISOR_SINTETICO",
            reviewed_at="2026-11-21T22:00:00+00:00",
            queries=[],
            candidates=[],
            evidence_ids=["E10", "E11"],
        )
        socials = [
            SocialCandidate(
                platform=SocialPlatform.INSTAGRAM,
                url="https://instagram.com/boutiquecafechapinero",
                status=SocialVerificationStatus.CONFIRMADO,
                confidence=ConfidenceLevel.ALTA,
                signals="coincide direccion",
                evidence_ids=["E11"],
            )
        ]

        result1 = calculate_commercial_ranking(prospect_data, review, socials)
        result2 = calculate_commercial_ranking(prospect_data, review, socials)

        # 1. Determinismo
        self.assertEqual(result1.score, result2.score)
        self.assertEqual(result1.eligible, result2.eligible)
        self.assertEqual(result1.breakdown, result2.breakdown)

        # 2. Desglose presente y estructurado
        self.assertIsNotNone(result1.score)
        self.assertTrue(result1.eligible)
        self.assertFalse(result1.ready_for_contact)
        self.assertTrue(result1.preliminary_commercial_fit)
        self.assertIn("web", result1.breakdown)
        self.assertIn("rating", result1.breakdown)
        self.assertIn("reviews", result1.breakdown)
        self.assertIn("social", result1.breakdown)
        self.assertIn("niche", result1.breakdown)
        self.assertIn("public_channel", result1.breakdown)

    def test_configurable_weights(self) -> None:
        c2 = self.cases["case-02-solo-red-social"]
        prospect_data = {
            "name": c2["name"],
            "category": c2["category"],
            "country": c2["country"],
            "city": c2["city"],
            "address": c2["address"],
            "phone": c2["phone"],
            "rating": c2["rating"],
            "review_count": c2["review_count"],
            "business_status": c2["business_status"],
            "external_id_kind": EXTERNAL_ID_LOCAL,
        }
        review = WebsiteReview(
            status=WebsiteStatus.SOLO_REDES_SOCIALES,
            confidence=ConfidenceLevel.ALTA,
            official_url=None,
            reviewer="REVISOR_SINTETICO",
            reviewed_at="2026-11-21T22:00:00+00:00",
            queries=[],
            candidates=[],
            evidence_ids=["E10"],
        )

        default_result = calculate_commercial_ranking(prospect_data, review, [])

        # Configuración personalizada con mayor peso a web (50) y menor a rating (10)
        custom_config = RankingConfig(
            weight_web=50.0,
            weight_rating=10.0,
            weight_reviews=10.0,
            weight_social=15.0,
            weight_niche=10.0,
            weight_public_channel=5.0,
            rule_version="custom-v1",
        )
        custom_result = calculate_commercial_ranking(
            prospect_data, review, [], config=custom_config
        )

        self.assertNotEqual(default_result.score, custom_result.score)
        self.assertEqual(custom_result.rule_version, "custom-v1")

    def test_high_score_with_pending_web_is_not_ready_for_contact(self) -> None:
        # Negocio con reputación excelente (5.0, 500 reviews), pero web PENDIENTE
        prospect_data = {
            "name": "Super Restaurante",
            "category": "Restaurante",
            "country": "Colombia",
            "city": "Bogota",
            "address": "Calle 100 #10-10",
            "phone": "+573009998877",
            "rating": 5.0,
            "review_count": 500,
            "business_status": "ACTIVO",
            "external_id_kind": EXTERNAL_ID_LOCAL,
        }
        pending_review = WebsiteReview(
            status=WebsiteStatus.PENDIENTE_VERIFICACION,
            confidence=ConfidenceLevel.BAJA,
            official_url=None,
            reviewer="REVISOR_SINTETICO",
            reviewed_at="2026-11-21T22:00:00+00:00",
            queries=[],
            candidates=[],
            evidence_ids=["E90"],
        )

        result = calculate_commercial_ranking(prospect_data, pending_review, [])
        self.assertFalse(result.eligible)
        self.assertFalse(result.ready_for_contact)
        self.assertTrue(any("pendiente" in r.lower() for r in result.reasons))

    def test_insufficient_identity_is_not_ready_for_contact(self) -> None:
        prospect_data = {
            "name": "Local Sin Direccion",
            "category": "Restaurante",
            "country": "Colombia",
            "city": "Bogota",
            "address": None,
            "phone": None,
            "rating": 4.8,
            "review_count": 80,
            "business_status": "ACTIVO",
            "external_id_kind": EXTERNAL_ID_REVISION,
        }
        review = WebsiteReview(
            status=WebsiteStatus.SOLO_REDES_SOCIALES,
            confidence=ConfidenceLevel.ALTA,
            official_url=None,
            reviewer="REVISOR_SINTETICO",
            reviewed_at="2026-11-21T22:00:00+00:00",
            queries=[],
            candidates=[],
            evidence_ids=["E91"],
        )

        result = calculate_commercial_ranking(prospect_data, review, [])
        self.assertFalse(result.eligible)
        self.assertFalse(result.ready_for_contact)
        self.assertTrue(any("revisión" in r.lower() or "identidad" in r.lower() for r in result.reasons))


class TestStage2PersistenceAndHistory(_TemporaryWorkspace):
    def test_review_history_is_preserved_without_deletion(self) -> None:
        with CaptacionWebStore(self.db_path) as store:
            # Crear prospecto sintético
            prospect_id = "test-prospect-uuid-001"
            store.conn.execute(
                """
                INSERT INTO prospect (
                    id, source, external_id, external_id_kind, name, category,
                    country, city, address_public, phone_public, website_url,
                    business_status, first_seen_at, last_seen_at
                ) VALUES (?, 'test-src', 'local:v4:test1', 'LOCAL', 'Cafe Test', 'Cafeteria',
                          'Colombia', 'Bogota', 'Calle 1 #2-3', '+573001112233', NULL,
                          'ACTIVO', '2026-11-21T20:00:00+00:00', '2026-11-21T20:00:00+00:00')
                """,
                (prospect_id,),
            )
            store.conn.commit()

            # Revisión 1: PENDIENTE_VERIFICACION
            review1 = WebsiteReview(
                status=WebsiteStatus.PENDIENTE_VERIFICACION,
                confidence=ConfidenceLevel.BAJA,
                official_url=None,
                reviewer="OPERADOR_A",
                reviewed_at="2026-11-21T21:00:00+00:00",
                queries=[],
                candidates=[],
                evidence_ids=["E01"],
                notes="Búsqueda inicial inconclusa",
            )
            store.add_website_review(prospect_id, review1)

            # Revisión 2: SOLO_REDES_SOCIALES tras encontrar Instagram
            review2 = WebsiteReview(
                status=WebsiteStatus.SOLO_REDES_SOCIALES,
                confidence=ConfidenceLevel.ALTA,
                official_url=None,
                reviewer="OPERADOR_B",
                reviewed_at="2026-11-21T22:00:00+00:00",
                queries=[],
                candidates=[],
                evidence_ids=["E01", "E02"],
                notes="Instagram verificado y confirmado",
            )
            store.add_website_review(prospect_id, review2)

            # Candidato de red social
            candidate = SocialCandidate(
                platform=SocialPlatform.INSTAGRAM,
                url="https://instagram.com/cafetest",
                status=SocialVerificationStatus.CONFIRMADO,
                confidence=ConfidenceLevel.ALTA,
                signals="coincide telefono y bio",
                evidence_ids=["E02"],
                reviewed_at="2026-11-21T22:00:00+00:00",
                reviewer="OPERADOR_B",
            )
            store.add_social_candidate(prospect_id, candidate)

            # Ranking snapshot
            ranking = calculate_commercial_ranking(
                prospect_data={
                    "name": "Cafe Test",
                    "category": "Cafeteria",
                    "country": "Colombia",
                    "city": "Bogota",
                    "address": "Calle 1 #2-3",
                    "phone": "+573001112233",
                    "rating": 4.8,
                    "review_count": 60,
                    "business_status": "ACTIVO",
                    "external_id_kind": EXTERNAL_ID_LOCAL,
                },
                website_review=review2,
                social_profiles=[candidate],
            )
            store.add_ranking_snapshot(prospect_id, ranking)

            # Verificación de persistencia de historial
            history = store.get_website_reviews(prospect_id)
            self.assertEqual(len(history), 2, "La nueva revisión no debe borrar la evidencia previa")
            self.assertEqual(history[0]["status"], WebsiteStatus.PENDIENTE_VERIFICACION.value)
            self.assertEqual(history[1]["status"], WebsiteStatus.SOLO_REDES_SOCIALES.value)

            latest = store.get_latest_website_review(prospect_id)
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], WebsiteStatus.SOLO_REDES_SOCIALES.value)
            self.assertEqual(latest["reviewer"], "OPERADOR_B")

            socials = store.get_social_candidates(prospect_id)
            self.assertEqual(len(socials), 1)
            self.assertEqual(socials[0]["platform"], "INSTAGRAM")
            self.assertEqual(socials[0]["status"], "CONFIRMADO")

            latest_rank = store.get_latest_ranking_snapshot(prospect_id)
            self.assertIsNotNone(latest_rank)
            self.assertEqual(latest_rank["eligible"], 1)
            self.assertEqual(latest_rank["ready_for_contact"], 0)
            self.assertAlmostEqual(latest_rank["score"], ranking.score, places=2)

    def test_run_search_with_ranking_flag(self) -> None:
        from prospector.captacion_web.cli import run_search
        from prospector.captacion_web.providers import FixtureProvider

        filters = SearchFilters("Colombia", "Bogota", "Panadería")
        fixture_path = FIXTURES / "synthetic_businesses.json"
        out_dir = self.root / "runs"

        report = run_search(
            filters=filters,
            provider=FixtureProvider(fixture_path),
            db_path=self.db_path,
            output_dir=out_dir,
            with_ranking=True,
        )

        self.assertEqual(report.status, "COMPLETA")
        self.assertTrue(len(report.prospects) > 0)
        self.assertIn("ranking", report.prospects[0])

        with CaptacionWebStore(self.db_path) as store:
            p_id = report.prospects[0]["id"]
            snapshot = store.get_latest_ranking_snapshot(p_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["rule_version"], "v1")


if __name__ == "__main__":
    unittest.main()

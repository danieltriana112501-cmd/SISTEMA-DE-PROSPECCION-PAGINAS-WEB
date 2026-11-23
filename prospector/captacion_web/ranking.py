"""Módulo de ranking comercial explicable y configurable (RF-06).

Reglas de negocio:
- Cálculo determinista, configurable, versionado y explicable.
- Separa elegibilidad, datos observados y puntuación estimada.
- Pesos iniciales propuestos en 05_MODELO_DE_DATOS.md:
  web: 35, rating: 20, reviews: 15, social: 15, niche: 10, public_channel: 5.
- Devuelve el desglose (breakdown) y señala los datos faltantes.
- Una puntuación alta NO convierte un caso web pendiente o una identidad
  insuficiente en un prospecto listo para contacto.
- No presenta el score como probabilidad de compra.
- No inventa actividad social, reputación, canales ni datos de nicho.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import EXTERNAL_ID_REVISION, normalize_text
from .verification import (
    SocialCandidate,
    SocialVerificationStatus,
    WebsiteReview,
    WebsiteStatus,
)

DEFAULT_NICHE_MULTIPLIERS = {
    "restaurante": 1.0,
    "cafeteria": 0.9,
    "cafe": 0.9,
    "panaderia": 0.85,
    "pasteleria": 0.85,
    "bar": 0.8,
    "hotel": 0.9,
    "odontologia": 0.95,
    "clinica": 0.9,
    "estetica": 0.9,
    "gimnasio": 0.85,
}


@dataclass(frozen=True)
class RankingConfig:
    weight_web: float = 35.0
    weight_rating: float = 20.0
    weight_reviews: float = 15.0
    weight_social: float = 15.0
    weight_niche: float = 10.0
    weight_public_channel: float = 5.0
    min_rating: float = 4.5
    min_reviews: int = 30
    rule_version: str = "v1"


@dataclass(frozen=True)
class RankingResult:
    score: float | None
    eligible: bool
    ready_for_contact: bool
    rule_version: str
    breakdown: dict[str, Any]
    reasons: list[str]
    missing_data: list[str]
    preliminary_commercial_fit: bool = False


def calculate_commercial_ranking(
    prospect_data: dict[str, Any],
    website_review: WebsiteReview | None = None,
    social_profiles: list[SocialCandidate] | None = None,
    config: RankingConfig | None = None,
    niche_weights: dict[str, float] | None = None,
) -> RankingResult:
    """Calcula el score comercial, elegibilidad y readiness para contacto."""
    cfg = config or RankingConfig()
    niches = niche_weights if niche_weights is not None else DEFAULT_NICHE_MULTIPLIERS

    reasons: list[str] = []
    missing_data: list[str] = []
    breakdown: dict[str, Any] = {}

    name = prospect_data.get("name") or "Negocio sin nombre"
    rating = prospect_data.get("rating")
    review_count = prospect_data.get("review_count")
    category = prospect_data.get("category") or ""
    phone = prospect_data.get("phone")
    business_status = prospect_data.get("business_status") or "DESCONOCIDO"
    external_id_kind = prospect_data.get("external_id_kind") or ""

    # 1. Evaluar elegibilidad básica de negocio
    is_closed = business_status.upper() == "CERRADO"
    if is_closed:
        reasons.append("Negocio marcado como CERRADO; no elegible.")

    is_revision_identity = external_id_kind == EXTERNAL_ID_REVISION
    if is_revision_identity:
        reasons.append("Identidad en REVISIÓN (falta dirección o evidencia unívoca); no elegible para contacto.")

    # Umbrales de reputación
    reputation_ok = True
    if rating is None:
        missing_data.append("rating")
        reputation_ok = False
        reasons.append("Sin calificación (rating ausente).")
    elif rating < cfg.min_rating:
        reputation_ok = False
        reasons.append(f"Rating ({rating}) por debajo del mínimo exigido ({cfg.min_rating}).")

    if review_count is None:
        missing_data.append("review_count")
        reputation_ok = False
        reasons.append("Sin número de reseñas (review_count ausente).")
    elif review_count < cfg.min_reviews:
        reputation_ok = False
        reasons.append(f"Número de reseñas ({review_count}) por debajo del mínimo exigido ({cfg.min_reviews}).")

    # Estado web
    web_status = website_review.status if website_review else WebsiteStatus.PENDIENTE_VERIFICACION
    web_is_opportunity = web_status in (
        WebsiteStatus.SOLO_REDES_SOCIALES,
        WebsiteStatus.SOLO_AGREGADOR,
        WebsiteStatus.NO_ENCONTRADA,
    )

    if web_status == WebsiteStatus.OFICIAL_ENCONTRADA:
        reasons.append("Sitio web oficial ya existe (OFICIAL_ENCONTRADA); no elegible para captación de nueva web.")
    elif web_status == WebsiteStatus.OFICIAL_NO_DISPONIBLE:
        reasons.append("Sitio web oficial registrado como inaccesible; requiere revisión antes de descartar o contactar.")
    elif web_status == WebsiteStatus.PENDIENTE_VERIFICACION:
        reasons.append("Verificación web PENDIENTE; evidencia insuficiente.")

    eligible = bool(reputation_ok and not is_closed and not is_revision_identity and web_is_opportunity)

    # 2. Cálculo de subscores de ranking
    total_score = 0.0

    # A. Oportunidad Web
    web_score = 0.0
    if web_status == WebsiteStatus.SOLO_REDES_SOCIALES:
        web_score = cfg.weight_web * 1.0
        reasons.append(f"Oportunidad alta: perfiles sociales identificados sin web propia encontrada (+{web_score:.1f} pts).")
    elif web_status == WebsiteStatus.SOLO_AGREGADOR:
        web_score = cfg.weight_web * 0.85
        reasons.append(f"Oportunidad media-alta: presencia solo en agregador (+{web_score:.1f} pts).")
    elif web_status == WebsiteStatus.NO_ENCONTRADA:
        web_score = cfg.weight_web * 0.70
        reasons.append(f"Oportunidad básica: búsqueda sin sitio web identificable (+{web_score:.1f} pts).")
    else:
        web_score = 0.0
    breakdown["web"] = {
        "status": web_status.value if isinstance(web_status, WebsiteStatus) else str(web_status),
        "weight": cfg.weight_web,
        "points": web_score,
    }
    total_score += web_score

    # B. Calificación (Rating)
    rating_score = 0.0
    if rating is not None:
        if rating >= 4.8:
            rating_score = cfg.weight_rating * 1.0
        elif rating >= 4.5:
            rating_score = cfg.weight_rating * 0.85
        elif rating >= 4.0:
            rating_score = cfg.weight_rating * 0.60
        else:
            rating_score = cfg.weight_rating * 0.30
    breakdown["rating"] = {
        "value": rating,
        "weight": cfg.weight_rating,
        "points": rating_score,
    }
    total_score += rating_score

    # C. Volumen de reseñas (Reviews)
    reviews_score = 0.0
    if review_count is not None:
        if review_count >= 100:
            reviews_score = cfg.weight_reviews * 1.0
        elif review_count >= 50:
            reviews_score = cfg.weight_reviews * 0.80
        elif review_count >= 30:
            reviews_score = cfg.weight_reviews * 0.60
        else:
            reviews_score = cfg.weight_reviews * 0.30
    breakdown["reviews"] = {
        "value": review_count,
        "weight": cfg.weight_reviews,
        "points": reviews_score,
    }
    total_score += reviews_score

    # D. Redes sociales confirmadas (evaluar solo estado vigente por perfil)
    vigente_socials: dict[tuple[str, str], SocialCandidate] = {}
    for s in (social_profiles or []):
        plat_val = s.platform.value if hasattr(s.platform, "value") else str(s.platform)
        clean_url = (s.url or "").strip().lower().rstrip("/")
        # Si no hay URL, usar la plataforma como clave única
        key = (plat_val, clean_url or plat_val)
        vigente_socials[key] = s

    confirmed_socials = [
        s for s in vigente_socials.values()
        if s.status == SocialVerificationStatus.CONFIRMADO
    ]
    distinct_platforms = {s.platform for s in confirmed_socials}
    social_score = 0.0
    if len(distinct_platforms) >= 2:
        social_score = cfg.weight_social * 1.0
        reasons.append(f"Múltiples canales sociales confirmados ({len(distinct_platforms)}) (+{social_score:.1f} pts).")
    elif len(distinct_platforms) == 1:
        social_score = cfg.weight_social * 0.70
        reasons.append(f"Canal social confirmado ({list(distinct_platforms)[0].value}) (+{social_score:.1f} pts).")
    else:
        social_score = 0.0
        if not social_profiles:
            missing_data.append("social_profiles")
    breakdown["social"] = {
        "confirmed_count": len(distinct_platforms),
        "platforms": [p.value for p in distinct_platforms],
        "weight": cfg.weight_social,
        "points": social_score,
    }
    total_score += social_score

    # E. Nicho / Categoría
    norm_cat = normalize_text(category)
    multiplier = 0.5  # base si no se conoce el nicho
    for key, val in niches.items():
        if key in norm_cat:
            multiplier = max(multiplier, val)
    niche_score = cfg.weight_niche * multiplier
    breakdown["niche"] = {
        "category": category,
        "multiplier": multiplier,
        "weight": cfg.weight_niche,
        "points": niche_score,
    }
    total_score += niche_score

    # F. Canal de contacto público directo
    channel_score = 0.0
    has_direct_phone = bool(phone and phone.strip())
    has_whatsapp = any(
        s.platform.value in ("WHATSAPP_COMERCIAL", "WHATSAPP") and s.status == SocialVerificationStatus.CONFIRMADO
        for s in vigente_socials.values()
    )
    if has_whatsapp:
        channel_score = cfg.weight_public_channel * 1.0
    else:
        missing_data.append("phone_or_whatsapp")
    breakdown["public_channel"] = {
        "phone_present": has_direct_phone,
        "whatsapp_present": has_whatsapp,
        "weight": cfg.weight_public_channel,
        "points": channel_score,
    }
    total_score += channel_score

    # Regla de contacto en Etapa 2:
    # ready_for_contact permanece estrictamente False porque la verificación de decisor,
    # canal de contacto verificado y aprobación humana son requisitos de etapas posteriores (Etapa 3+).
    # preliminary_commercial_fit indica si el perfil comercial es preliminarmente favorable.
    preliminary_commercial_fit = bool(
        eligible and not is_revision_identity and web_is_opportunity and not is_closed
    )
    ready_for_contact = False

    breakdown["preliminary_commercial_fit"] = preliminary_commercial_fit
    breakdown["ready_for_contact"] = ready_for_contact

    final_score = round(total_score, 2)

    return RankingResult(
        score=final_score,
        eligible=eligible,
        ready_for_contact=ready_for_contact,
        rule_version=cfg.rule_version,
        breakdown=breakdown,
        reasons=reasons,
        missing_data=missing_data,
        preliminary_commercial_fit=preliminary_commercial_fit,
    )

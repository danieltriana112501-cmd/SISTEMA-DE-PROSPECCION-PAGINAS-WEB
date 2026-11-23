"""Módulo de verificación web manual y candidatos de redes sociales (RF-03, RF-04).

Reglas de negocio:
- Seis estados de contrato para sitio web:
  OFICIAL_ENCONTRADA, NO_ENCONTRADA, SOLO_REDES_SOCIALES, SOLO_AGREGADOR,
  OFICIAL_NO_DISPONIBLE, PENDIENTE_VERIFICACION.
- URL vacía en la fuente inicial NO demuestra ausencia de web.
- OFICIAL_NO_DISPONIBLE no infiere ausencia; requiere revisión humana.
- Bloqueos, timeouts, consultas parciales o contradicciones mantienen PENDIENTE_VERIFICACION.
- Un sitio oficial en un constructor (Wix, Shopify, etc.) cuenta como OFICIAL_ENCONTRADA.
- Redes sociales: candidatos no se confirman solo por coincidencia de nombre (homónimos).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .models import normalize_identity, normalize_text, utc_now_iso


class WebsiteStatus(str, enum.Enum):
    OFICIAL_ENCONTRADA = "OFICIAL_ENCONTRADA"
    NO_ENCONTRADA = "NO_ENCONTRADA"
    SOLO_REDES_SOCIALES = "SOLO_REDES_SOCIALES"
    SOLO_AGREGADOR = "SOLO_AGREGADOR"
    OFICIAL_NO_DISPONIBLE = "OFICIAL_NO_DISPONIBLE"
    PENDIENTE_VERIFICACION = "PENDIENTE_VERIFICACION"


class ConfidenceLevel(str, enum.Enum):
    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


class WebCandidateType(str, enum.Enum):
    OFICIAL = "OFICIAL"
    RED_SOCIAL = "RED_SOCIAL"
    AGREGADOR = "AGREGADOR"
    DIRECTORIO = "DIRECTORIO"
    OTRO = "OTRO"


class IdentityMatch(str, enum.Enum):
    COINCIDE = "COINCIDE"
    HOMONIMO = "HOMONIMO"
    INCIERTA = "INCIERTA"


class QueryOutcomeStatus(str, enum.Enum):
    COMPLETA = "COMPLETA"
    PARCIAL = "PARCIAL"
    BLOQUEADA = "BLOQUEADA"
    ERROR = "ERROR"


class SocialPlatform(str, enum.Enum):
    INSTAGRAM = "INSTAGRAM"
    FACEBOOK = "FACEBOOK"
    TIKTOK = "TIKTOK"
    LINKEDIN = "LINKEDIN"
    WHATSAPP_COMERCIAL = "WHATSAPP_COMERCIAL"
    OTRA = "OTRA"


class SocialVerificationStatus(str, enum.Enum):
    CANDIDATO = "CANDIDATO"
    CONFIRMADO = "CONFIRMADO"
    DESCARTADO = "DESCARTADO"


BUILDER_DOMAINS = frozenset(
    {
        "wixsite.com",
        "myshopify.com",
        "squarespace.com",
        "wordpress.com",
        "webnode.es",
        "site123.me",
        "weebly.com",
        "jimdosite.com",
        "tiendanube.com",
    }
)

SOCIAL_DOMAINS = {
    "instagram.com": SocialPlatform.INSTAGRAM,
    "facebook.com": SocialPlatform.FACEBOOK,
    "fb.com": SocialPlatform.FACEBOOK,
    "tiktok.com": SocialPlatform.TIKTOK,
    "linkedin.com": SocialPlatform.LINKEDIN,
    "wa.me": SocialPlatform.WHATSAPP_COMERCIAL,
    "whatsapp.com": SocialPlatform.WHATSAPP_COMERCIAL,
}

AGGREGATOR_DOMAINS = frozenset(
    {
        "tripadvisor.com",
        "tripadvisor.co",
        "yelp.com",
        "paginasamarillas.com",
        "paginasamarillas.com.co",
        "foursquare.com",
        "degusta.com.co",
        "guiastur.com",
    }
)


def classify_url_domain(url: str) -> tuple[WebCandidateType, str]:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except Exception:
        return WebCandidateType.OTRO, ""

    for builder in BUILDER_DOMAINS:
        if hostname == builder or hostname.endswith("." + builder):
            return WebCandidateType.OFICIAL, f"Constructor de sitios web ({builder})"

    for s_domain in SOCIAL_DOMAINS:
        if hostname == s_domain or hostname.endswith("." + s_domain):
            return WebCandidateType.RED_SOCIAL, f"Red social ({s_domain})"

    for agg in AGGREGATOR_DOMAINS:
        if hostname == agg or hostname.endswith("." + agg):
            return WebCandidateType.AGREGADOR, f"Agregador o directorio ({agg})"

    return WebCandidateType.OFICIAL, "Dominio propio independiente"


def is_valid_web_url(url: str | None) -> bool:
    """Valida si un string es una URL web sintácticamente válida con esquema http/https."""
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    try:
        parsed = urlparse(clean)
        parsed.port  # Parsing the port also rejects invalid port syntax and ranges.
        return (
            parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not any(char.isspace() or ord(char) < 32 for char in clean)
        )
    except ValueError:
        return False


def is_sufficient_query(query: WebQuery) -> bool:
    """Determina si una consulta cumple con el criterio de búsqueda suficiente y documentada."""
    if not query.consulta or not query.consulta.strip():
        return False
    if not query.evidence_ids:
        return False
    if query.resultado != QueryOutcomeStatus.COMPLETA:
        return False
    return True


@dataclass(frozen=True)
class WebQuery:
    consulta: str
    resultado: QueryOutcomeStatus
    evidence_ids: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class WebCandidate:
    url: str
    tipo: WebCandidateType
    identidad: IdentityMatch
    evidence_ids: list[str] = field(default_factory=list)
    access_error: str = ""


@dataclass(frozen=True)
class SocialCandidate:
    platform: SocialPlatform
    url: str
    status: SocialVerificationStatus
    confidence: ConfidenceLevel
    signals: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    reviewed_at: str = ""
    reviewer: str = ""


@dataclass(frozen=True)
class WebsiteReview:
    status: WebsiteStatus
    confidence: ConfidenceLevel
    official_url: str | None
    reviewer: str
    reviewed_at: str
    queries: list[WebQuery] = field(default_factory=list)
    candidates: list[WebCandidate] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    notes: str = ""
    reasons: list[str] = field(default_factory=list)


def evaluate_website_review(
    queries: list[WebQuery],
    candidates: list[WebCandidate],
    social_profiles: list[SocialCandidate] | None = None,
) -> tuple[WebsiteStatus, ConfidenceLevel, str | None, list[str]]:
    """Evalúa la precedencia y reglas de confianza documentadas en 05_MODELO_DE_DATOS.md.

    Precedencia:
    1. Sitio oficial comprobado (OFICIAL_ENCONTRADA), incluso en constructor.
       Una URL vacía o inválida no puede sustentar OFICIAL_ENCONTRADA (queda PENDIENTE_VERIFICACION).
    2. Sitio oficial inaccesible (OFICIAL_NO_DISPONIBLE).
    3. Pendiente por búsqueda bloqueada/incompleta o evidencia contradictoria o insuficiente (PENDIENTE_VERIFICACION).
    4. Sin sitio tras búsqueda suficiente, con red social confirmada (SOLO_REDES_SOCIALES).
       Si además hay agregador, se conserva como evidencia secundaria.
    5. Sin sitio tras búsqueda suficiente, solo agregador (SOLO_AGREGADOR).
    6. Sin sitio tras búsqueda suficiente documentada, sin redes ni agregador (NO_ENCONTRADA).
    """
    reasons: list[str] = []

    # 1. Revisar si hay candidatos a sitio oficial (accesible o inaccesible)
    official_accessible: list[WebCandidate] = []
    official_inaccessible: list[WebCandidate] = []

    for c in candidates:
        if c.tipo == WebCandidateType.OFICIAL and c.identidad == IdentityMatch.COINCIDE:
            if not is_valid_web_url(c.url):
                reasons.append(
                    f"Candidato a sitio oficial con URL vacía o inválida ('{c.url}'). "
                    "No puede sustentar OFICIAL_ENCONTRADA; estado pendiente de verificación."
                )
                return (
                    WebsiteStatus.PENDIENTE_VERIFICACION,
                    ConfidenceLevel.BAJA,
                    None,
                    reasons,
                )
            if c.access_error:
                official_inaccessible.append(c)
            else:
                official_accessible.append(c)

    if official_accessible:
        cand = official_accessible[0]
        _, desc = classify_url_domain(cand.url)
        reasons.append(f"Sitio oficial comprobado: {cand.url} ({desc})")
        return (
            WebsiteStatus.OFICIAL_ENCONTRADA,
            ConfidenceLevel.ALTA,
            cand.url,
            reasons,
        )

    if official_inaccessible:
        cand = official_inaccessible[0]
        reasons.append(
            f"Sitio oficial identificado pero inaccesible ({cand.access_error}): {cand.url}. "
            "No se infiere ausencia; se remite a revisión."
        )
        return (
            WebsiteStatus.OFICIAL_NO_DISPONIBLE,
            ConfidenceLevel.ALTA,
            cand.url,
            reasons,
        )

    # 2. Revisar si las consultas tuvieron fallos o bloqueos
    blocked_or_partial_queries = [
        q for q in queries if q.resultado in (QueryOutcomeStatus.BLOQUEADA, QueryOutcomeStatus.ERROR, QueryOutcomeStatus.PARCIAL)
    ]
    if blocked_or_partial_queries:
        reasons.append(
            f"Búsqueda bloqueada o incompleta en {len(blocked_or_partial_queries)} consulta(s). "
            "Evidencia insuficiente para dictaminar ausencia."
        )
        return (
            WebsiteStatus.PENDIENTE_VERIFICACION,
            ConfidenceLevel.BAJA,
            None,
            reasons,
        )

    if not queries:
        reasons.append("Sin consultas de búsqueda documentadas; estado pendiente de verificación.")
        return (
            WebsiteStatus.PENDIENTE_VERIFICACION,
            ConfidenceLevel.BAJA,
            None,
            reasons,
        )

    # Validar que exista al menos una consulta suficiente (no vacía, con evidencias, COMPLETA)
    sufficient_queries = [q for q in queries if is_sufficient_query(q)]
    if not sufficient_queries:
        reasons.append(
            "Búsqueda insuficiente o indocumentada (consultas con texto vacío, sin evidencias o no completadas). "
            "No permite dictaminar ausencia de sitio oficial ni presencia social/agregador."
        )
        return (
            WebsiteStatus.PENDIENTE_VERIFICACION,
            ConfidenceLevel.BAJA,
            None,
            reasons,
        )

    # Revisar contradicciones de identidad
    ambiguous_candidates = [
        c for c in candidates if c.identidad in (IdentityMatch.INCIERTA, IdentityMatch.HOMONIMO) and c.tipo == WebCandidateType.OFICIAL
    ]
    if ambiguous_candidates:
        reasons.append("Candidatos a sitio web con identidad incierta o homónimos no resueltos.")
        return (
            WebsiteStatus.PENDIENTE_VERIFICACION,
            ConfidenceLevel.BAJA,
            None,
            reasons,
        )

    # 3. Revisar perfiles de redes sociales confirmados
    confirmed_socials = [
        s for s in (social_profiles or []) if s.status == SocialVerificationStatus.CONFIRMADO
    ]
    # También revisar candidatos de tipo RED_SOCIAL coincidentes
    social_candidates_coincide = [
        c for c in candidates if c.tipo == WebCandidateType.RED_SOCIAL and c.identidad == IdentityMatch.COINCIDE
    ]

    has_confirmed_social = bool(confirmed_socials or social_candidates_coincide)

    # 4. Revisar agregadores
    aggregator_candidates = [
        c for c in candidates if c.tipo in (WebCandidateType.AGREGADOR, WebCandidateType.DIRECTORIO) and c.identidad == IdentityMatch.COINCIDE
    ]
    has_aggregator = bool(aggregator_candidates)

    if has_confirmed_social:
        reasons.append("Sin sitio web oficial tras búsqueda suficiente; perfil en red social confirmado.")
        if has_aggregator:
            reasons.append(
                f"Presencia en agregador detectada ({aggregator_candidates[0].url}) "
                "y conservada como evidencia secundaria."
            )
        return (
            WebsiteStatus.SOLO_REDES_SOCIALES,
            ConfidenceLevel.ALTA,
            None,
            reasons,
        )

    if has_aggregator:
        reasons.append(
            f"Sin sitio web oficial ni red social confirmada tras búsqueda suficiente; "
            f"solo agregador comprobado ({aggregator_candidates[0].url})."
        )
        return (
            WebsiteStatus.SOLO_AGREGADOR,
            ConfidenceLevel.ALTA,
            None,
            reasons,
        )

    # 5. Búsqueda suficiente completa sin ningún candidato aplicable
    reasons.append(
        "Búsqueda suficiente documentada sin sitio web oficial, perfiles de redes sociales ni agregadores aplicables. "
        "No demuestra inexistencia absoluta pero cumple el criterio de ausencia observable."
    )
    return (
        WebsiteStatus.NO_ENCONTRADA,
        ConfidenceLevel.MEDIA,
        None,
        reasons,
    )


def evaluate_social_candidate(
    business_name: str,
    business_city: str | None,
    business_phone: str | None,
    candidate_url: str,
    profile_name: str | None = None,
    profile_bio: str | None = None,
    profile_city: str | None = None,
    profile_phone: str | None = None,
    business_address: str | None = None,
    profile_address: str | None = None,
    official_linked: bool = False,
    signals: str | None = None,
) -> tuple[SocialVerificationStatus, ConfidenceLevel, str]:
    """Evalúa un candidato de red social siguiendo las reglas de corroboración de 05_MODELO_DE_DATOS.md.

    Reglas:
    - Confirmación exige corroboración fuerte: enlace desde fuente oficial verificada O
      nombre/sede coincidentes MÁS teléfono verificado o dirección verificada.
    - Nombre parcial o ciudad solos NO confirman (permanecen como CANDIDATO).
    - No confirmar puramente por teléfono si está asociado a un nombre de negocio diferente o señales contradictorias.
    - Casos ambiguos permanecen como CANDIDATO.
    - DESCARTADO se reserva para contradicción explícita (ciudad diferente, teléfono discordante, nombre incompatible).
    """
    norm_bname = normalize_text(business_name)
    norm_pname = normalize_text(profile_name or "")
    norm_bcity = normalize_text(business_city or "")
    norm_pcity = normalize_text(profile_city or "")
    norm_bio = normalize_text(profile_bio or "")
    norm_baddr = normalize_text(business_address or "")
    norm_paddr = normalize_text(profile_address or "")

    def _digits(val: str | None) -> str:
        return "".join(c for c in (val or "") if c.isdigit())

    bphone_digits = _digits(business_phone)
    pphone_digits = _digits(profile_phone)

    # 1. Contradicción explícita -> DESCARTADO
    if norm_bcity and norm_pcity and norm_bcity != norm_pcity:
        return (
            SocialVerificationStatus.DESCARTADO,
            ConfidenceLevel.ALTA,
            f"Descartado por discrepancia de ciudad ('{profile_city}' != '{business_city}').",
        )

    if bphone_digits and pphone_digits:
        if len(bphone_digits) >= 7 and len(pphone_digits) >= 7 and bphone_digits[-7:] != pphone_digits[-7:]:
            return (
                SocialVerificationStatus.DESCARTADO,
                ConfidenceLevel.ALTA,
                "Descartado por discrepancia de número telefónico.",
            )

    name_matches = bool(
        norm_bname and norm_pname and (norm_bname in norm_pname or norm_pname in norm_bname)
    )

    if norm_pname and not name_matches:
        return (
            SocialVerificationStatus.DESCARTADO,
            ConfidenceLevel.ALTA,
            f"Descartado por discrepancia en el nombre del negocio ('{profile_name}' incompatible con '{business_name}').",
        )

    # 2. Corroboración fuerte -> CONFIRMADO
    if official_linked:
        return (
            SocialVerificationStatus.CONFIRMADO,
            ConfidenceLevel.ALTA,
            "Confirmado por enlace directo desde fuente oficial verificada.",
        )

    phone_matches = False
    if bphone_digits and len(bphone_digits) >= 7:
        if pphone_digits and bphone_digits[-7:] == pphone_digits[-7:]:
            phone_matches = True
        elif bphone_digits[-7:] in _digits(norm_bio):
            phone_matches = True

    address_matches = False
    if norm_baddr and len(norm_baddr) >= 5:
        if norm_paddr and (norm_baddr in norm_paddr or norm_paddr in norm_baddr):
            address_matches = True
        elif norm_baddr in norm_bio:
            address_matches = True

    if name_matches and (phone_matches or address_matches):
        return (
            SocialVerificationStatus.CONFIRMADO,
            ConfidenceLevel.ALTA,
            "Confirmado por corroboración fuerte (nombre concordante más teléfono o dirección verificada).",
        )

    # 3. Coincidencia parcial o débil (nombre solo, nombre+ciudad sin teléfono/dirección) -> CANDIDATO
    if name_matches:
        return (
            SocialVerificationStatus.CANDIDATO,
            ConfidenceLevel.BAJA,
            "Candidato homónimo sin corroboración fuerte (falta teléfono, dirección o enlace oficial concordante).",
        )

    return (
        SocialVerificationStatus.CANDIDATO,
        ConfidenceLevel.BAJA,
        "Candidato sin corroboración concluyente.",
    )

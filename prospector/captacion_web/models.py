"""Modelos y reglas puras de la primera vertical de Captacion Web.

No importa nada del modo SG-SST. Los valores ausentes se conservan como
`None`; no se imputan rating, resenas, estado ni identidad externa.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_COMPLETA = "COMPLETA"
RUN_PARCIAL = "PARCIAL"
RUN_FALLIDA = "FALLIDA"

ATTEMPT_COMPLETA = "COMPLETA"
ATTEMPT_PARCIAL = "PARCIAL"
ATTEMPT_FALLIDA = "FALLIDA"

REPUTATION_CUMPLE = "CUMPLE"
REPUTATION_NO_CUMPLE = "NO_CUMPLE"
REPUTATION_REVISION = "REVISION"

WEBSITE_PENDIENTE = "PENDIENTE_VERIFICACION"

EXTERNAL_ID_PROVIDER = "PROVEEDOR"
EXTERNAL_ID_LOCAL = "LOCAL"
EXTERNAL_ID_REVISION = "REVISION"

# Lista explicita de campos autorizados. Todo lo que no este aqui se descarta
# antes de tocar SQLite, los reportes o la salida de consola.
PERSISTED_FIELDS = (
    "external_id",
    "name",
    "category",
    "country",
    "city",
    "address",
    "phone",
    "website_url",
    "rating",
    "review_count",
    "business_status",
    "observed_at",
)

# Campos que distinguen dos registros cuando no hay identidad suficiente.
# `observed_at` queda fuera a proposito para que repetir la misma entrada no
# invente una identidad nueva solo porque cambio la hora de la corrida.
IDENTITY_FINGERPRINT_FIELDS = (
    "name",
    "category",
    "country",
    "city",
    "address",
    "phone",
    "website_url",
    "rating",
    "review_count",
    "business_status",
)


class ProviderError(Exception):
    """La fuente local no pudo leerse o no tiene el contrato esperado."""


class RecordError(Exception):
    """Un registro sintetico no cumple el contrato minimo."""


class DatabaseRejectedError(Exception):
    """La ruta de SQLite pertenece al sistema SG-SST o a una base ajena."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    """Minusculas, sin tildes y con espacios colapsados para comparar filtros."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def normalize_identity(value: Any) -> str:
    """Minusculas, espacios colapsados y forma canonica Unicode (NFC) para identidad.

    A diferencia de `normalize_text` (usada en busqueda y filtros), NO elimina
    signos diacriticos ni caracteres como la 'ñ', para que negocios como
    'El Niño' y 'El Nino' mantengan identidades distintas.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class SearchFilters:
    country: str
    city: str
    category: str
    keywords: tuple[str, ...] = ()
    location: str = ""
    min_rating: float = 4.5
    min_reviews: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "country", (self.country or "").strip())
        object.__setattr__(self, "city", (self.city or "").strip())
        object.__setattr__(self, "category", (self.category or "").strip())
        object.__setattr__(self, "location", (self.location or "").strip())
        raw_keywords = (self.keywords,) if isinstance(self.keywords, str) else (self.keywords or ())
        keywords = tuple(
            dict.fromkeys(
                keyword.strip() for keyword in raw_keywords if keyword and keyword.strip()
            )
        )
        object.__setattr__(self, "keywords", keywords)

        if not self.country:
            raise ValueError("country es obligatorio")
        if not self.city:
            raise ValueError("city es obligatoria")
        if not self.category:
            raise ValueError("category es obligatoria")
        if isinstance(self.min_rating, bool) or not isinstance(self.min_rating, (int, float)):
            raise ValueError("min_rating debe ser numerico")
        rating = float(self.min_rating)
        if not math.isfinite(rating) or not 1 <= rating <= 5:
            raise ValueError("min_rating debe estar entre 1 y 5")
        object.__setattr__(self, "min_rating", rating)
        if isinstance(self.min_reviews, bool) or not isinstance(self.min_reviews, int):
            raise ValueError("min_reviews debe ser entero")
        if self.min_reviews < 0:
            raise ValueError("min_reviews no puede ser negativo")

    def queries(self) -> list[str]:
        base = f"{self.category} en {self.city}, {self.country}"
        keyword_queries = [f"{keyword} en {self.city}, {self.country}" for keyword in self.keywords]
        return list(dict.fromkeys([base, *keyword_queries]))

    def as_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "city": self.city,
            "category": self.category,
            "keywords": list(self.keywords),
            "location": self.location,
            "min_rating": self.min_rating,
            "min_reviews": self.min_reviews,
        }


# Version explicita de la huella local. Cada cambio de formato sube la version:
# v1 (etapa 1) no incluia el pais; v2 usaba `|` como separador y colisionaba
# cuando un componente contenia ese caracter; v3 serializaba como JSON canonico
# pero eliminaba diacriticos (mezclando 'El Niño' y 'El Nino'); v4 usa
# `normalize_identity` con NFC, conservando la 'ñ' y caracteres distintivos.
# Las claves anteriores no se migran ni se reutilizan: se conservan intactas
# y la corrida nueva crea un prospecto distinto.
LOCAL_IDENTITY_VERSION = "v4"


def _local_identity_digest(*parts: str) -> str:
    """Huella inequivoca de componentes normalizados para identidad.

    Usa `normalize_identity` para conservar caracteres distintivos (como 'ñ')
    y forma canonica Unicode NFC, colapsando espacios y pasando a minusculas.
    Serializa con JSON canonico para preservar los limites entre campos.
    """
    encoded = json.dumps(
        [normalize_identity(part) for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def build_identity(
    source: str,
    external_id: str | None,
    name: str,
    address: str | None,
    city: str | None,
    country: str | None,
    revision_key: str | None = None,
) -> tuple[str, str]:
    """Identidad estable por fuente.

    Con ID del proveedor se usa ese ID. Sin ID se exige nombre + direccion +
    ciudad + pais para formar una huella local serializada de forma canonica,
    porque dos sedes con los mismos valores en paises distintos no son la misma
    sede y un `|` dentro de un campo no debe mezclar componentes. Si falta la
    direccion o el pais, la evidencia no permite concluir que dos registros sean
    la misma sede: se usa una clave de revision unica y se marca `REVISION`.
    Telefono y dominio NUNCA forman parte de la identidad, asi que dos sedes que
    los comparten siguen siendo distintas.
    """
    if external_id and external_id.strip():
        return EXTERNAL_ID_PROVIDER, external_id.strip()
    if normalize_identity(address) and normalize_identity(city) and normalize_identity(country):
        digest = _local_identity_digest(source, name, address, city, country)
        return EXTERNAL_ID_LOCAL, f"local:{LOCAL_IDENTITY_VERSION}:{digest}"
    if revision_key and revision_key.strip():
        return EXTERNAL_ID_REVISION, revision_key.strip()
    raise ValueError("identidad insuficiente: falta direccion o pais y no hay clave de revision")


def identity_fingerprint(values: dict[str, Any]) -> str:
    """Huella determinista de los campos observados que distinguen registros.

    Solo usa la lista autorizada; nunca incluye campos extra del JSON de
    entrada como contrasenas, tokens o datos personales.
    """
    payload = {name: values.get(name) for name in IDENTITY_FINGERPRINT_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reputation_state(rating: float | None, review_count: int | None, min_rating: float, min_reviews: int) -> str:
    """Umbrales inclusivos; cualquier dato ausente queda en revision."""
    if rating is None or review_count is None:
        return REPUTATION_REVISION
    if rating >= min_rating and review_count >= min_reviews:
        return REPUTATION_CUMPLE
    return REPUTATION_NO_CUMPLE


@dataclass(frozen=True)
class ObservedBusiness:
    source: str
    external_id: str
    external_id_kind: str
    identity_key: str
    name: str
    category: str | None
    country: str | None
    city: str | None
    address: str | None
    phone: str | None
    website_url: str | None
    rating: float | None
    review_count: int | None
    business_status: str
    observed_at: str
    persisted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryOutcome:
    query: str
    status: str
    records: list[ObservedBusiness]
    records_seen: int
    records_invalid: int
    records_filtered_out: int
    error: str = ""


@dataclass(frozen=True)
class SearchOutcome:
    source: str
    outcomes: list[QueryOutcome]


@dataclass
class RunReport:
    run_id: str
    status: str
    source: str
    filters: SearchFilters
    counts: dict[str, int]
    errors: list[str]
    queries: list[dict[str, Any]]
    prospects: list[dict[str, Any]]
    report_path: Path | None

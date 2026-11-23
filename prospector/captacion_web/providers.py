"""Fuente local de Captacion Web.

Solo lee un JSON sintetico o una importacion local autorizada. No hay cliente
HTTP, scraper, Maps, redes sociales ni IA en este modulo. El contrato del
archivo se documenta en el README del paquete.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import (
    ATTEMPT_COMPLETA,
    ATTEMPT_FALLIDA,
    ATTEMPT_PARCIAL,
    ObservedBusiness,
    ProviderError,
    QueryOutcome,
    RecordError,
    SearchFilters,
    SearchOutcome,
    build_identity,
    identity_fingerprint,
    normalize_identity,
    normalize_text,
    utc_now_iso,
)

_BUSINESS_STATUS = {
    "ACTIVO": "ACTIVO",
    "CERRADO": "CERRADO",
    "DESCONOCIDO": "DESCONOCIDO",
    "UNKNOWN": "DESCONOCIDO",
}

# `fail_queries` viene del archivo de entrada y puede contener cualquier texto
# (tokens, rutas, datos personales). Nunca se copia a SQLite, al reporte ni a la
# consola: solo se traduce a uno de estos codigos acotados. El valor por defecto
# conserva la identificacion de la consulta fallida sin revelar su contenido.
FAILURE_CODES = {
    "TIMEOUT": "TIMEOUT",
    "RATE_LIMIT": "RATE_LIMIT",
    "AUTH": "AUTH",
    "NETWORK": "NETWORK",
    "UNAVAILABLE": "UNAVAILABLE",
}
DEFAULT_FAILURE_CODE = "QUERY_FAILED"


def safe_failure_code(value: Any) -> str:
    """Traduce el valor de `fail_queries` a un codigo seguro y documentado."""
    token = normalize_text(value).upper().replace(" ", "_")
    return FAILURE_CODES.get(token, DEFAULT_FAILURE_CODE)


class FixtureProvider:
    """Proveedor determinista sobre un archivo JSON local."""

    name = "fixture-local"

    def __init__(self, path: Path, source: str | None = None) -> None:
        self.path = Path(path)
        self._source_override = source.strip() if source and source.strip() else None

    @property
    def source(self) -> str:
        if self._source_override:
            return self._source_override
        data = self._load()
        declared = data.get("source")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
        return self.path.stem

    def search(self, filters: SearchFilters) -> SearchOutcome:
        data = self._load()
        source = self._source_override
        if not source:
            declared = data.get("source")
            source = declared.strip() if isinstance(declared, str) and declared.strip() else self.path.stem

        raw_records = data["records"]
        fail_queries = data.get("fail_queries") or {}
        outcomes: list[QueryOutcome] = []

        for query in filters.queries():
            if query in fail_queries:
                # El valor de la entrada se traduce a un codigo; nunca se copia.
                outcomes.append(
                    QueryOutcome(
                        query=query,
                        status=ATTEMPT_FALLIDA,
                        records=[],
                        records_seen=0,
                        records_invalid=0,
                        records_filtered_out=0,
                        error=safe_failure_code(fail_queries[query]),
                    )
                )
                continue

            records, seen, invalid, filtered, errors = _select_records(raw_records, filters, source)
            status = ATTEMPT_PARCIAL if invalid else ATTEMPT_COMPLETA
            outcomes.append(
                QueryOutcome(
                    query=query,
                    status=status,
                    records=records,
                    records_seen=seen,
                    records_invalid=invalid,
                    records_filtered_out=filtered,
                    error="; ".join(errors),
                )
            )

        return SearchOutcome(source=source, outcomes=outcomes)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise ProviderError(f"archivo de entrada no existe: {self.path.name}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"no se pudo leer el JSON local: {exc}") from exc

        if not isinstance(data, dict):
            raise ProviderError("el archivo debe contener un objeto JSON")
        if not isinstance(data.get("records"), list):
            raise ProviderError("el archivo debe incluir una lista 'records'")
        fail_queries = data.get("fail_queries")
        if fail_queries is not None and not isinstance(fail_queries, dict):
            raise ProviderError("'fail_queries' debe ser un objeto JSON")
        return data


def _select_records(
    raw_records: list[Any],
    filters: SearchFilters,
    source: str,
) -> tuple[list[ObservedBusiness], int, int, int, list[str]]:
    """Valida cada registro antes de aplicar filtros.

    Un registro incompleto o con tipos invalidos se cuenta como invalido y se
    reporta, aunque su ciudad o categoria no coincida con la busqueda. Asi un
    dato roto nunca se disfraza de "resultado filtrado".
    """
    records: list[ObservedBusiness] = []
    seen = invalid = filtered = 0
    errors: list[str] = []
    revision_ordinals: dict[str, int] = {}

    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            invalid += 1
            errors.append(f"registro {index}: no es un objeto JSON")
            continue
        try:
            record = _parse_record(raw, source, revision_ordinals)
        except RecordError as exc:
            invalid += 1
            errors.append(f"registro {index}: {exc}")
            continue
        seen += 1
        if _matches(record, filters):
            records.append(record)
        else:
            filtered += 1

    return records, seen, invalid, filtered, errors


def _matches(record: ObservedBusiness, filters: SearchFilters) -> bool:
    if normalize_text(record.country) != normalize_text(filters.country):
        return False
    if normalize_text(record.city) != normalize_text(filters.city):
        return False
    if normalize_text(record.category) != normalize_text(filters.category):
        return False

    if filters.keywords:
        haystack = " ".join(
            normalize_text(value) for value in (record.name, record.category, record.address)
        )
        if not any(normalize_text(keyword) in haystack for keyword in filters.keywords):
            return False

    if filters.location:
        if normalize_text(filters.location) not in normalize_text(record.address):
            return False

    return True


def _parse_record(
    raw: dict[str, Any],
    source: str,
    revision_ordinals: dict[str, int],
) -> ObservedBusiness:
    name = _text(raw.get("name"), "name", required=True)
    category = _text(raw.get("category"), "category", required=True)
    country = _text(raw.get("country"), "country", required=True)
    city = _text(raw.get("city"), "city", required=True)
    address = _text(raw.get("address"), "address")
    phone = _text(raw.get("phone"), "phone")
    external_id = _text(raw.get("external_id"), "external_id")
    website_url = _url(raw.get("website_url"))
    rating = _rating(raw.get("rating"))
    review_count = _review_count(raw.get("review_count"))
    business_status = _business_status(raw.get("business_status"))
    observed_at = _observed_at(raw.get("observed_at"))

    observed = {
        "name": name,
        "category": category,
        "country": country,
        "city": city,
        "address": address,
        "phone": phone,
        "website_url": website_url,
        "rating": rating,
        "review_count": review_count,
        "business_status": business_status,
    }
    revision_key = None
    if not (external_id and external_id.strip()) and not (normalize_identity(address) and normalize_identity(city)):
        fingerprint = identity_fingerprint(observed)
        ordinal = revision_ordinals.get(fingerprint, 0)
        revision_ordinals[fingerprint] = ordinal + 1
        revision_key = f"revision:{fingerprint[:24]}:{ordinal}"

    kind, key = build_identity(source, external_id, name, address, city, country, revision_key=revision_key)

    # Solo se persiste la lista autorizada; cualquier campo extra del JSON de
    # entrada (password, token, email, notas, etc.) se descarta aqui.
    persisted = {
        "external_id": key,
        "name": name,
        "category": category,
        "country": country,
        "city": city,
        "address": address,
        "phone": phone,
        "website_url": website_url,
        "rating": rating,
        "review_count": review_count,
        "business_status": business_status,
        "observed_at": observed_at,
    }

    return ObservedBusiness(
        source=source,
        external_id=key,
        external_id_kind=kind,
        identity_key=key,
        name=name,
        category=category,
        country=country,
        city=city,
        address=address,
        phone=phone,
        website_url=website_url,
        rating=rating,
        review_count=review_count,
        business_status=business_status,
        observed_at=observed_at,
        persisted=persisted,
    )


def _text(value: Any, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise RecordError(f"{field} es obligatorio")
        return None
    if not isinstance(value, str):
        raise RecordError(f"{field} debe ser texto o null")
    text = value.strip()
    if not text:
        if required:
            raise RecordError(f"{field} no puede estar vacio")
        return None
    return text


def _rating(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError("rating debe ser numero o null")
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 5:
        raise RecordError("rating debe estar entre 1 y 5")
    return number


def _review_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RecordError("review_count debe ser entero o null")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise RecordError("review_count debe ser entero o null")
    if number < 0:
        raise RecordError("review_count no puede ser negativo")
    return number


def _business_status(value: Any) -> str:
    if value is None:
        return "DESCONOCIDO"
    if not isinstance(value, str):
        raise RecordError("business_status debe ser texto")
    token = normalize_text(value).upper()
    if token not in _BUSINESS_STATUS:
        # No se repite el valor recibido: un campo mal etiquetado no debe
        # terminar copiado en errores o logs.
        raise RecordError("business_status no reconocido")
    return _BUSINESS_STATUS[token]


def _url(value: Any) -> str | None:
    text = _text(value, "website_url")
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RecordError("website_url debe ser http(s) o null")
    return text


def _observed_at(value: Any) -> str:
    if value is None:
        return utc_now_iso()
    if not isinstance(value, str) or not value.strip():
        raise RecordError("observed_at debe ser fecha ISO-8601 o null")
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError("observed_at debe ser fecha ISO-8601") from exc
    return text

# Captación Web · primera vertical aislada

Módulo **opt-in** para probar la captación de oportunidades web con datos
sintéticos o una importación local autorizada. No consulta Maps, redes
sociales, IA ni ningún servicio externo, y no toca `data/leads.db` ni el CLI
SG-SST.

## Ejecutar con el fixture sintético

Desde `SISTEMA DE PROPECCION/`:

```powershell
.\.venv\Scripts\python.exe -m prospector.captacion_web `
  --input tests\captacion_web\fixtures\synthetic_businesses.json `
  --db "$env:TEMP\captacion_web_demo.db" `
  --output-dir "$env:TEMP\captacion_web_demo\runs" `
  --country Colombia `
  --city Bogota `
  --category Restaurante `
  --keywords "sabor" `
  --location "Chapinero" `
  --min-rating 4.5 `
  --min-reviews 30
```

Sin `--db` y `--output-dir`, la corrida escribe en
`data/captacion_web/captacion_web.db` y `data/captacion_web/runs/`. Esas rutas
son distintas de las del modo SG-SST y `data/` está ignorado por Git.

## Códigos de salida

| Código | Estado | Significado |
|---|---|---|
| `0` | `COMPLETA` | Todas las consultas se ejecutaron y ningún registro válido falló. |
| `3` | `PARCIAL` | Al menos una consulta falló o algún registro no cumplió el contrato; se conservaron los registros válidos. |
| `1` | `FALLIDA` | La fuente no se pudo leer, no devolvió consultas o falló por completo. No es una búsqueda exitosa sin resultados. |
| `4` | — | Base rechazada por seguridad: es la base SG-SST o una base ajena. No se escribió nada. |
| `2` | — | Error de uso del CLI (argparse). |

Un resultado vacío con consultas completas es `COMPLETA` con
`prospects_matched = 0`; no se confunde con un fallo.

## Protección de la base SG-SST

Antes de crear o abrir una base en modo escritura, `--db` se valida:

- Se comparan rutas canónicas, mayúsculas/minúsculas, alias con `..` y
  archivos que apuntan al mismo inodo que `prospector.config.DB_PATH`.
- Si el archivo ya existe, se inspecciona **en solo lectura**. Se rechaza si
  contiene tablas del sistema SG-SST (`leads`, `zonas_completadas`,
  `raw_maps_cache`, `corridas`) o tablas ajenas a esta vertical.
- Una base inexistente, un archivo vacío o una base que solo tiene las cuatro
  tablas propias (`search_run`, `query_attempt`, `prospect`, `observation`)
  se acepta.
- El rechazo ocurre antes de `mkdir`, de conectar en escritura y de escribir
  cualquier reporte. El CLI responde con salida `4` y un mensaje `ERROR: base
  rechazada: ...`.

## Contrato del JSON local

```json
{
  "source": "fixture-sintetico-v1",
  "records": [
    {
      "external_id": "fx-001",
      "name": "Negocio Sintético",
      "category": "Restaurante",
      "country": "Colombia",
      "city": "Bogota",
      "address": "Calle 1 #2-3, Chapinero, Bogota",
      "rating": 4.5,
      "review_count": 30,
      "business_status": "ACTIVO",
      "website_url": null,
      "phone": "+573001112233",
      "observed_at": "2026-11-20T12:00:00+00:00"
    }
  ],
  "fail_queries": {
    "sabor en Bogota, Colombia": "TIMEOUT"
  }
}
```

- `records` es obligatorio y debe ser una lista.
- `name`, `country`, `city` y `category` son obligatorios. Un registro sin
  ciudad, por ejemplo, se cuenta como inválido y se reporta; **no** se oculta
  como “resultado filtrado”.
- La validación ocurre **antes** de aplicar los filtros. Los registros
  inválidos se reportan aunque su ciudad o categoría no coincida con la
  búsqueda.
- `rating` va de 1 a 5 o `null`; `review_count` es entero no negativo o
  `null`. Los valores ausentes **no se imputan**.
- `business_status` acepta `ACTIVO`, `CERRADO`, `DESCONOCIDO` o `UNKNOWN`;
  si falta, se guarda `DESCONOCIDO`.
- `website_url` vacío o `null` **no** significa que el negocio no tenga web:
  en esta etapa toda observación queda en `PENDIENTE_VERIFICACION`.
- `observed_at` es ISO-8601; si falta, se usa la hora UTC de la corrida.
- `external_id` identifica la sede dentro de la fuente. Sin él se usa una
  huella local versionada (`local:v4:<huella>`) serializada como JSON canónico
  de [fuente, nombre, dirección, ciudad, país] normalizada con
  `normalize_identity`. Esta normalización aplica forma canónica Unicode NFC,
  minúsculas (`casefold`) y colapso de espacios múltiples, pero **conserva la
  `ñ` y caracteres distintivos** para evitar fusionar negocios distintos como
  `El Niño` y `El Nino`. Por separado, la búsqueda y los filtros continúan
  usando `normalize_text` (insensible a acentos y tildes). La serialización
  estructurada evita además colisiones cuando algún componente contiene
  delimitadores como `|`. El teléfono y el dominio **nunca** forman parte de
  la identidad, así que dos sedes que los comparten no se fusionan, y dos sedes
  con nombre, dirección y ciudad iguales en países distintos tampoco.
- **Identidad insuficiente:** si no hay `external_id` y falta la dirección o el
  país, no se puede concluir que dos registros sean la misma sede. En ese caso
  cada registro se conserva como prospecto separado con
  `external_id_kind = "REVISION"` y `identity_state = "REVISION"` en el
  reporte. Nunca se fusionan homónimos ni duplicados exactos por nombre y
  ciudad. La clave de revisión es una huella de los campos autorizados más un
  ordinal, así que repetir la misma entrada conserva los mismos prospectos y
  solo agrega observaciones.
- **Compatibilidad con claves históricas (`local:`, `local:v2:` y `local:v3:`):**
  las claves `local:<huella>` (etapa 1 sin país), `local:v2:<huella>`
  (delimitador `|`) y `local:v3:<huella>` (con eliminación diacrítica) ya
  persistidas no se reutilizan, no se sobrescriben ni se migran
  automáticamente. Una corrida nueva conserva esas filas intactas y crea un
  prospecto nuevo con clave `local:v4:...`. Cualquier duplicación derivada de
  este versionado se documenta; una migración o reconciliación de duplicados
  históricos requiere autorización explícita previa.
- `fail_queries` es opcional y permite simular una consulta fallida. Las
  consultas planificadas son `"<categoría> en <ciudad>, <país>"` y una por
  cada palabra clave: `"<palabra> en <ciudad>, <país>"`.
- El valor de `fail_queries` **nunca** se copia a la base, al reporte ni a la
  consola. Se traduce a un código seguro: `TIMEOUT`, `RATE_LIMIT`, `AUTH`,
  `NETWORK`, `UNAVAILABLE`, o `QUERY_FAILED` para cualquier otro contenido. El
  código queda en `query_attempt.error` y en `report.errors`, junto con la
  consulta que falló.

## Datos persistidos

Solo se guardan los campos de una lista explícita:
`external_id`, `name`, `category`, `country`, `city`, `address`, `phone`,
`website_url`, `rating`, `review_count`, `business_status` y `observed_at`.
Cualquier campo adicional del JSON (`password`, `token`, `email`,
`owner_name`, notas internas, etc.) se descarta antes de SQLite, del reporte y
de la consola.

## Reglas de la primera vertical

- Filtros: país, ciudad y categoría por igualdad normalizada (sin tildes ni
  mayúsculas); palabras clave por coincidencia en nombre/categoría/dirección;
  ubicación por coincidencia en la dirección.
- Reputación: `CUMPLE` si `rating >= min_rating` y
  `review_count >= min_reviews` (ambos inclusivos); `NO_CUMPLE` si algún dato
  existe pero no alcanza; `REVISION` si falta rating o número de reseñas.
- Estado de la corrida: `COMPLETA` si todas las consultas se ejecutaron sin
  registros inválidos; `PARCIAL` si alguna consulta falló o hay registros
  inválidos junto a resultados utilizables; `FALLIDA` si la fuente no se pudo
  leer o todas las consultas fallaron. Una búsqueda completa sin coincidencias
  sigue siendo `COMPLETA` con cero prospectos.
- Cada corrida guarda filtros efectivos, fuente, fechas UTC, estado,
  errores, intentos por consulta y observaciones.
- Repetir la misma entrada crea una corrida nueva y **agrega** observaciones;
  no duplica el prospecto ni borra observaciones anteriores. La repetición es
  idempotente para prospectos con identidad suficiente o con la misma clave de
  revisión.
- El reporte JSON de cada corrida queda en `--output-dir/<run_id>.json`.
- Si el proceso se interrumpe antes de cerrar la corrida, la fila queda en
  `EN_CURSO`; no se presenta como completa. No hay heartbeat ni reanudación
  automática en esta etapa.

## Pruebas

Con el runner estándar disponible (`unittest`; no se agregó `pytest`):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Las pruebas usan solo fixtures sintéticos y SQLite en directorios temporales.
No abren `data/leads.db`, no leen `.env` y no llaman a servicios externos.
`tests/captacion_web/test_corrections.py` cubre específicamente la protección
de la base SG-SST, la identidad insuficiente, la validación previa a los
filtros y la minimización de datos persistidos.
`tests/captacion_web/test_pending_findings.py` cubre el país en la identidad
local, la compatibilidad con claves de la etapa 1 y la traducción de
`fail_queries` a códigos seguros.
`tests/captacion_web/test_identity_collision.py` cubre la prevención de
colisiones por delimitadores en la identidad local, la separación de
sedes entre países, la persistencia de revisiones y la coexistencia con claves
históricas.
`tests/captacion_web/test_identity_normalization.py` cubre la separación entre
las reglas de normalización de filtros (`normalize_text`) e identidad
(`normalize_identity`), la no fusión de `El Niño` y `El Nino`, la forma canónica
Unicode NFC, el colapso de espacios, la idempotencia y la coexistencia con
claves `local:`, `local:v2:` y `local:v3:`.
`tests/captacion_web/test_stage2_verification_ranking.py` cubre la Etapa 2:
los seis estados de verificación web manual y su precedencia, corroboración
de sitios en constructores, detección de sitio oficial inaccesible,
evaluación de candidatos homónimos en redes sociales sin confirmar por solo
nombre, cálculo determinista de ranking con desglose y pesos configurables,
preservación de historial de revisiones y la regla de no contacto para casos
pendientes o con identidad insuficiente.
`tests/captacion_web/test_stage2_corrections.py` cubre la suite de regresión
para las correcciones de la Etapa 2: `ready_for_contact` estrictamente `False`
en Etapa 2 con `preliminary_commercial_fit`, paso de `external_id_kind` y
umbrales efectivos en el CLI, requerimiento de búsqueda suficiente documentada
(consultas no vacías con evidencias) para estados concluyentes, validación
sintáctica de URL para `OFICIAL_ENCONTRADA`, corroboración estricta de redes
sociales, deduplicación de perfil por estado social vigente, flujo operativo
de CLI manual y compatibilidad/reapertura idempotente de SQLite sintética con
esquema de Etapa 1.

## Etapa 2 · Verificación web manual, redes y ranking (RF-03, RF-04, RF-06)

- **Verificación web manual (`verification.py`):**
  Implementa los seis estados de contrato:
  - `OFICIAL_ENCONTRADA`: Sitio oficial del negocio identificado (incluyendo constructores tipo Wix, Shopify, etc.). Exige URL sintácticamente válida (`http`/`https`); una URL vacía o inválida no sustenta este estado y remite a `PENDIENTE_VERIFICACION`.
  - `NO_ENCONTRADA`: Búsqueda suficiente documentada (consulta no vacía, con evidencias registradas y completada) sin sitio web, perfiles sociales ni agregadores aplicables.
  - `SOLO_REDES_SOCIALES`: Sin sitio oficial tras búsqueda suficiente; perfil en red social confirmado. Agregador secundario conservado como evidencia si existe.
  - `SOLO_AGREGADOR`: Sin sitio web ni redes confirmadas tras búsqueda suficiente; solo agregador o directorio comercial comprobado.
  - `OFICIAL_NO_DISPONIBLE`: Sitio oficial corroborado pero inaccesible (error 5xx, timeout). No se infiere ausencia.
  - `PENDIENTE_VERIFICACION`: Búsqueda bloqueada, timeout, consulta parcial, consulta vacía sin evidencias o evidencia contradictoria.
  - Precedencia estricta: oficial accesible → oficial inaccesible → pendiente por evidencia/bloqueo → redes → agregador → no encontrada.
- **Candidatos de redes sociales (`verification.py`):**
  Canales: `INSTAGRAM`, `FACEBOOK`, `TIKTOK`, `LINKEDIN`, `WHATSAPP_COMERCIAL`.
  Estados: `CANDIDATO`, `CONFIRMADO`, `DESCARTADO`.
  Reglas de corroboración estricta (`05_MODELO_DE_DATOS.md`):
  - Confirmación exige corroboración fuerte: enlace desde fuente oficial verificada O nombre y ubicación coincidentes MÁS teléfono o dirección empresarial concordante.
  - Coincidencia de nombre o nombre+ciudad solos permanece como `CANDIDATO` (no se confirma por mero nombre o zona).
  - Un teléfono coincidente no confirma si el perfil tiene un nombre de negocio contradictorio o incompatible (se descarta o permanece en revisión).
  - `DESCARTADO` se reserva para contradicción explícita comprobada (ciudad distinta, teléfono divergente, nombre incompatible).
- **Ranking comercial (`ranking.py`):**
  Cálculo determinista, explicable, versionado (`rule_version = "v1"`) y configurable (`RankingConfig`).
  Pesos iniciales: web (35), rating (20), reviews (15), redes confirmadas (15), nicho (10), canal directo (5).
  Un teléfono observado no puntúa como canal verificado; este componente solo puntúa WhatsApp comercial confirmado. La presencia de un perfil no demuestra actividad reciente.
  Desglose (`breakdown`) y campos ausentes (`missing_data`) explícitos.
  El cálculo respeta los umbrales efectivos de filtros (`min_rating`, `min_reviews`) configurados en la corrida.
  Evalúa únicamente el **estado vigente** de cada perfil de red social (`(platform, url)`), asegurando que revisiones posteriores que descartan un perfil anulen puntos previamente otorgados.
  **Regla de contacto en Etapa 2:** `ready_for_contact` es estrictamente `False` para todos los prospectos en la Etapa 2, dado que la verificación de decisor confirmado, canal verificado y aprobación humana forman parte de etapas posteriores (Etapa 3+). Se expone `preliminary_commercial_fit` (y en `breakdown`) para reflejar si el prospecto cumple preliminarmente los criterios comerciales sin habilitar outreach ni contacto.
- **Historial y persistencia en SQLite (`storage.py`):**
  Tablas propias `website_review`, `social_candidate` y `ranking_snapshot` asociadas a `prospect`.
  Las revisiones son acumulativas y no borran evidencia histórica previa.
  Soporta evolución segura sobre bases SQLite creadas en Etapa 1, preservando prospectos preexistentes con claves `local:`, `local:v2:`, `local:v3:` y `local:v4:` con aperturas e inserciones idempotentes.

## Flujo operativo de revisión manual vía CLI

La vertical ofrece una interfaz por línea de comandos para registrar y consultar revisiones sin necesidad de invocar Python programáticamente:

### 1. Registrar revisión manual de sitio web

Para `SOLO_REDES_SOCIALES`, registra primero el perfil corroborado con el comando del paso 2. Un estado concluyente exige referencias a evidencia; no se generan IDs de evidencia por defecto. `--query-status` permite registrar bloqueos o resultados parciales sin inferir ausencia. Usa `--official-url` para un sitio oficial, más `--access-error` cuando esté inaccesible; para un agregador corroborado usa `--aggregator-url`.
```powershell
.\.venv\Scripts\python.exe -m prospector.captacion_web review-web `
  --db "data/captacion_web/captacion_web.db" `
  --prospect-id "<prospect_id_interno>" `
  --status "SOLO_REDES_SOCIALES" `
  --confidence "alta" `
  --reviewer "OPERADOR_HUMANO" `
  --evidence-ids "EV-01,EV-02" `
  --query "Restaurante Sabor Bogota" `
  --notes "Búsqueda completa sin web propia encontrada; perfil de Instagram corroborado"
```

### 2. Registrar candidato de red social
```powershell
.\.venv\Scripts\python.exe -m prospector.captacion_web review-social `
  --db "data/captacion_web/captacion_web.db" `
  --prospect-id "<prospect_id_interno>" `
  --platform "INSTAGRAM" `
  --url "https://instagram.com/restaurantesabor" `
  --status "CONFIRMADO" `
  --confidence "alta" `
  --reviewer "OPERADOR_HUMANO" `
  --corroboration "NOMBRE_SEDE_DIRECCION" `
  --signals "Teléfono y dirección coinciden con sede física" `
  --evidence-ids "EV-02"
```

### 3. Consultar revisiones e historial de un prospecto

`show-reviews` abre una base existente en solo lectura. No crea la base si falta ni modifica sus datos, esquema o modo de journal. En bases con WAL, SQLite puede crear o gestionar los archivos auxiliares `-wal` y `-shm` durante la consulta.

En texto, muestra primero la web vigente, las redes confirmadas, el último ranking (una estimación) y lo que falta verificar; después mantiene el historial. La salida `--json` conserva su formato anterior.
```powershell
.\.venv\Scripts\python.exe -m prospector.captacion_web show-reviews `
  --db "data/captacion_web/captacion_web.db" `
  --prospect-id "<prospect_id_interno>"
```
(Opcional: añadir `--json` para obtener la salida completa estructurada).

### 4. Importar lote estructurado de revisiones (JSON)

El lote es atómico: cualquier revisión inválida devuelve código 1 y no se guarda ninguna fila del lote. Los grupos `website_reviews` y `social_candidates` aceptan los mismos campos que el CLI en formato JSON (por ejemplo, `evidence_ids` como lista y `corroboration` para confirmar un perfil). El operador aporta las referencias y declara el tipo de corroboración: el programa no consulta ni valida su contenido en Internet. Se aplican primero los candidatos sociales del lote y después las revisiones web. `reviewed_at` es opcional, en ISO 8601 con zona, y se guarda en UTC.
```powershell
.\.venv\Scripts\python.exe -m prospector.captacion_web import-reviews `
  --db "data/captacion_web/captacion_web.db" `
  --input "ruta/a/revisiones.json"
```

## Etapa 3A · Registro manual del decisor

Usa el ID interno de un prospecto existente. La primera revisión válida añade
su historial a la base de Captación Web; el comando no crea una base nueva.

```powershell
python -m prospector.captacion_web review-decision-maker `
  --db "ruta/a/captacion_web.db" --prospect-id "<id_interno>" `
  --status DECISOR_ALTERNO_CONFIRMADO --reviewer "OPERADOR" `
  --name "Ana Ejemplo" --role "Gerente" --source-url "https://example.test/equipo"
```

También puedes elegir `PROPIETARIO_CONFIRMADO` solo cuando la fuente pública
respaldó explícitamente la propiedad. Para `PENDIENTE_MANUAL` o
`NO_VERIFICABLE`, omite el nombre y el cargo. Usa una fuente profesional
pública, sin datos privados ni enlaces de sesión. El comando no abre la URL
ni verifica su contenido: el operador documenta la comprobación. Con
`--reviewed-at` puedes aportar una fecha ISO 8601 con zona horaria; si la
omites se registra la hora actual en UTC.

`show-reviews` muestra el estado vigente y el historial en texto. La ficha
no cambia el ranking ni habilita el contacto. La salida `--json` conserva
su contrato; preparar la exportación al asistente corresponde a 3B.

## Fuera de esta etapa

Verificación automatizada con scrapers en vivo, conectores externos a plataformas,
exportación al asistente IA (RF-08), panel,
Web Factory, Video Factory, CRM y contacto comercial: corresponden a etapas
posteriores.

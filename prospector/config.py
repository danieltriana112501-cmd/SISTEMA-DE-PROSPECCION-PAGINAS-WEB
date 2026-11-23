"""Config estática: zonas, queries, matriz de riesgo (doc maestro §3-5)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
QUERIES_DIR = ROOT / "queries"
DB_PATH = DATA_DIR / "leads.db"

GOOGLE_MAPS_SCRAPER_IMAGE = "gosom/google-maps-scraper"

# Diccionario de queries por zona/sector — cobertura de los 8 sectores y 37 tipos
# de empresa objetivo del catalogo (doc §4), expandido desde el set inicial de §5B.
# Todo empleador en Colombia esta obligado a SG-SST (doc §3); estas zonas priorizan
# por exigencia legal (Prioridad 1 = Riesgo IV-V / 60 Estandares) pero cubren tambien
# comercio y gastronomia (Riesgo I-III / 7-21 Estandares) porque tambien son leads validos.
QUERIES_POR_ZONA = {
    # --- A. Salud, clinico y diagnostico ---
    "salud-bogota": [
        "IPS clinica en Bogota",
        "centro medico en Chapinero",
        "clinica de estetica y cirugia plastica en Bogota",
        "consultorio odontologico en Bogota",
        "laboratorio clinico en Usaquen",
        "laboratorio de patologia Bogota",
        "optometria en Bogota",
        "clinica oftalmologica Bogota",
        "imagenes diagnosticas Bogota",
        "centro de radiologia Bogota",
    ],
    # --- B. Servicios generales, aseo y tercerizacion ---
    "servicios-aseo": [
        "empresa de aseo y limpieza en Bogota",
        "empresa de vigilancia y seguridad en Bogota",
        "empresa de servicios temporales Bogota",
        "mantenimiento locativo Bogota",
        "mantenimiento de ascensores Bogota",
        "empresa de fumigacion y control de plagas Bogota",
    ],
    # --- C. Alimentos, bebidas y gastronomia (incluye restaurantes) ---
    "alimentos-gastronomia": [
        "restaurante en Bogota",
        "cadena de restaurantes en Bogota",
        "casino de alimentacion empresarial Bogota",
        "planta procesadora de alimentos en Fontibon",
        "frigorifico en Bogota",
        "planta lactea Bogota",
        "panificadora industrial Bogota",
        "fabrica de bebidas Bogota",
        "cerveceria artesanal Bogota",
    ],
    # --- D. Manufactura e industria ---
    "manufactura-industrial": [
        "metalmecanica en Montevideo Bogota",
        "taller de soldadura y estructuras metalicas Bogota",
        "fabrica de plasticos en Alamos",
        "fabrica de quimicos y pinturas Bogota",
        "laboratorio farmaceutico Bogota",
        "fabrica de muebles Bogota",
        "confeccion industrial Bogota",
        "litografia e imprenta Bogota",
        "taller de mecanica automotriz Bogota",
        "concesionario de vehiculos Bogota",
    ],
    # --- E. Construccion, infraestructura y mineria ---
    "construccion": [
        "constructora en Bogota",
        "contratista de obras civiles Bogota",
        "empresa de montajes electricos industriales Bogota",
        "planta de concreto Bogota",
        "cantera de agregados Cundinamarca",
    ],
    # --- F. Logistica, almacenamiento y transporte ---
    "logistica-transporte": [
        "operador logistico en Bogota",
        "centro de distribucion y bodegas Bogota",
        "transporte de carga en Bogota",
        "transporte de pasajeros empresarial Bogota",
        "empresa de mensajeria y paqueteria Bogota",
    ],
    # --- G. Comercial, educativo y administrativo ---
    "comercial-educativo": [
        "colegio privado en Bogota",
        "instituto tecnico en Bogota",
        "centro comercial en Bogota",
        "hotel corporativo en Bogota",
        "ferreteria industrial mayorista Bogota",
        "supermercado local en Bogota",
    ],
    # --- Corredor Sabana Occidente (Funza, Mosquera, Madrid, Facatativa) ---
    "sabana-occidente": [
        "parque industrial en Funza",
        "restaurante en Mosquera",
        "bodegas logistica en Mosquera",
        "planta de alimentos en Madrid Cundinamarca",
        "distribuidora mayorista en Madrid Cundinamarca",
        "floricultura en Facatativa",
    ],
    # --- Corredor Sabana Norte (Cota, Chia, Tocancipa, Zipaquira, Cajica, Sopo) ---
    "sabana-norte": [
        "operador logistico en Siberia Cota",
        "laboratorio farmaceutico en Tocancipa",
        "parque industrial Oikos Cota",
        "constructora en Chia",
        "restaurante en Chia",
        "hotel campestre en Chia",
        "planta lactea en Sopo",  # corredor lacteo real (Alpina y proveedores), no generico
        "empresa agroindustrial en Cajica",
        "restaurante en Cajica",
        "laboratorio farmaceutico en Zipaquira",
        "empresa de mineria en Zipaquira",
    ],
    # --- Sabana Sur (Soacha, Sibate) ---
    "sabana-sur": [
        "empresa de manufactura en Cazuca Soacha",
        "fabrica en Soacha Cundinamarca",
        "restaurante en Soacha",
        "bodega de almacenamiento en Soacha",
        "fabrica en Sibate Cundinamarca",
        "restaurante en Sibate",
    ],
    # --- Fusagasuga (Sumapaz) — pueblo grande al sur, fuera de la Sabana propia,
    # mezcla agroindustria/comercio/salud/construccion en un solo municipio, igual
    # que sabana-sur, en vez de forzarlo dentro de una zona de la Sabana que no es. ---
    "fusagasuga": [
        "empresa agroindustrial en Fusagasuga",
        "restaurante en Fusagasuga",
        "ferreteria industrial en Fusagasuga",
        "IPS clinica en Fusagasuga",
        "constructora en Fusagasuga",
        "colegio privado en Fusagasuga",
    ],
}

# Prioridad comercial por zona/sector (doc §5A) — usada para ordenar corridas por lotes.
# 1 = Riesgo IV-V / 60 Estandares (urgencia legal maxima). 2 = auditorias cruzadas /
# multi-sede. 3 = pymes en regulacion menor (comercio, educativo, gastronomia).
PRIORIDAD_ZONA = {
    "salud-bogota": 1,
    "construccion": 1,
    "manufactura-industrial": 1,
    "sabana-norte": 1,
    "servicios-aseo": 2,
    "alimentos-gastronomia": 2,
    "logistica-transporte": 2,
    "sabana-occidente": 2,
    "comercial-educativo": 3,
    "sabana-sur": 3,
    "fusagasuga": 2,  # mezcla agroindustria (Riesgo II-IV) + comercio (Riesgo I-III)
}

# Nivel de riesgo ARL / estándares exigidos por categoría de Google Maps (doc §3-4)
# Heurística estática: palabras clave en la categoría -> (riesgo, estandares)
RIESGO_POR_CATEGORIA = [
    (["clinica", "ips", "laboratorio", "hospital", "cirugia"], "Riesgo III-V", "60 Estándares (Res. 0312/2019)"),
    (["constructora", "obra civil", "cantera", "concreto", "mineria", "minero"], "Riesgo IV-V", "60 Estándares (Res. 0312/2019)"),
    (["metalmecanica", "quimica", "farmaceutica", "plastico", "soldadura"], "Riesgo III-V", "60 Estándares (Res. 0312/2019)"),
    (["textil", "confeccion", "mueble", "madera", "imprenta", "litografia", "automotriz", "mecanica"], "Riesgo III-V", "60 Estándares (Res. 0312/2019)"),
    (["aseo", "limpieza", "vigilancia", "seguridad privada", "fumigacion", "plagas"], "Riesgo III-IV", "21 Estándares (Res. 0312/2019)"),
    (["restaurante", "catering", "alimentos", "bebidas", "frigorifico", "panificadora", "cerveceria"], "Riesgo II-IV", "21 Estándares (Res. 0312/2019)"),
    (["logistico", "transporte", "carga", "mensajeria", "bodega", "distribucion"], "Riesgo III-IV", "21 Estándares (Res. 0312/2019)"),
    (["floricultura", "avicola", "porcicola", "invernadero"], "Riesgo II-IV", "21 Estándares (Res. 0312/2019)"),
]
RIESGO_DEFAULT = ("Riesgo I-III", "7 Estándares (Res. 0312/2019)")  # comercio, educativo, hoteles, oficinas

# Cajas geograficas para barrido -grid-bbox (PLAN_MEJORAS.md 3.1) en los corredores
# industriales de la Sabana donde el retorno por query de texto viene bajo (techo de
# ~120-200 resultados por vista de Google Maps, sin importar que tan buena sea la query).
# Coordenadas estimadas sobre el corredor/barrio real (no requieren precision milimetrica,
# es punto de partida calibrable con una corrida de prueba chica antes de escalar).
# celda en km; zoom 16 da vistas de calle/manzana, suficientemente cerrado para no
# repetir el mismo techo de resultados a menor escala.
GRID_ZONAS = {
    "siberia-cota": {
        "bbox": "4.7550,-74.1700,4.8350,-74.0950",
        "zoom": 16,
        "celda": 1.2,
    },
    "alamos": {
        "bbox": "4.6850,-74.1250,4.7150,-74.0950",
        "zoom": 16,
        "celda": 1.0,
    },
    "funza": {
        "bbox": "4.6900,-74.2450,4.7450,-74.1950",
        "zoom": 16,
        "celda": 1.2,
    },
    "cazuca": {
        "bbox": "4.5450,-74.2350,4.5850,-74.1950",
        "zoom": 17,
        "celda": 1.0,
    },
}

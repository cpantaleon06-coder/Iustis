"""Troceo de textos legales por artículo.

Principio rector: este módulo nunca completa, corrige ni reescribe texto legal.
Solo normaliza espacios y codificación, separa por artículo y, si algo parece
faltar, marca el artículo como incompleto explicando el motivo.

Dos niveles de alerta:
  incompleto   señal fuerte (sin texto, puntos suspensivos, marca [INCOMPLETO]);
               el artículo no entra al índice y nunca se cita.
  advertencias señal débil (por ejemplo, termina sin punto); el artículo sí se
               indexa, pero aparece en el reporte para que alguien lo revise.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Sufijos de artículos adicionados (48 Bis, 3o. Ter, etc.)
_SUFIJOS = r"(?i:bis|ter|qu[aá]ter|quinquies|sexies|septies|octies|nonies|decies)"

# Encabezado de artículo. Se exige "Artículo" con mayúscula inicial y un separador
# después del número (".-", ".", "-" o guion corto) para no confundir las remisiones
# que aparecen en el cuerpo ("artículo 50 de esta Ley").
ENCABEZADO_ARTICULO = re.compile(
    r"^\s*(?:ART[IÍ]CULO|Art[ií]culo)\s+"
    r"(?P<num>\d+)(?:\s*(?:o|º|°)\.?)?"
    r"(?:\s*-\s*(?P<letra>[A-ZÑ])(?![A-Za-zÁÉÍÓÚáéíóúÑñ]))?"  # artículos adicionados con letra: 39-A, 353-Ñ
    r"(?:\s*[.\-]?\s*(?P<suf>" + _SUFIJOS + r")\b\.?)?"
    r"\s*(?:\.\s*-|\.|-|–)\s*(?P<resto>.*)$"
)

# Encabezados de estructura: "TÍTULO SEGUNDO", "Capítulo IV", "Sección Primera", "CAPITULO III BIS"
ENCABEZADO_ESTRUCTURA = re.compile(
    r"^\s*(?P<nivel>LIBRO|T[IÍ]TULO|CAP[IÍ]TULO|SECCI[OÓ]N)\s+(?P<resto>\S.*)$", re.IGNORECASE
)
_ORDINAL = re.compile(
    r"^(?:[IVXLCDM]+|\d+|[úu]nic[oa]|primer[oa]?|segund[oa]|tercer[oa]?|cuart[oa]|quint[oa]|sext[oa]|"
    r"s[ée]ptim[oa]|octav[oa]|noven[oa]|d[ée]cim[oa]|und[ée]cim[oa]|duod[ée]cim[oa]|once|doce|trece|catorce|"
    r"quince|diecis[ée]is|diecisiete|dieciocho|diecinueve|veinte|bis|ter|qu[aá]ter)\.?$",
    re.IGNORECASE,
)
_NIVELES = ["LIBRO", "TITULO", "CAPITULO", "SECCION"]
MAX_LINEAS_SUBTITULO = 3

ENCABEZADO_TRANSITORIOS = re.compile(
    r"^\s*(?:ART[IÍ]CULOS?\s+)?TRANSITORIOS?\s*\.?\s*$"
)

# Notas de reforma de los textos compilados. Pueden ocupar varias líneas:
#   "Artículo reformado DOF 30-11-2012"
#   "Párrafo reformado G.O. CDMX 10/08/23"
#   "Fe de erratas al artículo DOF 30-04-1970"
#   "Reforma DOF 01-05-2026: Derogó del artículo el entonces párrafo segundo"
NOTA_INICIO = re.compile(
    r"^\s*(?:"
    r"(?:Art[ií]culo|P[áa]rrafo|Fracci[óo]n|Inciso|Apartado|Numeral|Cap[ií]tulo|Secci[óo]n|T[ií]tulo|"
    r"Denominaci[óo]n|Tabla|Anexo|Encabezado|Libro)\w*\b.{0,80}?"
    r"\b(?:reformad|adicionad|derogad|recorrid|modificad|reubicad)\w*"
    r"|Fe de erratas\b"
    r"|Derogad[oa]\s+(?:DOF|G\.\s*O\.)"
    r"|Reforma\s+(?:DOF|G\.\s*O\.)"
    r")",
    re.IGNORECASE,
)
GACETA = re.compile(r"\bDOF\b|\bGODF\b|\bGOCDMX\b|\bGaceta\b|\bG\.\s*O\.")
MAX_LINEAS_NOTA = 4


def _comillas_cerradas(texto: str) -> bool:
    return texto.count("“") <= texto.count("”")


DEROGADO = re.compile(r"^\(?\s*(?:se\s+deroga|derogad[oa])\s*\)?\.?\s*$", re.IGNORECASE)

# Marcas de posible omisión en la fuente
ELIPSIS = re.compile(r"\[\s*(?:\.\.\.|…)\s*\]|\(\s*(?:\.\.\.|…)\s*\)|\.\.\.|…")
MARCA_MANUAL = re.compile(r"\[\s*(?:INCOMPLETO|ILEGIBLE|FALTA)[^\]]*\]", re.IGNORECASE)
PUNTUACION_FINAL = tuple('.;:)"”»]')

RUIDO_POR_DEFECTO = [
    r"^\s*\d+\s+de\s+\d+\s*$",
    r"^\s*P[áa]gina\s+\d+(?:\s+de\s+\d+)?\s*$",
]


@dataclass
class Articulo:
    id: str
    articulo: str  # tal como se muestra al usuario, por ejemplo "48 Bis" o "39-A"
    numero_base: int
    sufijo: str | None
    ubicacion: str
    texto: str
    notas_reforma: list[str] = field(default_factory=list)
    derogado: bool = False
    incompleto: bool = False
    motivos_incompleto: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    linea_origen: int = 0


@dataclass
class ResultadoParseo:
    articulos: list[Articulo]
    lineas_ruido_eliminadas: int = 0
    lineas_transitorios_omitidas: int = 0
    encabezados_descartados: list[dict] = field(default_factory=list)
    saltos_numeracion: list[dict] = field(default_factory=list)
    duplicados: list[str] = field(default_factory=list)
    texto_previo_descartado: int = 0  # líneas antes del primer artículo (no son artículos)
    notas_de_encabezados: int = 0  # notas de reforma de títulos o capítulos (no pertenecen a un artículo)


def normalizar_texto(crudo: str) -> str:
    """Normaliza codificación y espacios sin tocar el contenido."""
    texto = unicodedata.normalize("NFC", crudo)
    texto = texto.replace(" ", " ").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    lineas = [re.sub(r"[ \t]+", " ", linea).strip() for linea in texto.split("\n")]
    return "\n".join(lineas)


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _normalizar_sufijo(suf: str | None) -> str | None:
    return _sin_acentos(suf.lower()) if suf else None


def _nivel(nombre: str) -> str:
    return _sin_acentos(nombre.upper())


def es_encabezado_estructura(linea: str) -> re.Match | None:
    """Evita confundir con encabezados las líneas del cuerpo que empiezan con "Capítulo"."""
    m = ENCABEZADO_ESTRUCTURA.match(linea)
    if not m or len(linea) > 120:
        return None
    palabras = m.group("resto").split()
    if not _ORDINAL.match(palabras[0]):
        return None
    # "Capítulo IV", "Sección Primera", "CAPITULO III BIS": solo ordinales
    if all(_ORDINAL.match(p) for p in palabras):
        return m
    # Formato de una sola línea en mayúsculas: "CAPÍTULO I DISPOSICIONES GENERALES"
    return m if linea == linea.upper() else None


def _detectar_incompletitud(art: Articulo) -> None:
    motivos, advertencias = [], []
    if not art.texto.strip():
        motivos.append("sin texto en la fuente")
    else:
        if ELIPSIS.search(art.texto):
            motivos.append("contiene puntos suspensivos (posible omisión en la fuente)")
        if MARCA_MANUAL.search(art.texto):
            motivos.append("marcado manualmente como incompleto o ilegible")
        if not art.derogado and not art.texto.rstrip().endswith(PUNTUACION_FINAL):
            advertencias.append("termina sin puntuación final (revisar contra la fuente)")
    art.motivos_incompleto = motivos
    art.incompleto = bool(motivos)
    art.advertencias = advertencias


def parsear(
    crudo: str,
    abreviatura: str,
    excluir_transitorios: bool = True,
    ruido_regex: list[str] | None = None,
) -> ResultadoParseo:
    patrones_ruido = [re.compile(p) for p in RUIDO_POR_DEFECTO + list(ruido_regex or [])]
    lineas = normalizar_texto(crudo).split("\n")

    resultado = ResultadoParseo(articulos=[])
    estructura: dict[str, str] = {}
    actual: Articulo | None = None
    cuerpo: list[str] = []
    ultimo_num = 0
    subtitulo_de: str | None = None  # nivel cuyo nombre se está leyendo
    lineas_subtitulo = 0
    en_encabezado = False  # entre un encabezado de estructura y el siguiente artículo
    vistos: dict[str, int] = {}

    def cerrar_actual() -> None:
        if actual is None:
            return
        # Colapsa líneas en blanco repetidas y recorta extremos
        texto = re.sub(r"\n{3,}", "\n\n", "\n".join(cuerpo)).strip()
        actual.texto = texto
        actual.derogado = bool(DEROGADO.match(texto))
        _detectar_incompletitud(actual)
        resultado.articulos.append(actual)

    i = 0
    while i < len(lineas):
        linea = lineas[i]
        num_linea = i + 1
        i += 1

        if any(p.match(linea) for p in patrones_ruido):
            resultado.lineas_ruido_eliminadas += 1
            continue

        if excluir_transitorios and ENCABEZADO_TRANSITORIOS.match(linea):
            resultado.lineas_transitorios_omitidas = sum(1 for l in lineas[i:] if l)
            break

        # Notas de reforma (de una o varias líneas). Un encabezado de artículo nunca es nota.
        if NOTA_INICIO.match(linea) and not ENCABEZADO_ARTICULO.match(linea):
            nota = None
            partes, j = [linea], i
            completa = bool(GACETA.search(linea)) and _comillas_cerradas(linea)
            while not completa and j < len(lineas) and len(partes) < MAX_LINEAS_NOTA:
                siguiente = lineas[j]
                if ENCABEZADO_ARTICULO.match(siguiente) or es_encabezado_estructura(siguiente):
                    break
                if siguiente:
                    partes.append(siguiente)
                    texto_nota = " ".join(partes)
                    completa = bool(GACETA.search(texto_nota)) and _comillas_cerradas(texto_nota)
                j += 1
            if completa:
                nota = " ".join(partes)
                i = max(i, j)
            if nota is not None:
                if en_encabezado or actual is None:
                    resultado.notas_de_encabezados += 1
                else:
                    actual.notas_reforma.append(nota)
                continue

        m_est = es_encabezado_estructura(linea)
        if m_est:
            nivel = _nivel(m_est.group("nivel"))
            idx = _NIVELES.index(nivel)
            for n in _NIVELES[idx:]:
                estructura.pop(n, None)
            estructura[nivel] = linea
            subtitulo_de, lineas_subtitulo, en_encabezado = nivel, 0, True
            continue

        m_art = ENCABEZADO_ARTICULO.match(linea)
        if m_art:
            num = int(m_art.group("num"))
            letra = m_art.group("letra")
            suf_bis = _normalizar_sufijo(m_art.group("suf"))
            # El sufijo combina letra y adición: "a", "bis", "a-bis"
            # La letra se conserva tal cual (353-Ñ no es 353-N)
            suf = "-".join(x for x in (letra.lower() if letra else None, suf_bis) if x) or None
            if num < ultimo_num:
                # Número menor al anterior: casi siempre es una remisión que cayó al
                # inicio de línea. Se conserva como texto y se reporta para revisión.
                resultado.encabezados_descartados.append(
                    {"linea": num_linea, "texto": linea[:120], "motivo": f"número {num} menor al anterior {ultimo_num}"}
                )
            else:
                cerrar_actual()
                if actual is not None and num > ultimo_num + 1 and ultimo_num > 0:
                    resultado.saltos_numeracion.append({"despues_de": ultimo_num, "siguiente": num})
                ultimo_num = num
                clave = f"{num}-{suf}" if suf else str(num)
                vistos[clave] = vistos.get(clave, 0) + 1
                art_id = f"{abreviatura}-{num}" + (f"-{letra}" if letra else "") + (f"-{suf_bis}" if suf_bis else "")
                if vistos[clave] > 1:
                    art_id += f"-dup{vistos[clave]}"
                    resultado.duplicados.append(art_id)
                etiqueta = f"{num}-{letra}" if letra else str(num)
                if suf_bis:
                    etiqueta += f" {m_art.group('suf').capitalize()}"
                actual = Articulo(
                    id=art_id,
                    articulo=etiqueta,
                    numero_base=num,
                    sufijo=suf,
                    ubicacion=" > ".join(estructura[n] for n in _NIVELES if n in estructura),
                    texto="",
                    linea_origen=num_linea,
                )
                cuerpo = [m_art.group("resto")] if m_art.group("resto") else []
                subtitulo_de, en_encabezado = None, False
                continue

        if subtitulo_de:
            # Nombre del título o capítulo: líneas seguidas al encabezado (puede ocupar varias).
            # Se admiten líneas en blanco entre el encabezado y el nombre.
            if not linea and lineas_subtitulo == 0:
                continue
            if linea and len(linea) <= 120 and lineas_subtitulo < MAX_LINEAS_SUBTITULO:
                sep = " (" if lineas_subtitulo == 0 else " "
                base = estructura[subtitulo_de]
                estructura[subtitulo_de] = (base[:-1] if lineas_subtitulo else base) + f"{sep}{linea})"
                lineas_subtitulo += 1
                continue
            subtitulo_de = None
            if not linea:
                continue

        if actual is None:
            if linea:
                resultado.texto_previo_descartado += 1
            continue
        if en_encabezado and not linea:
            continue
        cuerpo.append(linea)

    cerrar_actual()
    return resultado

"""Troceo de textos legales por artículo.

Principio rector: este módulo nunca completa, corrige ni reescribe texto legal.
Solo normaliza espacios y codificación, separa por artículo y, si algo parece
faltar, marca el artículo como incompleto explicando el motivo.
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
    r"(?:\s*[.\-]?\s*(?P<suf>" + _SUFIJOS + r")\b\.?)?"
    r"\s*(?:\.\s*-|\.|-|–)\s*(?P<resto>.*)$"
)

# Encabezados de estructura (siempre en mayúsculas en los textos oficiales)
ENCABEZADO_ESTRUCTURA = re.compile(
    r"^\s*(?P<nivel>LIBRO|T[IÍ]TULO|CAP[IÍ]TULO|SECCI[OÓ]N)\s+(?P<resto>\S.*)?$"
)
_NIVELES = ["LIBRO", "TITULO", "CAPITULO", "SECCION"]

ENCABEZADO_TRANSITORIOS = re.compile(
    r"^\s*(?:ART[IÍ]CULOS?\s+)?TRANSITORIOS?\s*\.?\s*$"
)

# Notas de reforma que publican los textos compilados ("Artículo reformado DOF 30-11-2012")
NOTA_REFORMA = re.compile(
    r"^\s*(?:Art[ií]culo|P[áa]rrafo|Fracci[óo]n|Inciso|Apartado|Numeral|Cap[ií]tulo|"
    r"Secci[óo]n|T[ií]tulo|Denominaci[óo]n|Fe de erratas)\b.{0,60}?"
    r"\b(?:reformad|adicionad|derogad|recorrid|con fe de erratas)\w*\b.*"
    r"\b(?:DOF|GODF|GOCDMX|Gaceta)\b.*$",
    re.IGNORECASE,
)

DEROGADO = re.compile(r"^\(?\s*(?:se\s+deroga|derogad[oa])\s*\)?\.?\s*$", re.IGNORECASE)

# Marcas de posible omisión en la fuente
ELIPSIS = re.compile(r"\[\s*(?:\.\.\.|…)\s*\]|\(\s*(?:\.\.\.|…)\s*\)|\.\.\.|…")
MARCA_MANUAL = re.compile(r"\[\s*(?:INCOMPLETO|ILEGIBLE|FALTA)[^\]]*\]", re.IGNORECASE)
PUNTUACION_FINAL = tuple('.;:)"”»')

RUIDO_POR_DEFECTO = [
    r"^\s*\d+\s+de\s+\d+\s*$",
    r"^\s*P[áa]gina\s+\d+(?:\s+de\s+\d+)?\s*$",
]


@dataclass
class Articulo:
    id: str
    articulo: str  # tal como se muestra al usuario, por ejemplo "48 Bis"
    numero_base: int
    sufijo: str | None
    ubicacion: str
    texto: str
    notas_reforma: list[str] = field(default_factory=list)
    derogado: bool = False
    incompleto: bool = False
    motivos_incompleto: list[str] = field(default_factory=list)
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


def normalizar_texto(crudo: str) -> str:
    """Normaliza codificación y espacios sin tocar el contenido."""
    texto = unicodedata.normalize("NFC", crudo)
    texto = texto.replace(" ", " ").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    lineas = [re.sub(r"[ \t]+", " ", linea).strip() for linea in texto.split("\n")]
    return "\n".join(lineas)


def _normalizar_sufijo(suf: str | None) -> str | None:
    if not suf:
        return None
    s = unicodedata.normalize("NFD", suf.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _nivel(nombre: str) -> str:
    s = unicodedata.normalize("NFD", nombre.upper())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _detectar_incompletitud(art: Articulo) -> None:
    motivos = []
    if not art.texto.strip():
        motivos.append("sin texto en la fuente")
    else:
        if ELIPSIS.search(art.texto):
            motivos.append("contiene puntos suspensivos (posible omisión en la fuente)")
        if MARCA_MANUAL.search(art.texto):
            motivos.append("marcado manualmente como incompleto o ilegible")
        if not art.derogado and not art.texto.rstrip().endswith(PUNTUACION_FINAL):
            motivos.append("termina sin puntuación final (posible truncamiento)")
    art.motivos_incompleto = motivos
    art.incompleto = bool(motivos)


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
    esperando_subtitulo: str | None = None
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

    for i, linea in enumerate(lineas, start=1):
        if any(p.match(linea) for p in patrones_ruido):
            resultado.lineas_ruido_eliminadas += 1
            continue

        if excluir_transitorios and ENCABEZADO_TRANSITORIOS.match(linea):
            resultado.lineas_transitorios_omitidas = sum(1 for l in lineas[i:] if l)
            break

        if NOTA_REFORMA.match(linea):
            if actual is not None:
                actual.notas_reforma.append(linea)
            continue

        m_est = ENCABEZADO_ESTRUCTURA.match(linea)
        if m_est and len(linea) <= 120:
            nivel = _nivel(m_est.group("nivel"))
            idx = _NIVELES.index(nivel)
            for n in _NIVELES[idx:]:
                estructura.pop(n, None)
            estructura[nivel] = linea
            esperando_subtitulo = nivel
            continue

        m_art = ENCABEZADO_ARTICULO.match(linea)
        if m_art:
            num = int(m_art.group("num"))
            suf = _normalizar_sufijo(m_art.group("suf"))
            if num < ultimo_num:
                # Número menor al anterior: casi siempre es una remisión que cayó al
                # inicio de línea. Se conserva como texto y se reporta para revisión.
                resultado.encabezados_descartados.append(
                    {"linea": i, "texto": linea[:120], "motivo": f"número {num} menor al anterior {ultimo_num}"}
                )
            else:
                cerrar_actual()
                if actual is not None and num > ultimo_num + 1 and ultimo_num > 0:
                    resultado.saltos_numeracion.append({"despues_de": ultimo_num, "siguiente": num})
                ultimo_num = num
                clave = f"{num}-{suf}" if suf else str(num)
                vistos[clave] = vistos.get(clave, 0) + 1
                art_id = f"{abreviatura}-{clave}"
                if vistos[clave] > 1:
                    art_id += f"-dup{vistos[clave]}"
                    resultado.duplicados.append(art_id)
                etiqueta = f"{num} {m_art.group('suf').capitalize()}" if suf else str(num)
                actual = Articulo(
                    id=art_id,
                    articulo=etiqueta,
                    numero_base=num,
                    sufijo=suf,
                    ubicacion=" > ".join(estructura[n] for n in _NIVELES if n in estructura),
                    texto="",
                    linea_origen=i,
                )
                cuerpo = [m_art.group("resto")] if m_art.group("resto") else []
                esperando_subtitulo = None
                continue

        if esperando_subtitulo and linea:
            # Nombre del título o capítulo en la línea siguiente al encabezado
            if len(linea) <= 120 and not linea.endswith(PUNTUACION_FINAL[:3]):
                estructura[esperando_subtitulo] += f" ({linea})"
                esperando_subtitulo = None
                continue
            esperando_subtitulo = None

        if actual is None:
            if linea:
                resultado.texto_previo_descartado += 1
            continue
        cuerpo.append(linea)

    cerrar_actual()
    return resultado

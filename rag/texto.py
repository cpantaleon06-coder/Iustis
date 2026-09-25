"""Utilidades de texto compartidas por la búsqueda léxica y el verificador de citas."""

import re
import unicodedata

# Palabras vacías frecuentes en español (no aportan a la búsqueda léxica)
STOPWORDS = set(
    """a al algo algun alguna algunas alguno algunos ante antes aquel asi aun cada como con contra cual
    cuando de del desde donde dos el ella ellas ello ellos en entre era eran es esa esas ese eso esos esta
    estaba estan estas este esto estos fue fueron ha han hasta hay la las le les lo los mas me mi mis muy
    ni no nos o os otra otras otro otros para pero por porque que quien se sea segun ser si sin sobre su
    sus tambien tan tanto te ti tu tus un una unas uno unos y ya yo""".split()
)


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def tokenizar(texto: str) -> list[str]:
    tokens = re.findall(r"\w+", sin_acentos(texto.lower()))
    return [t for t in tokens if t not in STOPWORDS]


_COMILLAS = str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'"})


def normalizar_para_cotejo(s: str) -> str:
    """Normalización mínima para comparar una cita contra el texto fuente.

    Solo unifica espacios, saltos de línea y comillas tipográficas. No toca
    mayúsculas, acentos ni palabras: la cita debe ser literal.
    """
    s = unicodedata.normalize("NFC", s).translate(_COMILLAS).replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()

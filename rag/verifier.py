"""Compuertas de verificación (código puro, no dependen de que el modelo obedezca).

Compuerta 1 (antes de generar): si ningún artículo recuperado se parece lo suficiente
a la consulta, no se genera respuesta jurídica; se declara que no se puede confirmar
y se canaliza.

Compuerta 2 (después de generar): cada cita que produce el modelo se coteja contra el
texto fuente. Solo sobrevive si el artículo estaba entre los recuperados y el texto
citado aparece literalmente en él. Se admiten fragmentos unidos por "(...)", "[...]"
o "…", siempre en el mismo orden en que aparecen en el artículo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag.retriever import Recuperado
from rag.texto import normalizar_para_cotejo

SEPARADOR_FRAGMENTOS = re.compile(r"\(\s*(?:\.\.\.|…)\s*\)|\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…")
FRAGMENTO_MIN = 10


@dataclass
class DecisionRecuperacion:
    contestar: bool
    motivo: str
    mejor_similitud: float
    umbral: float
    umbral_calibrado: bool
    contexto: list[Recuperado] = field(default_factory=list)


def evaluar_recuperacion(
    resultados: list[Recuperado], umbral: float, umbral_calibrado: bool = False
) -> DecisionRecuperacion:
    mejor = max((r.similitud for r in resultados), default=0.0)
    base = dict(mejor_similitud=mejor, umbral=umbral, umbral_calibrado=umbral_calibrado)
    if not resultados:
        return DecisionRecuperacion(False, "no se recuperó ningún artículo", **base)
    if any(r.referencia_explicita for r in resultados):
        return DecisionRecuperacion(True, "la consulta menciona un artículo del corpus", contexto=resultados, **base)
    if mejor < umbral:
        return DecisionRecuperacion(
            False, f"la similitud máxima ({mejor:.3f}) no alcanza el umbral ({umbral:.3f})", **base
        )
    return DecisionRecuperacion(True, "similitud suficiente", contexto=resultados, **base)


@dataclass
class Cita:
    id: str
    texto: str


@dataclass
class CitaVerificada:
    cita: Cita
    valida: bool
    motivo: str
    articulo: dict | None = None


def _fragmentos(texto_cita: str) -> list[str]:
    partes = SEPARADOR_FRAGMENTOS.split(normalizar_para_cotejo(texto_cita))
    limpias = []
    for p in partes:
        p = p.strip().strip("\"'").strip().rstrip(".,;:").strip()
        if p:
            limpias.append(p)
    return limpias


def verificar_cita(cita: Cita, contexto: dict[str, dict], min_caracteres: int = 20) -> CitaVerificada:
    articulo = contexto.get(cita.id)
    if articulo is None:
        return CitaVerificada(cita, False, "el artículo citado no estaba entre los recuperados")
    fragmentos = _fragmentos(cita.texto)
    if not fragmentos:
        return CitaVerificada(cita, False, "cita vacía", articulo)
    if sum(len(f) for f in fragmentos) < min_caracteres:
        return CitaVerificada(cita, False, f"cita demasiado corta (mínimo {min_caracteres} caracteres)", articulo)
    if len(fragmentos) > 1 and any(len(f) < FRAGMENTO_MIN for f in fragmentos):
        return CitaVerificada(cita, False, "fragmento de cita demasiado corto", articulo)

    fuente = normalizar_para_cotejo(articulo["texto"])
    pos = 0
    for frag in fragmentos:
        encontrado = fuente.find(frag, pos)
        if encontrado < 0:
            return CitaVerificada(cita, False, f"no aparece literalmente en el artículo: \"{frag[:60]}\"", articulo)
        pos = encontrado + len(frag)
    return CitaVerificada(cita, True, "literal", articulo)


def verificar_citas(
    citas: list[Cita], recuperados: list[Recuperado], min_caracteres: int = 20
) -> tuple[list[CitaVerificada], list[CitaVerificada]]:
    contexto = {r.id: r.articulo for r in recuperados}
    resultados = [verificar_cita(c, contexto, min_caracteres) for c in citas]
    return [r for r in resultados if r.valida], [r for r in resultados if not r.valida]

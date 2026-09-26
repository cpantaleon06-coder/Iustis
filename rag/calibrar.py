"""Calibración del umbral de similitud a partir del banco de pruebas.

Para cada pregunta del banco se obtiene la similitud coseno del mejor artículo
recuperado. Las preguntas con esperado "responder" deberían quedar arriba del umbral
y las de "abstenerse" abajo. Se propone el umbral que mejor separa ambos grupos
(máximo de aciertos; en empate, el punto medio del hueco más amplio).

Uso:
    python -m rag.calibrar                 (solo muestra la propuesta)
    python -m rag.calibrar --escribir      (la guarda en rag/config.yaml)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from rag.config import RAIZ, cargar_config
from rag.retriever import recuperador_para

BANCO = RAIZ / "tests" / "bench" / "preguntas.yaml"


def mejor_similitud(pregunta: dict, llm=None) -> float:
    consulta = pregunta["pregunta"]
    if llm is not None:
        # Misma consulta que usa el pipeline en producción (reformulación del triaje + original)
        from backend.pipeline import consulta_de_busqueda

        consulta = consulta_de_busqueda(consulta, llm.triaje(consulta))
    if pregunta.get("modulo"):
        modulos = [pregunta["modulo"]]
    else:
        # Pregunta fuera de todo módulo: se busca en todos y se toma el máximo
        from ingestion.ingest import cargar_modulos

        modulos = [k for k, v in cargar_modulos().items() if v.get("activo", True)]
    return max(max((r.similitud for r in recuperador_para(m).buscar(consulta, k=1)), default=0.0) for m in modulos)


def proponer_umbral(puntos: list[tuple[float, bool]]) -> tuple[float, int]:
    """puntos: (similitud, debe_responder). Devuelve (umbral, aciertos)."""
    valores = sorted({s for s, _ in puntos})
    candidatos = [valores[0] - 1e-3] + [(a + b) / 2 for a, b in zip(valores, valores[1:])] + [valores[-1] + 1e-3]
    mejor = None
    for i, u in enumerate(candidatos):
        aciertos = sum((s >= u) == responder for s, responder in puntos)
        hueco = (valores[i] - valores[i - 1]) if 0 < i < len(valores) else 0
        clave = (aciertos, hueco)
        if mejor is None or clave > mejor[0]:
            mejor = (clave, u)
    return round(mejor[1], 3), mejor[0][0]


def escribir_umbral(umbral: float) -> None:
    ruta = RAIZ / "rag" / "config.yaml"
    texto = ruta.read_text(encoding="utf-8")
    texto = re.sub(r"umbral_similitud: [\d.]+", f"umbral_similitud: {umbral}", texto)
    texto = re.sub(r"umbral_calibrado: \w+", "umbral_calibrado: true", texto)
    ruta.write_text(texto, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--banco", type=Path, default=BANCO)
    ap.add_argument("--escribir", action="store_true")
    ap.add_argument("--sin-triaje", action="store_true", help="busca con la pregunta cruda (no llama a Claude)")
    args = ap.parse_args(argv)
    llm = None
    if not args.sin_triaje:
        from backend.llm import crear_llm

        llm = crear_llm()

    preguntas = (yaml.safe_load(args.banco.read_text(encoding="utf-8")) or {}).get("preguntas") or []
    if not preguntas:
        print(f"El banco {args.banco} no tiene preguntas todavía.")
        return 1
    puntos = []
    print(f"{'id':<8}{'esperado':<12}{'similitud':>10}  pregunta")
    for p in sorted(preguntas, key=lambda p: p["id"]):
        s = mejor_similitud(p, llm)
        puntos.append((s, p["esperado"] == "responder"))
        print(f"{p['id']:<8}{p['esperado']:<12}{s:>10.3f}  {p['pregunta'][:60]}")

    if len({r for _, r in puntos}) < 2:
        print("\nSe necesitan preguntas de ambos tipos (responder y abstenerse) para calibrar.")
        return 1
    umbral, aciertos = proponer_umbral(puntos)
    actual = cargar_config()["verificacion"]["umbral_similitud"]
    print(f"\nUmbral propuesto: {umbral} ({aciertos}/{len(puntos)} bien separadas). Umbral actual: {actual}")
    for s, responder in puntos:
        if (s >= umbral) != responder:
            print(f"  mal separada: similitud {s:.3f}, esperado {'responder' if responder else 'abstenerse'}")
    if args.escribir:
        escribir_umbral(umbral)
        print("Guardado en rag/config.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())

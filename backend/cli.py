"""Prueba del asistente desde la terminal, sin canal de mensajería.

Uso:
    python -m backend.cli                        (conversación interactiva)
    python -m backend.cli "me corrieron ayer"    (un solo mensaje)
    python -m backend.cli --traza "..."          (muestra además la traza de decisiones)
"""

from __future__ import annotations

import argparse
import json
import sys

from backend.llm import ClaudeLLM
from backend.pipeline import Pipeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mensaje", nargs="?")
    ap.add_argument("--traza", action="store_true")
    args = ap.parse_args(argv)

    pipeline = Pipeline(ClaudeLLM())

    def atender(texto: str) -> None:
        r = pipeline.procesar("cli", texto)
        print(f"\n[{r.tipo}]\n{r.texto}\n")
        if args.traza:
            print(json.dumps(r.traza, ensure_ascii=False, indent=2))

    if args.mensaje:
        atender(args.mensaje)
        return 0
    print("Escribe tu mensaje (línea vacía para salir).")
    while texto := input("> ").strip():
        atender(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())

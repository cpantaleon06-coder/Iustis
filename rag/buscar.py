"""Inspección manual de la recuperación (útil para revisar el corpus).

Uso:
    python -m rag.buscar --modulo laboral_despido "me corrieron sin darme nada por escrito"
"""

from __future__ import annotations

import argparse
import sys

from rag.config import cargar_config
from rag.retriever import recuperador_para
from rag.verifier import evaluar_recuperacion


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modulo", required=True)
    ap.add_argument("--k", type=int)
    ap.add_argument("consulta")
    args = ap.parse_args(argv)

    config = cargar_config()
    k = args.k or config["recuperacion"]["k"]
    resultados = recuperador_para(args.modulo).buscar(args.consulta, k=k)
    v = config["verificacion"]
    decision = evaluar_recuperacion(resultados, v["umbral_similitud"], v["umbral_calibrado"])

    print(f"{'id':<22}{'coseno':>8}{'bm25':>8}{'rrf':>8}  ubicación / inicio del texto")
    for r in resultados:
        marca = " (ref. explícita)" if r.referencia_explicita else ""
        print(f"{r.id:<22}{r.similitud:>8.3f}{r.bm25:>8.2f}{r.rrf:>8.4f}  {r.articulo['texto'][:70]!r}{marca}")
    estado = "CONTESTAR" if decision.contestar else "ABSTENERSE Y CANALIZAR"
    calibrado = "" if decision.umbral_calibrado else " (umbral SIN CALIBRAR)"
    print(f"\nCompuerta 1: {estado} ({decision.motivo}){calibrado}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

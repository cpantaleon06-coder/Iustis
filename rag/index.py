"""Construcción del índice por módulo.

Uso:
    python -m rag.index --modulo transito_cdmx
    python -m rag.index --todos
    python -m rag.index --probar-embeddings     (verifica llave y modelo de Voyage)

Genera data/index/<modulo>/ con:
    articulos.jsonl  artículos citables (se excluyen derogados e incompletos)
    embeddings.npy   un vector normalizado por artículo, mismo orden
    manifest.json    modelo, huellas de los archivos procesados y excluidos
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from ingestion.ingest import cargar_modulos
from rag.config import DATA, DIR_INDICE, cargar_config
from rag.embeddings import Embedder, crear_embedder


MAX_CARACTERES_FRAGMENTO = 1200


def encabezado_articulo(art: dict) -> str:
    encabezado = f"{art['ley']}, artículo {art['articulo']}"
    if art.get("ubicacion"):
        encabezado += f". {art['ubicacion']}"
    return encabezado


def texto_para_embedding(art: dict) -> str:
    return f"{encabezado_articulo(art)}\n{art['texto']}"


def fragmentos_de_articulo(art: dict, maximo: int = MAX_CARACTERES_FRAGMENTO) -> list[str]:
    """Parte el artículo en fragmentos de párrafos consecutivos, cada uno con el encabezado
    del artículo. Los modelos de embeddings solo leen el inicio de textos largos; así la
    parte final de un artículo largo también queda representada en el índice."""
    parrafos = [p.strip() for p in art["texto"].split("\n\n") if p.strip()] or [art["texto"]]
    fragmentos, actual = [], ""
    for p in parrafos:
        if actual and len(actual) + len(p) > maximo:
            fragmentos.append(actual)
            actual = p
        else:
            actual = f"{actual}\n\n{p}" if actual else p
    if actual:
        fragmentos.append(actual)
    encabezado = encabezado_articulo(art)
    return [f"{encabezado}\n{f}" for f in fragmentos]


def huella(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def construir_indice(
    modulo: str,
    embedder: Embedder,
    modulos: dict | None = None,
    dir_procesado: Path = DATA / "processed",
    dir_indice: Path = DIR_INDICE,
) -> dict:
    modulos = modulos or cargar_modulos()
    conf = modulos[modulo]
    citables, excluidos, huellas = [], [], {}
    for fuente in conf["fuentes"]:
        ruta = dir_procesado / f"{fuente}.jsonl"
        if not ruta.exists():
            raise FileNotFoundError(f"Falta {ruta}. Corre primero: python -m ingestion.ingest --modulo {modulo}")
        huellas[fuente] = huella(ruta)
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            art = json.loads(linea)
            if art["derogado"]:
                excluidos.append({"id": art["id"], "motivo": "derogado"})
            elif art["incompleto"]:
                excluidos.append({"id": art["id"], "motivo": "incompleto: " + "; ".join(art["motivos_incompleto"])})
            else:
                citables.append(art)
    if not citables:
        raise ValueError(f"El módulo {modulo} no tiene artículos citables")

    textos, fragmento_de = [], []
    for i, art in enumerate(citables):
        for fragmento in fragmentos_de_articulo(art):
            textos.append(fragmento)
            fragmento_de.append(i)
    vectores = embedder.documentos(textos)

    destino = dir_indice / modulo
    destino.mkdir(parents=True, exist_ok=True)
    np.save(destino / "embeddings.npy", vectores)
    np.save(destino / "fragmento_de.npy", np.array(fragmento_de, dtype=np.int32))
    with open(destino / "articulos.jsonl", "w", encoding="utf-8") as f:
        for art in citables:
            f.write(json.dumps(art, ensure_ascii=False) + "\n")
    manifest = {
        "modulo": modulo,
        "area": conf["area"],
        "embedder": embedder.nombre,
        "dimension": int(vectores.shape[1]),
        "total_citables": len(citables),
        "total_fragmentos": len(textos),
        "excluidos": excluidos,
        "huellas_procesado": huellas,
        "construido": datetime.now().isoformat(timespec="seconds"),
    }
    (destino / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Construye el índice de recuperación por módulo")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--modulo")
    g.add_argument("--todos", action="store_true")
    g.add_argument("--probar-embeddings", action="store_true")
    args = ap.parse_args(argv)

    config = cargar_config()
    try:
        embedder = crear_embedder(config)
    except RuntimeError as e:
        print(f"ERROR {e}")
        return 1

    if args.probar_embeddings:
        v = embedder.consulta("me despidieron sin darme un aviso por escrito")
        print(f"OK {embedder.nombre}: vector de dimensión {v.shape[0]}")
        return 0

    modulos = cargar_modulos()
    ids = [args.modulo] if args.modulo else [k for k, v in modulos.items() if v.get("activo", True)]
    errores = 0
    for m in ids:
        try:
            man = construir_indice(m, embedder, modulos)
        except (FileNotFoundError, ValueError, KeyError) as e:
            errores += 1
            print(f"[{m}] ERROR {e}")
            continue
        print(f"[{m}] {man['total_citables']} artículos indexados con {man['embedder']}")
        for ex in man["excluidos"]:
            print(f"    excluido {ex['id']}: {ex['motivo']}")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

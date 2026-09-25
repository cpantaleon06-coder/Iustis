"""Ingesta de corpus legal por módulo.

Uso:
    python -m ingestion.ingest --modulo transito_cdmx
    python -m ingestion.ingest --todos

Por cada fuente del módulo lee data/raw/<fuente>.txt y <fuente>.meta.yaml y escribe:
    data/processed/<fuente>.jsonl         (un artículo por línea)
    data/processed/<fuente>.reporte.json  (incompletos, saltos, ruido eliminado, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from ingestion.parser import parsear

RAIZ = Path(__file__).resolve().parents[1]
DATA = RAIZ / "data"
CAMPOS_META = ["ley", "abreviatura", "jurisdiccion", "fuente_url", "fecha_version", "fecha_consulta"]


class ErrorIngesta(Exception):
    pass


def cargar_modulos(ruta: Path = DATA / "modules.yaml") -> dict:
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)["modulos"]


def leer_crudo(ruta: Path) -> tuple[str, str]:
    """Devuelve (texto, codificacion). Intenta UTF-8 y luego Windows-1252."""
    datos = ruta.read_bytes()
    try:
        return datos.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return datos.decode("cp1252"), "cp1252"


def cargar_meta(ruta: Path) -> tuple[dict, list[str]]:
    if not ruta.exists():
        raise ErrorIngesta(f"Falta el archivo de metadata {ruta.name} (copia data/raw/_plantilla.meta.yaml)")
    with open(ruta, encoding="utf-8") as f:
        meta = yaml.safe_load(f) or {}
    faltantes = [c for c in CAMPOS_META if not str(meta.get(c) or "").strip()]
    if faltantes:
        raise ErrorIngesta(f"{ruta.name}: faltan campos obligatorios {faltantes}")
    pendientes = [c for c in CAMPOS_META if str(meta[c]).strip().upper().startswith("PLACEHOLDER")]
    return meta, pendientes


def ingestar_fuente(fuente: str, modulo: str, dir_raw: Path, dir_salida: Path) -> dict:
    ruta_txt = dir_raw / f"{fuente}.txt"
    if not ruta_txt.exists():
        raise ErrorIngesta(f"Falta el texto fuente {ruta_txt} (coloca ahí el texto oficial verificado)")
    meta, pendientes = cargar_meta(dir_raw / f"{fuente}.meta.yaml")
    crudo, codificacion = leer_crudo(ruta_txt)

    res = parsear(
        crudo,
        abreviatura=str(meta["abreviatura"]),
        excluir_transitorios=meta.get("excluir_transitorios", True),
        ruido_regex=meta.get("ruido_regex") or [],
    )
    if not res.articulos:
        raise ErrorIngesta(f"{ruta_txt.name}: no se detectó ningún encabezado de artículo")

    comunes = {
        "modulo": modulo,
        "fuente": fuente,
        "ley": meta["ley"],
        "jurisdiccion": meta["jurisdiccion"],
        "fuente_url": meta["fuente_url"],
        "fecha_version": str(meta["fecha_version"]),
        "fecha_consulta": str(meta["fecha_consulta"]),
    }
    dir_salida.mkdir(parents=True, exist_ok=True)
    with open(dir_salida / f"{fuente}.jsonl", "w", encoding="utf-8") as f:
        for art in res.articulos:
            f.write(json.dumps({**asdict(art), **comunes}, ensure_ascii=False) + "\n")

    reporte = {
        "fuente": fuente,
        "modulo": modulo,
        "sha256_fuente": hashlib.sha256(ruta_txt.read_bytes()).hexdigest(),
        "codificacion": codificacion,
        "metadata_pendiente": pendientes,
        "total_articulos": len(res.articulos),
        "derogados": [a.id for a in res.articulos if a.derogado],
        "incompletos": [
            {"id": a.id, "linea": a.linea_origen, "motivos": a.motivos_incompleto}
            for a in res.articulos
            if a.incompleto
        ],
        "saltos_numeracion": res.saltos_numeracion,
        "duplicados": res.duplicados,
        "encabezados_descartados": res.encabezados_descartados,
        "lineas_ruido_eliminadas": res.lineas_ruido_eliminadas,
        "lineas_transitorios_omitidas": res.lineas_transitorios_omitidas,
        "lineas_previas_al_primer_articulo": res.texto_previo_descartado,
    }
    with open(dir_salida / f"{fuente}.reporte.json", "w", encoding="utf-8") as f:
        json.dump(reporte, f, ensure_ascii=False, indent=2)
    return reporte


def imprimir_reporte(r: dict) -> None:
    print(f"\n[{r['modulo']}] {r['fuente']}: {r['total_articulos']} artículos")
    if r["metadata_pendiente"]:
        print(f"  AVISO metadata con PLACEHOLDER: {', '.join(r['metadata_pendiente'])}")
    if r["codificacion"] != "utf-8":
        print(f"  AVISO el archivo no estaba en UTF-8, se leyó como {r['codificacion']}")
    print(f"  Incompletos: {len(r['incompletos'])}")
    for inc in r["incompletos"]:
        print(f"    - {inc['id']} (línea {inc['linea']}): {'; '.join(inc['motivos'])}")
    if r["derogados"]:
        print(f"  Derogados: {', '.join(r['derogados'])}")
    if r["saltos_numeracion"]:
        saltos = ", ".join(f"{s['despues_de']} a {s['siguiente']}" for s in r["saltos_numeracion"])
        print(f"  Saltos de numeración (normal si la fuente es un extracto): {saltos}")
    if r["duplicados"]:
        print(f"  REVISAR artículos duplicados: {', '.join(r['duplicados'])}")
    for d in r["encabezados_descartados"]:
        print(f"  REVISAR línea {d['linea']} parece encabezado pero se trató como texto ({d['motivo']})")
    print(
        f"  Ruido eliminado: {r['lineas_ruido_eliminadas']} líneas | "
        f"transitorios omitidos: {r['lineas_transitorios_omitidas']} líneas | "
        f"texto antes del primer artículo: {r['lineas_previas_al_primer_articulo']} líneas"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingesta de corpus legal por módulo")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--modulo", help="id del módulo en data/modules.yaml")
    grupo.add_argument("--todos", action="store_true", help="ingesta todos los módulos activos")
    args = ap.parse_args(argv)

    modulos = cargar_modulos()
    if args.modulo:
        if args.modulo not in modulos:
            print(f"Módulo desconocido: {args.modulo}. Disponibles: {', '.join(modulos)}")
            return 2
        seleccion = {args.modulo: modulos[args.modulo]}
    else:
        seleccion = {k: v for k, v in modulos.items() if v.get("activo", True)}

    errores = 0
    for id_mod, conf in seleccion.items():
        for fuente in conf["fuentes"]:
            try:
                imprimir_reporte(ingestar_fuente(fuente, id_mod, DATA / "raw", DATA / "processed"))
            except ErrorIngesta as e:
                errores += 1
                print(f"\n[{id_mod}] ERROR {e}")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

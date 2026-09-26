"""Banco de pruebas del asistente contra el pipeline real (Claude + Voyage).

Uso:
    python -m tests.bench.run_bench --validar          revisa el banco sin llamar a ningún modelo
    python -m tests.bench.run_bench                    corre todas las preguntas
    python -m tests.bench.run_bench --solo L01 T03     corre solo esas preguntas
    python -m tests.bench.run_bench --comparar         compara con la corrida anterior

Cada pregunta cuesta una llamada a Haiku (triaje) y, si no se abstiene antes, una a
Sonnet (respuesta). Los resultados se guardan en tests/bench/resultados/ (no se suben a git).

Además de las métricas, hace una auditoría independiente de la salida: vuelve a
extraer cada cita del texto final que vería el usuario y la coteja contra el corpus.
Una sola cita no literal hace fallar la corrida.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from ingestion.ingest import cargar_modulos
from rag.config import DATA, DIR_INDICE
from rag.texto import normalizar_para_cotejo

DIR_BANCO = Path(__file__).parent
BANCO = DIR_BANCO / "preguntas.yaml"
DIR_RESULTADOS = DIR_BANCO / "resultados"

# Resultados posibles por pregunta
CITA_CORRECTA = "cita_correcta"
CITA_INCORRECTA = "cita_incorrecta"
ABSTENCION_INDEBIDA = "abstencion_indebida"
PIDIO_DATO = "pidio_dato"
ABSTENCION_CORRECTA = "abstencion_correcta"
RESPUESTA_INDEBIDA = "respuesta_indebida"
ERROR = "error"

# Cita tal como la escribe backend/render.py:  "texto"\n  (Ley, art. N, versión F)
CITA_EN_SALIDA = re.compile(r'^  "(?P<texto>.+)"\n  \((?P<ley>.+), art\. (?P<art>.+?), versión [^)]*\)$', re.MULTILINE)


# Banco

def cargar_banco(ruta: Path = BANCO) -> list[dict]:
    return (yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}).get("preguntas") or []


def ids_del_corpus(dir_procesado: Path = DATA / "processed") -> set[str]:
    ids = set()
    for ruta in dir_procesado.glob("*.jsonl"):
        ids |= {json.loads(l)["id"] for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()}
    return ids


def validar_banco(preguntas: list[dict], modulos: dict, ids_corpus: set[str]) -> list[str]:
    errores = []
    vistos = Counter(p.get("id") for p in preguntas)
    for pid, n in vistos.items():
        if n > 1:
            errores.append(f"id repetido: {pid}")
    for p in preguntas:
        pid = p.get("id", "(sin id)")
        for campo in ("id", "pregunta", "esperado"):
            if not p.get(campo):
                errores.append(f"{pid}: falta el campo {campo}")
        if p.get("esperado") not in ("responder", "abstenerse"):
            errores.append(f"{pid}: esperado debe ser responder o abstenerse")
        if p.get("modulo") and p["modulo"] not in modulos:
            errores.append(f"{pid}: módulo desconocido {p['modulo']}")
        citas = p.get("citas_esperadas") or []
        if p.get("esperado") == "responder" and not citas:
            errores.append(f"{pid}: una pregunta que debe responderse necesita citas_esperadas")
        if p.get("esperado") == "abstenerse" and citas:
            errores.append(f"{pid}: una pregunta que debe abstenerse no lleva citas_esperadas")
        for c in citas:
            if ids_corpus and c not in ids_corpus:
                errores.append(f"{pid}: el artículo esperado {c} no existe en el corpus procesado")
    return errores


# Evaluación

def evaluar(pregunta: dict, tipo: str, citas: list[str]) -> str:
    if tipo == "error":
        return ERROR
    if pregunta["esperado"] == "abstenerse":
        return RESPUESTA_INDEBIDA if tipo == "respuesta" else ABSTENCION_CORRECTA
    if tipo == "respuesta":
        return CITA_CORRECTA if set(citas) & set(pregunta.get("citas_esperadas") or []) else CITA_INCORRECTA
    if tipo == "pregunta":
        return PIDIO_DATO
    return ABSTENCION_INDEBIDA


def cargar_corpus_por_etiqueta(dir_indice: Path = DIR_INDICE) -> dict[tuple[str, str], dict]:
    corpus = {}
    for ruta in dir_indice.glob("*/articulos.jsonl"):
        for l in ruta.read_text(encoding="utf-8").splitlines():
            a = json.loads(l)
            corpus[(a["ley"], a["articulo"])] = a
    return corpus


def auditar_salida(texto: str, corpus: dict[tuple[str, str], dict]) -> list[str]:
    """Cotejo independiente de cada cita del texto final. Devuelve las fallas."""
    fallas = []
    for m in CITA_EN_SALIDA.finditer(texto):
        art = corpus.get((m["ley"], m["art"]))
        if art is None:
            fallas.append(f"artículo no encontrado en el corpus: {m['ley']}, art. {m['art']}")
            continue
        fragmentos = re.split(r"\(\s*(?:\.\.\.|…)\s*\)|\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…", normalizar_para_cotejo(m["texto"]))
        fuente = normalizar_para_cotejo(art["texto"])
        for f in (x.strip().strip("\"'").strip().rstrip(".,;:").strip() for x in fragmentos):
            if f and f not in fuente:
                fallas.append(f"cita no literal en {art['id']}: \"{f[:60]}\"")
    return fallas


def correr_pregunta(pipeline, pregunta: dict, corpus: dict) -> dict:
    usuario = f"bench:{pregunta['id']}:{time.time_ns()}"
    inicio = time.perf_counter()
    r = pipeline.procesar(usuario, pregunta["pregunta"])
    if r.tipo == "pregunta" and pregunta.get("seguimiento"):
        r = pipeline.procesar(usuario, pregunta["seguimiento"])
    segundos = time.perf_counter() - inicio
    citas = r.traza.get("citas_validas") or []
    return {
        "id": pregunta["id"],
        "esperado": pregunta["esperado"],
        "citas_esperadas": pregunta.get("citas_esperadas") or [],
        "tipo": r.tipo,
        "resultado": evaluar(pregunta, r.tipo, citas),
        "citas": citas,
        "descartes": r.traza.get("descartes") or [],
        "auditoria": auditar_salida(r.texto, corpus),
        "segundos": round(segundos, 2),
        "area": (r.traza.get("triaje") or {}).get("area"),
        "recuperacion": r.traza.get("recuperacion"),
        "texto": r.texto,
    }


def resumir(detalles: list[dict]) -> dict:
    c = Counter(d["resultado"] for d in detalles)
    responder = [d for d in detalles if d["esperado"] == "responder"]
    abstenerse = [d for d in detalles if d["esperado"] == "abstenerse"]
    tiempos = sorted(d["segundos"] for d in detalles)
    return {
        "total": len(detalles),
        "debian_responder": len(responder),
        "debian_abstenerse": len(abstenerse),
        CITA_CORRECTA: c[CITA_CORRECTA],
        CITA_INCORRECTA: c[CITA_INCORRECTA],
        ABSTENCION_INDEBIDA: c[ABSTENCION_INDEBIDA],
        PIDIO_DATO: c[PIDIO_DATO],
        ABSTENCION_CORRECTA: c[ABSTENCION_CORRECTA],
        RESPUESTA_INDEBIDA: c[RESPUESTA_INDEBIDA],
        ERROR: c[ERROR],
        "citas_sin_respaldo_en_salida": sum(len(d["auditoria"]) for d in detalles),
        "citas_descartadas_por_compuerta": sum(
            1 for d in detalles for x in d["descartes"] if x["seccion"] in ("derechos", "que_hacer") and x["id"]
        ),
        "latencia_mediana_s": tiempos[len(tiempos) // 2] if tiempos else 0,
        "latencia_max_s": tiempos[-1] if tiempos else 0,
    }


def imprimir(detalles: list[dict], resumen: dict, config_verif: dict) -> None:
    print(f"\n{'id':<8}{'esperado':<12}{'obtenido':<12}{'resultado':<22}{'seg':>6}  citas")
    for d in detalles:
        citas = ", ".join(d["citas"]) or "-"
        print(f"{d['id']:<8}{d['esperado']:<12}{d['tipo']:<12}{d['resultado']:<22}{d['segundos']:>6.1f}  {citas}")
        for f in d["auditoria"]:
            print(f"        AUDITORÍA: {f}")
    r = resumen
    print("\nResumen")
    print(f"  Con cita correcta:           {r[CITA_CORRECTA]}/{r['debian_responder']}")
    print(f"  Abstenciones correctas:      {r[ABSTENCION_CORRECTA]}/{r['debian_abstenerse']}")
    print(f"  Citaron otro artículo:       {r[CITA_INCORRECTA]}")
    print(f"  Abstenciones indebidas:      {r[ABSTENCION_INDEBIDA]}")
    print(f"  Pidieron un dato:            {r[PIDIO_DATO]}  (agrega 'seguimiento' en el banco para completarlas)")
    print(f"  Respondieron sin deber:      {r[RESPUESTA_INDEBIDA]}")
    print(f"  Errores:                     {r[ERROR]}")
    print(f"  Citas sin respaldo (salida): {r['citas_sin_respaldo_en_salida']}  (debe ser 0)")
    print(f"  Citas frenadas por compuerta: {r['citas_descartadas_por_compuerta']}")
    print(f"  Latencia mediana / máxima:   {r['latencia_mediana_s']} s / {r['latencia_max_s']} s")
    if not config_verif.get("umbral_calibrado"):
        print(f"  AVISO umbral SIN CALIBRAR ({config_verif.get('umbral_similitud')}); corre: python -m rag.calibrar")


def comparar(actual: dict, anterior: dict) -> None:
    print(f"\nComparación contra {anterior['fecha']}")
    for clave, valor in actual["resumen"].items():
        previo = anterior["resumen"].get(clave)
        if isinstance(valor, (int, float)) and previo is not None and valor != previo:
            print(f"  {clave}: {previo} -> {valor}")
    antes = {d["id"]: d["resultado"] for d in anterior["detalles"]}
    for d in actual["detalles"]:
        if d["id"] in antes and antes[d["id"]] != d["resultado"]:
            print(f"  {d['id']}: {antes[d['id']]} -> {d['resultado']}")


def ultima_corrida() -> dict | None:
    corridas = sorted(DIR_RESULTADOS.glob("*.json"))
    return json.loads(corridas[-1].read_text(encoding="utf-8")) if corridas else None


def main(argv: list[str] | None = None, pipeline=None) -> int:
    ap = argparse.ArgumentParser(description="Banco de pruebas del asistente")
    ap.add_argument("--banco", type=Path, default=BANCO)
    ap.add_argument("--validar", action="store_true", help="solo revisa el banco, sin llamar a modelos")
    ap.add_argument("--solo", nargs="+", metavar="ID")
    ap.add_argument("--comparar", action="store_true", help="compara con la corrida anterior")
    args = ap.parse_args(argv)

    preguntas = cargar_banco(args.banco)
    errores = validar_banco(preguntas, cargar_modulos(), ids_del_corpus())
    if errores:
        print("El banco tiene errores:")
        for e in errores:
            print(f"  - {e}")
        return 2
    if not preguntas:
        print(f"El banco {args.banco} no tiene preguntas todavía.")
        return 1
    if args.validar:
        n_resp = sum(p["esperado"] == "responder" for p in preguntas)
        print(f"Banco válido: {len(preguntas)} preguntas ({n_resp} responder, {len(preguntas) - n_resp} abstenerse).")
        return 0
    if args.solo:
        preguntas = [p for p in preguntas if p["id"] in set(args.solo)]

    from rag.config import cargar_config

    if pipeline is None:
        from backend.llm import crear_llm
        from backend.pipeline import Pipeline

        pipeline = Pipeline(crear_llm())
    corpus = cargar_corpus_por_etiqueta()
    anterior = ultima_corrida() if args.comparar else None

    detalles = []
    for i, p in enumerate(preguntas, start=1):
        print(f"[{i}/{len(preguntas)}] {p['id']}...", end=" ", flush=True)
        d = correr_pregunta(pipeline, p, corpus)
        print(d["resultado"])
        detalles.append(d)

    resumen = resumir(detalles)
    config = cargar_config()
    imprimir(detalles, resumen, config["verificacion"])
    corrida = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "config": {"verificacion": config["verificacion"], "recuperacion": config["recuperacion"]},
        "resumen": resumen,
        "detalles": detalles,
    }
    DIR_RESULTADOS.mkdir(parents=True, exist_ok=True)
    destino = DIR_RESULTADOS / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    destino.write_text(json.dumps(corrida, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResultados guardados en {destino}")
    if anterior:
        comparar(corrida, anterior)
    # Falla la corrida si hubo citas sin respaldo o respuestas donde debía abstenerse
    return 1 if resumen["citas_sin_respaldo_en_salida"] or resumen[RESPUESTA_INDEBIDA] else 0


if __name__ == "__main__":
    sys.exit(main())

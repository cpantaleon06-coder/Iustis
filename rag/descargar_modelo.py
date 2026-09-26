"""Descarga el modelo de embeddings a modelos/<nombre>/, reanudando si la conexión se corta.

Uso:
    python -m rag.descargar_modelo                       (el modelo de rag/config.yaml)
    python -m rag.descargar_modelo intfloat/multilingual-e5-small

Existe porque en algunas redes la descarga normal de Hugging Face se corta a la mitad
y vuelve a empezar desde cero; aquí cada archivo se reanuda desde el último byte
recibido y se verifica que el tamaño final coincida con el publicado.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

from rag.config import RAIZ, cargar_config

DIR_MODELOS = RAIZ / "modelos"
BASE = "https://huggingface.co"
# Archivos que necesita sentence-transformers; se omiten pesos duplicados en otros formatos
EXTENSIONES = (".json", ".txt", ".model", ".safetensors")
OMITIR = ("onnx/", "openvino/", "pytorch_model.bin", "tf_model", "flax_model")
MAX_INTENTOS = 30
TAMANO_BLOQUE = 1 << 20  # 1 MB


def dir_modelo(nombre: str) -> Path:
    return DIR_MODELOS / nombre.split("/")[-1]


def archivos_del_modelo(nombre: str, http: httpx.Client) -> list[str]:
    r = http.get(f"{BASE}/api/models/{nombre}")
    r.raise_for_status()
    archivos = [s["rfilename"] for s in r.json()["siblings"]]
    return [a for a in archivos if a.endswith(EXTENSIONES) and not a.startswith(OMITIR) and not any(o in a for o in OMITIR)]


def descargar_archivo(url: str, destino: Path, http: httpx.Client) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    total = int(http.head(url, follow_redirects=True).headers.get("content-length", 0))
    if destino.exists() and total and destino.stat().st_size == total:
        return
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    for intento in range(1, MAX_INTENTOS + 1):
        inicio = parcial.stat().st_size if parcial.exists() else 0
        if total and inicio >= total:
            break
        cabeceras = {"Range": f"bytes={inicio}-"} if inicio else {}
        try:
            with http.stream("GET", url, headers=cabeceras, follow_redirects=True) as r:
                if r.status_code not in (200, 206):
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                if inicio and r.status_code == 200:
                    inicio = 0  # el servidor no aceptó reanudar: se empieza de nuevo
                modo = "ab" if inicio else "wb"
                siguiente_aviso = inicio + 50_000_000
                with open(parcial, modo) as f:
                    for bloque in r.iter_bytes(TAMANO_BLOQUE):
                        f.write(bloque)
                        if total and f.tell() >= siguiente_aviso:
                            print(f"  {destino.name}: {f.tell() / 1e6:,.0f} / {total / 1e6:,.0f} MB", flush=True)
                            siguiente_aviso += 50_000_000
        except (httpx.HTTPError, OSError) as e:
            recibido = parcial.stat().st_size if parcial.exists() else 0
            print(f"  corte en {recibido / 1e6:,.0f} MB ({type(e).__name__}); reanudando (intento {intento})", flush=True)
            time.sleep(min(2 * intento, 15))
            continue
        if not total or parcial.stat().st_size >= total:
            break
    tamano = parcial.stat().st_size if parcial.exists() else 0
    if total and tamano != total:
        raise RuntimeError(f"{destino.name}: se recibieron {tamano} de {total} bytes")
    parcial.replace(destino)


def descargar(nombre: str) -> Path:
    destino = dir_modelo(nombre)
    with httpx.Client(timeout=httpx.Timeout(60, read=120)) as http:
        for archivo in archivos_del_modelo(nombre, http):
            print(f"{archivo}", flush=True)
            descargar_archivo(f"{BASE}/{nombre}/resolve/main/{archivo}", destino / archivo, http)
    print(f"Modelo listo en {destino}")
    return destino


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    nombre = argv[0] if argv else cargar_config()["embeddings"]["modelo"]
    descargar(nombre)
    return 0


if __name__ == "__main__":
    sys.exit(main())

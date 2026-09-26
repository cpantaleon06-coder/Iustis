"""Recuperación híbrida por módulo: embeddings (semántica) + BM25 (léxica), fusionadas con RRF.

Por qué híbrida: la gente describe su problema en lenguaje coloquial ("me corrieron"),
donde gana la búsqueda semántica, pero también usa términos exactos ("artículo 48",
"corralón", "finiquito"), donde los embeddings fallan y BM25 acierta. RRF combina
ambos rankings sin tener que calibrar sus escalas entre sí.

La similitud coseno densa se conserva por separado porque es la señal que usa la
compuerta de verificación (el puntaje RRF no está calibrado y no sirve como umbral).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from ingestion.ingest import areas_de, cargar_modulos
from rag.config import DATA, DIR_INDICE, cargar_config
from rag.embeddings import Embedder, crear_embedder
from rag.index import huella, texto_para_embedding
from rag.texto import sin_acentos, tokenizar

REFERENCIA_ARTICULO = re.compile(
    r"\bart(?:iculo|\.)?s?\s*(\d+)(?:\s*(?:o|º|°)\.?)?"
    r"(?:\s*-\s*([a-z])\b)?"
    r"(?:\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)\b)?"
)


class IndiceDesactualizado(RuntimeError):
    pass


@dataclass
class Recuperado:
    articulo: dict
    similitud: float  # coseno denso contra la consulta
    bm25: float
    rrf: float
    referencia_explicita: bool = False

    @property
    def id(self) -> str:
        return self.articulo["id"]


class Recuperador:
    def __init__(
        self,
        modulo: str,
        embedder: Embedder,
        dir_indice: Path = DIR_INDICE,
        dir_procesado: Path = DATA / "processed",
        rrf_k: int = 60,
    ):
        base = dir_indice / modulo
        if not (base / "manifest.json").exists():
            raise FileNotFoundError(f"No hay índice para {modulo}. Corre: python -m rag.index --modulo {modulo}")
        self.manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest["embedder"] != embedder.nombre:
            raise IndiceDesactualizado(
                f"El índice de {modulo} se construyó con {self.manifest['embedder']} "
                f"pero la consulta usa {embedder.nombre}. Reconstruye el índice."
            )
        for fuente, h in self.manifest["huellas_procesado"].items():
            ruta = dir_procesado / f"{fuente}.jsonl"
            if not ruta.exists() or huella(ruta) != h:
                raise IndiceDesactualizado(
                    f"El corpus de {fuente} cambió desde que se indexó. Corre: python -m rag.index --modulo {modulo}"
                )

        self.modulo = modulo
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.articulos = [json.loads(l) for l in (base / "articulos.jsonl").read_text(encoding="utf-8").splitlines()]
        self.vectores = np.load(base / "embeddings.npy")
        # Índice por fragmentos: la similitud de un artículo es la de su mejor fragmento
        ruta_fragmentos = base / "fragmento_de.npy"
        self.fragmento_de = np.load(ruta_fragmentos) if ruta_fragmentos.exists() else np.arange(len(self.articulos))
        self.bm25 = BM25Okapi([tokenizar(texto_para_embedding(a)) for a in self.articulos])
        self._por_clave = {self._clave(a["numero_base"], a["sufijo"]): i for i, a in enumerate(self.articulos)}

    @staticmethod
    def _clave(num: int, suf: str | None) -> str:
        return f"{num}-{suf}" if suf else str(num)

    def referencias_explicitas(self, consulta: str) -> list[int]:
        """Índices de artículos que la consulta menciona por número ("artículo 48", "art. 3o")."""
        encontrados = []
        for m in REFERENCIA_ARTICULO.finditer(sin_acentos(consulta.lower())):
            suf = "-".join(x for x in (m.group(2), m.group(3)) if x) or None
            i = self._por_clave.get(self._clave(int(m.group(1)), suf))
            if i is not None and i not in encontrados:
                encontrados.append(i)
        return encontrados

    def buscar(self, consulta: str, k: int = 5) -> list[Recuperado]:
        por_fragmento = self.vectores @ self.embedder.consulta(consulta)
        similitudes = np.full(len(self.articulos), -1.0, dtype=np.float32)
        np.maximum.at(similitudes, self.fragmento_de, por_fragmento)
        puntajes_bm25 = self.bm25.get_scores(tokenizar(consulta))

        rrf = np.zeros(len(self.articulos))
        for rango, i in enumerate(np.argsort(-similitudes), start=1):
            rrf[i] += 1 / (self.rrf_k + rango)
        for rango, i in enumerate(np.argsort(-puntajes_bm25), start=1):
            if puntajes_bm25[i] <= 0:
                break
            rrf[i] += 1 / (self.rrf_k + rango)

        explicitos = self.referencias_explicitas(consulta)
        orden = explicitos + [int(i) for i in np.argsort(-rrf) if int(i) not in explicitos]
        elegidos = orden[: max(k, len(explicitos))]
        vecino = self._siguiente_del_capitulo(elegidos[0]) if elegidos and not explicitos else None
        if vecino is not None and vecino not in elegidos and len(elegidos) > 1:
            # Los códigos agrupan reglas relacionadas en artículos contiguos (el 47 enumera
            # causas de rescisión, el 48 dice qué puede pedir el trabajador): el artículo
            # siguiente al mejor resultado, si es del mismo capítulo, ocupa el último lugar.
            elegidos[-1] = vecino
        return [
            Recuperado(
                articulo=self.articulos[i],
                similitud=float(similitudes[i]),
                bm25=float(puntajes_bm25[i]),
                rrf=float(rrf[i]),
                referencia_explicita=i in explicitos,
            )
            for i in elegidos
        ]

    def _siguiente_del_capitulo(self, i: int) -> int | None:
        if i + 1 >= len(self.articulos):
            return None
        actual, siguiente = self.articulos[i], self.articulos[i + 1]
        if actual.get("ubicacion") and siguiente.get("ubicacion") == actual.get("ubicacion"):
            return i + 1
        return None


_cache: dict[str, Recuperador] = {}


def recuperador_para(modulo: str) -> Recuperador:
    if modulo not in _cache:
        config = cargar_config()
        _cache[modulo] = Recuperador(modulo, crear_embedder(config), rrf_k=config["recuperacion"]["rrf_k"])
    return _cache[modulo]


def modulos_por_area(area: str) -> list[str]:
    return [k for k, v in cargar_modulos().items() if v.get("activo", True) and area in areas_de(v)]

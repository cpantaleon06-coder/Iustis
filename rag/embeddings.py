"""Proveedores de embeddings.

VoyageEmbedder es el proveedor real. EmbedderSimulado existe solo para pruebas
automatizadas: aproxima similitud por traslape de palabras y no debe usarse en el demo.
"""

from __future__ import annotations

import hashlib
import os
from typing import Protocol

import numpy as np

from rag.texto import tokenizar


class Embedder(Protocol):
    nombre: str

    def documentos(self, textos: list[str]) -> np.ndarray: ...

    def consulta(self, texto: str) -> np.ndarray: ...


def _normalizar(m: np.ndarray) -> np.ndarray:
    normas = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(normas == 0, 1, normas)


class VoyageEmbedder:
    LOTE = 64

    def __init__(self, modelo: str):
        import voyageai

        clave = os.getenv("VOYAGE_API_KEY")
        if not clave:
            raise RuntimeError("Falta VOYAGE_API_KEY en .env")
        self.cliente = voyageai.Client(api_key=clave, max_retries=3)
        self.modelo = modelo
        self.nombre = f"voyage:{modelo}"

    def _embed(self, textos: list[str], tipo: str) -> np.ndarray:
        vectores = []
        for i in range(0, len(textos), self.LOTE):
            r = self.cliente.embed(textos[i : i + self.LOTE], model=self.modelo, input_type=tipo)
            vectores.extend(r.embeddings)
        return _normalizar(np.array(vectores, dtype=np.float32))

    def documentos(self, textos: list[str]) -> np.ndarray:
        return self._embed(textos, "document")

    def consulta(self, texto: str) -> np.ndarray:
        return self._embed([texto], "query")[0]


class EmbedderLocal:
    """Modelo multilingüe local (sin costo ni llave). Los modelos E5 esperan los
    prefijos "passage: " para documentos y "query: " para consultas."""

    def __init__(self, modelo: str):
        from sentence_transformers import SentenceTransformer

        from rag.descargar_modelo import dir_modelo

        # Si el modelo se bajó con python -m rag.descargar_modelo, se usa esa copia local
        local = dir_modelo(modelo)
        self.modelo = SentenceTransformer(str(local) if (local / "config.json").exists() else modelo)
        self.nombre = f"local:{modelo}"
        self._prefijos = ("passage: ", "query: ") if "e5" in modelo.lower() else ("", "")

    def documentos(self, textos: list[str]) -> np.ndarray:
        return self.modelo.encode(
            [self._prefijos[0] + t for t in textos],
            batch_size=16,
            normalize_embeddings=True,
            show_progress_bar=True,
            convert_to_numpy=True,
        ).astype(np.float32)

    def consulta(self, texto: str) -> np.ndarray:
        return self.modelo.encode(self._prefijos[1] + texto, normalize_embeddings=True, convert_to_numpy=True).astype(
            np.float32
        )


class EmbedderSimulado:
    """Bolsa de palabras con hashing. Solo para tests."""

    nombre = "simulado"
    DIM = 512

    def _vector(self, texto: str) -> np.ndarray:
        v = np.zeros(self.DIM, dtype=np.float32)
        for tok in tokenizar(texto):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.DIM] += 1
        return v

    def documentos(self, textos: list[str]) -> np.ndarray:
        return _normalizar(np.array([self._vector(t) for t in textos]))

    def consulta(self, texto: str) -> np.ndarray:
        return _normalizar(self._vector(texto))


def crear_embedder(config: dict) -> Embedder:
    conf = config["embeddings"]
    if conf["proveedor"] == "voyage":
        return VoyageEmbedder(conf["modelo"])
    if conf["proveedor"] == "local":
        return EmbedderLocal(conf["modelo"])
    if conf["proveedor"] == "simulado":
        return EmbedderSimulado()
    raise ValueError(f"Proveedor de embeddings desconocido: {conf['proveedor']}")

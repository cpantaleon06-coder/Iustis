import shutil
from pathlib import Path

import pytest

from ingestion.ingest import ingestar_fuente
from rag.embeddings import EmbedderSimulado
from rag.index import construir_indice
from rag.retriever import Recuperador

FIXTURES = Path(__file__).parent / "fixtures"
MODULOS_NAVES = {"naves": {"nombre": "Naves", "area": "transito", "fuentes": ["naves"]}}


@pytest.fixture
def corpus_naves(tmp_path):
    """Corpus ficticio ingestado e indexado con embeddings simulados."""
    raw, proc, idx = tmp_path / "raw", tmp_path / "processed", tmp_path / "index"
    raw.mkdir()
    shutil.copy(FIXTURES / "naves.txt", raw / "naves.txt")
    (raw / "naves.meta.yaml").write_text(
        "ley: Ley Ficticia de Naves y Robots\nabreviatura: LFN\njurisdiccion: Ficticia\n"
        "fuente_url: ninguna\nfecha_version: '2000-01-01'\nfecha_consulta: '2026-09-23'\n",
        encoding="utf-8",
    )
    ingestar_fuente("naves", "naves", raw, proc)
    manifest = construir_indice("naves", EmbedderSimulado(), MODULOS_NAVES, proc, idx)
    return {
        "manifest": manifest,
        "proc": proc,
        "idx": idx,
        "rec": Recuperador("naves", EmbedderSimulado(), idx, proc),
    }

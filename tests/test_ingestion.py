"""Pruebas del parser de ingesta con una ley ficticia (tests/fixtures/ficticia.txt)."""

import json
import shutil
from pathlib import Path

import pytest

from ingestion.ingest import ErrorIngesta, ingestar_fuente
from ingestion.parser import parsear

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def resultado():
    crudo = (FIXTURES / "ficticia.txt").read_text(encoding="utf-8")
    return parsear(crudo, abreviatura="LFP")


@pytest.fixture(scope="module")
def por_id(resultado):
    return {a.id: a for a in resultado.articulos}


def test_ids_y_orden(resultado):
    assert [a.id for a in resultado.articulos] == [
        "LFP-1", "LFP-2", "LFP-2-bis", "LFP-3", "LFP-4", "LFP-5", "LFP-8", "LFP-9",
    ]


def test_texto_multilinea_y_nota_de_reforma(por_id):
    art = por_id["LFP-1"]
    assert art.texto == (
        "Este texto ficticio sirve únicamente para probar el parser.\n"
        "Tiene una segunda línea que forma parte del mismo artículo."
    )
    assert art.notas_reforma == ["Artículo reformado DOF 01-01-2000"]
    assert not art.incompleto


def test_ubicacion_estructural(por_id):
    assert por_id["LFP-1"].ubicacion == (
        "TÍTULO PRIMERO (Disposiciones ficticias) > CAPÍTULO I (De los objetos de prueba)"
    )
    assert por_id["LFP-4"].ubicacion.endswith("CAPÍTULO II (De los casos problemáticos)")


def test_remision_al_inicio_de_linea_no_parte_el_articulo(resultado, por_id):
    assert "Artículo 1. Esta línea es una remisión" in por_id["LFP-2"].texto
    assert resultado.encabezados_descartados[0]["texto"].startswith("Artículo 1.")


def test_ruido_de_pagina_eliminado(resultado, por_id):
    assert resultado.lineas_ruido_eliminadas == 1
    assert "Página" not in por_id["LFP-2"].texto


def test_sufijo_bis(por_id):
    art = por_id["LFP-2-bis"]
    assert art.articulo == "2 Bis" and art.sufijo == "bis"


def test_derogado_no_se_marca_incompleto(por_id):
    assert por_id["LFP-3"].derogado
    assert not por_id["LFP-3"].incompleto


def test_marcas_de_incompletitud(por_id):
    assert any("puntos suspensivos" in m for m in por_id["LFP-4"].motivos_incompleto)
    assert any("truncamiento" in m for m in por_id["LFP-5"].motivos_incompleto)
    assert any("manualmente" in m for m in por_id["LFP-8"].motivos_incompleto)
    assert por_id["LFP-9"].motivos_incompleto == ["sin texto en la fuente"]


def test_texto_incompleto_se_conserva_sin_completar(por_id):
    # El parser nunca rellena: el texto queda exactamente como en la fuente
    assert por_id["LFP-5"].texto == "Este artículo ficticio termina de forma abrupta y no tiene"


def test_salto_de_numeracion_reportado(resultado):
    assert resultado.saltos_numeracion == [{"despues_de": 5, "siguiente": 8}]


def test_transitorios_excluidos(resultado):
    assert resultado.lineas_transitorios_omitidas == 1
    assert all("transitorio" not in a.texto.lower() for a in resultado.articulos)


def test_ingesta_completa_con_metadata(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    shutil.copy(FIXTURES / "ficticia.txt", raw / "ficticia.txt")
    (raw / "ficticia.meta.yaml").write_text(
        "ley: Ley Ficticia de Pruebas\nabreviatura: LFP\njurisdiccion: Ficticia\n"
        "fuente_url: PLACEHOLDER\nfecha_version: '2000-01-01'\nfecha_consulta: '2026-09-23'\n",
        encoding="utf-8",
    )
    reporte = ingestar_fuente("ficticia", "modulo_prueba", raw, tmp_path / "out")
    assert reporte["total_articulos"] == 8
    assert reporte["metadata_pendiente"] == ["fuente_url"]
    assert len(reporte["incompletos"]) == 4

    lineas = (tmp_path / "out" / "ficticia.jsonl").read_text(encoding="utf-8").splitlines()
    primero = json.loads(lineas[0])
    assert primero["ley"] == "Ley Ficticia de Pruebas"
    assert primero["fecha_version"] == "2000-01-01"
    assert primero["modulo"] == "modulo_prueba"


def test_metadata_faltante_detiene_la_ingesta(tmp_path):
    shutil.copy(FIXTURES / "ficticia.txt", tmp_path / "ficticia.txt")
    (tmp_path / "ficticia.meta.yaml").write_text("ley: Algo\n", encoding="utf-8")
    with pytest.raises(ErrorIngesta, match="faltan campos"):
        ingestar_fuente("ficticia", "m", tmp_path, tmp_path / "out")


def test_texto_en_cp1252(tmp_path):
    (tmp_path / "f.txt").write_bytes("Artículo 1.- Texto con acentos: órgano.".encode("cp1252"))
    (tmp_path / "f.meta.yaml").write_text(
        "ley: L\nabreviatura: L\njurisdiccion: J\nfuente_url: U\nfecha_version: F\nfecha_consulta: C\n",
        encoding="utf-8",
    )
    reporte = ingestar_fuente("f", "m", tmp_path, tmp_path / "out")
    assert reporte["codificacion"] == "cp1252"
    art = json.loads((tmp_path / "out" / "f.jsonl").read_text(encoding="utf-8"))
    assert art["texto"] == "Texto con acentos: órgano."

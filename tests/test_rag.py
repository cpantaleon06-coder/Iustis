"""Pruebas de índice, recuperación y verificación con un corpus ficticio y embeddings simulados."""

import pytest

from rag.calibrar import proponer_umbral
from rag.embeddings import EmbedderSimulado
from rag.retriever import IndiceDesactualizado, Recuperador
from rag.verifier import Cita, evaluar_recuperacion, verificar_cita, verificar_citas

@pytest.fixture
def entorno(corpus_naves):
    return corpus_naves


# Índice

def test_indice_excluye_derogados_e_incompletos(entorno):
    m = entorno["manifest"]
    assert m["total_citables"] == 5
    assert {e["id"]: e["motivo"].split(":")[0] for e in m["excluidos"]} == {"LFN-6": "derogado", "LFN-7": "incompleto"}


def test_indice_desactualizado_si_cambia_el_corpus(entorno):
    ruta = entorno["proc"] / "naves.jsonl"
    ruta.write_text(ruta.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(IndiceDesactualizado, match="cambió"):
        Recuperador("naves", EmbedderSimulado(), entorno["idx"], entorno["proc"])


def test_indice_con_otro_embedder_se_rechaza(entorno):
    class Otro(EmbedderSimulado):
        nombre = "otro"

    with pytest.raises(IndiceDesactualizado, match="se construyó con"):
        Recuperador("naves", Otro(), entorno["idx"], entorno["proc"])


# Recuperación

def test_recupera_por_tema(entorno):
    rec = entorno["rec"]
    assert rec.buscar("soy un robot y me despidieron de la estación orbital sin aviso escrito")[0].id == "LFN-3"
    assert rec.buscar("cuánto es la multa por estacionar mi nave")[0].id == "LFN-2"


def test_referencia_explicita_va_primero(entorno):
    r = entorno["rec"].buscar("¿qué dice el artículo 4?")
    assert r[0].id == "LFN-4" and r[0].referencia_explicita
    r = entorno["rec"].buscar("y el art. 5 bis?")
    assert r[0].id == "LFN-5-bis"


def test_referencia_a_articulo_inexistente_no_inventa(entorno):
    r = entorno["rec"].buscar("¿qué dice el artículo 99?")
    assert not any(x.referencia_explicita for x in r)


# Compuerta 1

def test_compuerta_abstiene_fuera_de_tema(entorno):
    r = entorno["rec"].buscar("receta de mole poblano con chocolate")
    d = evaluar_recuperacion(r, umbral=0.3)
    assert not d.contestar and "no alcanza el umbral" in d.motivo
    assert d.contexto == []


def test_compuerta_contesta_en_tema(entorno):
    d = evaluar_recuperacion(entorno["rec"].buscar("multa por estacionar nave espacial"), umbral=0.3)
    assert d.contestar and d.contexto


def test_compuerta_referencia_explicita_pasa(entorno):
    d = evaluar_recuperacion(entorno["rec"].buscar("artículo 4"), umbral=0.99)
    assert d.contestar


def test_compuerta_sin_resultados():
    assert not evaluar_recuperacion([], umbral=0.1).contestar


# Compuerta 2

ART = {"id": "LFN-3", "texto": "Todo robot despedido de una estación orbital tiene derecho a recibir\nun aviso escrito con la causa del despido."}
CTX = {"LFN-3": ART}


@pytest.mark.parametrize(
    "texto",
    [
        "tiene derecho a recibir un aviso escrito con la causa del despido",
        "“tiene derecho a recibir un aviso escrito con la causa del despido.”",
        "Todo robot despedido (...) tiene derecho a recibir un aviso escrito",
        "Todo robot despedido … un aviso escrito con la causa del despido",
    ],
)
def test_cita_literal_valida(texto):
    assert verificar_cita(Cita("LFN-3", texto), CTX).valida


@pytest.mark.parametrize(
    "cita, motivo",
    [
        (Cita("LFN-3", "tiene derecho a recibir una indemnización por el despido"), "no aparece literalmente"),
        (Cita("LFN-3", "Tiene Derecho A Recibir Un Aviso Escrito"), "no aparece literalmente"),
        (Cita("LFN-9", "tiene derecho a recibir un aviso escrito"), "no estaba entre los recuperados"),
        (Cita("LFN-3", "aviso escrito"), "demasiado corta"),
        (Cita("LFN-3", "un aviso escrito con la causa (...) Todo robot despedido"), "no aparece literalmente"),
        (Cita("LFN-3", "Todo robot despedido de una estación (...) aviso"), "fragmento de cita demasiado corto"),
    ],
)
def test_cita_invalida(cita, motivo):
    r = verificar_cita(cita, CTX)
    assert not r.valida and motivo in r.motivo


def test_verificar_citas_separa_validas_y_rechazadas(entorno):
    recuperados = entorno["rec"].buscar("robot despedido")
    validas, rechazadas = verificar_citas(
        [Cita("LFN-3", "tiene derecho a recibir un aviso escrito"), Cita("LFN-3", "tiene derecho a cinco lunas de indemnización")],
        recuperados,
    )
    assert len(validas) == 1 and len(rechazadas) == 1


# Calibración

def test_proponer_umbral_separa_grupos():
    puntos = [(0.82, True), (0.75, True), (0.70, True), (0.41, False), (0.35, False), (0.30, False)]
    umbral, aciertos = proponer_umbral(puntos)
    assert aciertos == 6 and 0.41 < umbral < 0.70


def test_referencia_explicita_con_letra():
    from rag.retriever import REFERENCIA_ARTICULO
    from rag.texto import sin_acentos

    m = REFERENCIA_ARTICULO.search(sin_acentos("¿qué dice el artículo 153-A?".lower()))
    assert m.group(1) == "153" and m.group(2) == "a"


def test_fragmentos_cubren_todo_el_articulo_y_llevan_encabezado():
    from rag.index import fragmentos_de_articulo

    parrafos = [f"Párrafo ficticio {i} " + "x" * 500 for i in range(6)]
    art = {"ley": "Ley Ficticia", "articulo": "47", "ubicacion": "CAPITULO IV", "texto": "\n\n".join(parrafos)}
    frags = fragmentos_de_articulo(art, maximo=1200)
    assert len(frags) == 3
    assert all(f.startswith("Ley Ficticia, artículo 47. CAPITULO IV\n") for f in frags)
    assert all(any(p in f for f in frags) for p in parrafos)  # ningún párrafo se pierde


def test_similitud_de_articulo_es_la_de_su_mejor_fragmento(corpus_naves):
    rec = corpus_naves["rec"]
    assert len(rec.fragmento_de) >= len(rec.articulos)
    r = rec.buscar("robot despedido estación orbital aviso escrito")[0]
    assert r.id == "LFN-3" and r.similitud > 0


def test_incluye_el_siguiente_articulo_del_mismo_capitulo(corpus_naves):
    rec = corpus_naves["rec"]
    ids = [r.id for r in rec.buscar("soy un robot y me despidieron de la estación orbital sin aviso escrito", k=3)]
    # LFN-3 es el mejor resultado; LFN-4 (mismo capítulo, siguiente) debe estar en el contexto
    assert ids[0] == "LFN-3" and "LFN-4" in ids and len(ids) == 3

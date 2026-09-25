"""Pruebas del banco de pruebas (evaluación, validación, auditoría y corrida con pipeline simulado)."""

import json

import pytest

from backend.esquemas import CitaModelo, Derecho
from backend.pipeline import Resultado
from backend.render import RespuestaVerificada, render_respuesta
from tests.bench import run_bench as rb

ART = {"id": "LFN-3", "ley": "Ley Ficticia de Naves y Robots", "articulo": "3", "fecha_version": "2000-01-01",
       "texto": "Todo robot despedido de una estación orbital tiene derecho a recibir un aviso escrito con la causa del despido."}
ART_BIS = {**ART, "id": "LFN-5-bis", "articulo": "5 Bis", "texto": "Los pilotos deberán portar licencia de vuelo intergaláctico vigente."}
CORPUS = {(a["ley"], a["articulo"]): a for a in (ART, ART_BIS)}


def texto_con_cita(cita: str, art=ART) -> str:
    r = RespuestaVerificada(
        que_esta_pasando="x",
        derechos=[(Derecho(explicacion="y", cita=CitaModelo(articulo_id=art["id"], texto_literal=cita)), art)],
        que_hacer=[], instituciones=[], cuando_abogado="z",
    )
    return render_respuesta(r)


# Auditoría (acoplada al formato de backend/render.py a propósito)

def test_auditoria_acepta_cita_literal():
    assert rb.auditar_salida(texto_con_cita("tiene derecho a recibir un aviso escrito"), CORPUS) == []


def test_auditoria_acepta_fragmentos_y_sufijo_bis():
    assert rb.auditar_salida(texto_con_cita("Todo robot despedido (...) un aviso escrito"), CORPUS) == []
    assert rb.auditar_salida(texto_con_cita("deberán portar licencia de vuelo", ART_BIS), CORPUS) == []


def test_auditoria_detecta_cita_alterada():
    fallas = rb.auditar_salida(texto_con_cita("tiene derecho a una indemnización"), CORPUS)
    assert len(fallas) == 1 and "no literal" in fallas[0]


def test_auditoria_detecta_articulo_ajeno():
    otro = {**ART, "ley": "Ley Inexistente"}
    fallas = rb.auditar_salida(texto_con_cita("tiene derecho a recibir un aviso escrito", otro), CORPUS)
    assert "no encontrado" in fallas[0]


def test_auditoria_encuentra_la_cita_en_el_texto():
    # Si render.py cambia su formato y la regex deja de encontrar citas, esta prueba lo detecta
    assert len(list(rb.CITA_EN_SALIDA.finditer(texto_con_cita("tiene derecho a recibir un aviso escrito")))) == 1


# Evaluación

@pytest.mark.parametrize("esperado, tipo, citas, resultado", [
    ("responder", "respuesta", ["LFN-3"], rb.CITA_CORRECTA),
    ("responder", "respuesta", ["LFN-3", "LFN-4"], rb.CITA_CORRECTA),
    ("responder", "respuesta", ["LFN-4"], rb.CITA_INCORRECTA),
    ("responder", "abstencion", [], rb.ABSTENCION_INDEBIDA),
    ("responder", "pregunta", [], rb.PIDIO_DATO),
    ("responder", "error", [], rb.ERROR),
    ("abstenerse", "abstencion", [], rb.ABSTENCION_CORRECTA),
    ("abstenerse", "no_legal", [], rb.ABSTENCION_CORRECTA),
    ("abstenerse", "respuesta", ["LFN-3"], rb.RESPUESTA_INDEBIDA),
])
def test_evaluar(esperado, tipo, citas, resultado):
    assert rb.evaluar({"esperado": esperado, "citas_esperadas": ["LFN-3"]}, tipo, citas) == resultado


# Validación del banco

def test_validar_banco_detecta_errores():
    preguntas = [
        {"id": "A", "pregunta": "p", "esperado": "responder", "citas_esperadas": ["LFN-3"]},
        {"id": "A", "pregunta": "p", "esperado": "responder", "citas_esperadas": ["LFN-99"]},
        {"id": "B", "pregunta": "p", "esperado": "quizas"},
        {"id": "C", "pregunta": "p", "esperado": "responder", "modulo": "inexistente"},
        {"id": "D", "pregunta": "p", "esperado": "abstenerse", "citas_esperadas": ["LFN-3"]},
    ]
    errores = rb.validar_banco(preguntas, {"naves": {}}, {"LFN-3"})
    texto = "\n".join(errores)
    assert "id repetido: A" in texto
    assert "LFN-99 no existe" in texto
    assert "B: esperado debe ser" in texto
    assert "C: módulo desconocido" in texto
    assert "C: una pregunta que debe responderse necesita citas_esperadas" in texto
    assert "D: una pregunta que debe abstenerse no lleva" in texto


def test_validar_banco_correcto():
    p = [{"id": "A", "pregunta": "p", "esperado": "responder", "citas_esperadas": ["LFN-3"], "modulo": "naves"}]
    assert rb.validar_banco(p, {"naves": {}}, {"LFN-3"}) == []


# Corrida completa con pipeline simulado

class PipelineFalso:
    """Responde según el texto: 'robot' contesta con cita, 'dato' pide un dato, lo demás se abstiene."""

    def __init__(self, cita="tiene derecho a recibir un aviso escrito"):
        self.cita = cita
        self.llamadas = []

    def procesar(self, usuario, mensaje):
        self.llamadas.append(mensaje)
        if "dato" in mensaje:
            return Resultado("pregunta", "¿Cuándo?", {})
        if "robot" in mensaje or "ayer" in mensaje:
            return Resultado("respuesta", texto_con_cita(self.cita), {"citas_validas": ["LFN-3"], "descartes": []})
        return Resultado("abstencion", "no puedo", {})


BANCO = """
preguntas:
  - {id: R1, pregunta: "soy robot y me despidieron", esperado: responder, citas_esperadas: [LFN-3]}
  - {id: R2, pregunta: "te doy un dato", esperado: responder, citas_esperadas: [LFN-3], seguimiento: "fue ayer"}
  - {id: X1, pregunta: "receta de mole", esperado: abstenerse}
"""


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    banco = tmp_path / "preguntas.yaml"
    banco.write_text(BANCO, encoding="utf-8")
    monkeypatch.setattr(rb, "DIR_RESULTADOS", tmp_path / "resultados")
    monkeypatch.setattr(rb, "cargar_corpus_por_etiqueta", lambda: CORPUS)
    monkeypatch.setattr(rb, "ids_del_corpus", lambda: {"LFN-3"})
    monkeypatch.setattr(rb, "cargar_modulos", lambda: {})
    return banco, tmp_path / "resultados"


def test_corrida_completa(entorno, capsys):
    banco, dir_res = entorno
    p = PipelineFalso()
    assert rb.main(["--banco", str(banco)], pipeline=p) == 0
    assert p.llamadas == ["soy robot y me despidieron", "te doy un dato", "fue ayer", "receta de mole"]
    corrida = json.loads(next(dir_res.glob("*.json")).read_text(encoding="utf-8"))
    r = corrida["resumen"]
    assert (r["cita_correcta"], r["abstencion_correcta"], r["citas_sin_respaldo_en_salida"]) == (2, 1, 0)
    assert "Con cita correcta:           2/2" in capsys.readouterr().out


def test_corrida_falla_si_la_salida_trae_cita_no_literal(entorno):
    banco, _ = entorno
    assert rb.main(["--banco", str(banco)], pipeline=PipelineFalso(cita="tiene derecho a cinco lunas de indemnización")) == 1


def test_validar_no_llama_al_pipeline(entorno):
    banco, _ = entorno
    p = PipelineFalso()
    assert rb.main(["--banco", str(banco), "--validar"], pipeline=p) == 0
    assert p.llamadas == []


def test_comparar_con_corrida_anterior(entorno, capsys):
    banco, _ = entorno
    rb.main(["--banco", str(banco)], pipeline=PipelineFalso())
    capsys.readouterr()

    class PeorPipeline(PipelineFalso):
        def procesar(self, usuario, mensaje):
            return Resultado("abstencion", "no puedo", {})

    import time
    time.sleep(1.1)  # los archivos de resultados se nombran por segundo
    rb.main(["--banco", str(banco), "--comparar"], pipeline=PeorPipeline())
    salida = capsys.readouterr().out
    assert "R1: cita_correcta -> abstencion_indebida" in salida


def test_banco_vacio_del_repositorio_es_valido_pero_no_corre():
    assert rb.main(["--validar"]) == 1  # el banco real aún no tiene preguntas

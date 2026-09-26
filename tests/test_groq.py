"""Pruebas del cliente de Groq sin red (transporte simulado)."""

import json

import httpx
import pytest

from backend.esquemas import RespuestaGenerada, Triaje
from backend.llm import ErrorLLM, GroqLLM, crear_llm, duracion_a_segundos, esquema_estricto

TRIAJE_OK = {
    "es_consulta_legal": True, "area": "laboral", "urgencia": "media", "motivo_urgencia": "x",
    "hechos": ["h"], "datos_faltantes": [], "consulta_reformulada": "c", "datos_laborales": None,
}


def respuesta_groq(contenido: dict | str, finish="stop", status=200, headers=None):
    texto = contenido if isinstance(contenido, str) else json.dumps(contenido)
    return httpx.Response(status, headers=headers or {}, json={"choices": [{"finish_reason": finish, "message": {"content": texto}}]})


def cliente(respuestas, monkeypatch, vistos=None):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_prueba")
    cola = list(respuestas)

    def manejador(req):
        if vistos is not None:
            vistos.append(req)
        return cola.pop(0)

    esperas = []
    llm = GroqLLM(httpx.Client(transport=httpx.MockTransport(manejador)), dormir=esperas.append)
    return llm, esperas


def _objetos(nodo):
    if isinstance(nodo, dict):
        if nodo.get("type") == "object" and "properties" in nodo:
            yield nodo
        for v in nodo.values():
            yield from _objetos(v)
    elif isinstance(nodo, list):
        for v in nodo:
            yield from _objetos(v)


@pytest.mark.parametrize("modelo", [Triaje, RespuestaGenerada])
def test_esquema_estricto(modelo):
    esquema = esquema_estricto(modelo)
    objetos = list(_objetos(esquema))
    assert objetos
    for o in objetos:
        assert o["additionalProperties"] is False
        assert set(o["required"]) == set(o["properties"])
    assert '"title"' not in json.dumps(esquema)


def test_triaje_envia_esquema_y_valida(monkeypatch):
    vistos = []
    llm, _ = cliente([respuesta_groq(TRIAJE_OK)], monkeypatch, vistos)
    t = llm.triaje("me despidieron")
    assert t.area == "laboral"
    cuerpo = json.loads(vistos[0].content)
    assert vistos[0].headers["Authorization"] == "Bearer gsk_prueba"
    assert cuerpo["model"] == "openai/gpt-oss-20b"
    assert cuerpo["response_format"]["json_schema"]["strict"] is True
    assert "<fecha_hoy>" in cuerpo["messages"][1]["content"]


def test_reintenta_tras_429_respetando_retry_after(monkeypatch):
    llm, esperas = cliente(
        [httpx.Response(429, headers={"retry-after": "7"}, json={}), respuesta_groq(TRIAJE_OK)], monkeypatch
    )
    assert llm.triaje("x").area == "laboral"
    assert esperas == [7.5]


def test_limite_persistente_es_error_controlado(monkeypatch):
    llm, esperas = cliente([httpx.Response(429, headers={"retry-after": "999"}, json={})] * 4, monkeypatch)
    with pytest.raises(ErrorLLM, match="429"):
        llm.triaje("x")
    assert esperas == [60, 60, 60]  # la espera se acota a MAX_ESPERA_S y se rinde tras MAX_ESPERAS


def test_json_que_no_cumple_esquema_se_reintenta(monkeypatch):
    llm, _ = cliente([respuesta_groq({"area": "laboral"}), respuesta_groq(TRIAJE_OK)], monkeypatch)
    assert llm.triaje("x").area == "laboral"


def test_respuesta_truncada(monkeypatch):
    llm, _ = cliente([respuesta_groq(TRIAJE_OK, finish="length")], monkeypatch)
    with pytest.raises(ErrorLLM, match="incompleta"):
        llm.triaje("x")


def test_error_400_no_se_reintenta(monkeypatch):
    llm, _ = cliente([httpx.Response(400, text="modelo inexistente")], monkeypatch)
    with pytest.raises(ErrorLLM, match="400"):
        llm.triaje("x")


def test_articulos_largos_se_acotan(monkeypatch):
    vistos = []
    generada = {"que_esta_pasando": "a", "derechos": [], "que_hacer": [], "instituciones": [],
                "cuando_abogado": "b", "sin_respaldo": []}
    llm, _ = cliente([respuesta_groq(generada)], monkeypatch, vistos)
    llm.max_caracteres_articulo = 50
    art = {"id": "X-1", "ley": "L", "articulo": "1", "fecha_version": "f", "texto": "a" * 500}
    llm.generar("m", Triaje(**TRIAJE_OK), [art], [])
    contenido = json.loads(vistos[0].content)["messages"][1]["content"]
    assert "a" * 50 + "\n\n[...]" in contenido and "a" * 51 not in contenido


def test_sin_llave(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ErrorLLM, match="GROQ_API_KEY"):
        GroqLLM()


def test_crear_llm_por_defecto_es_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_prueba")
    monkeypatch.delenv("LLM_PROVEEDOR", raising=False)
    assert isinstance(crear_llm(), GroqLLM)


@pytest.mark.parametrize("valor, segundos", [("7", 7.0), ("40.897s", 40.897), ("1m2.5s", 62.5), ("350ms", 0.35), (None, None), ("x", None)])
def test_duracion_a_segundos(valor, segundos):
    assert duracion_a_segundos(valor) == (pytest.approx(segundos) if segundos is not None else None)


GENERADA = {"que_esta_pasando": "a", "derechos": [], "que_hacer": [], "instituciones": [],
            "cuando_abogado": "b", "sin_respaldo": []}


def test_generacion_usa_respaldo_si_la_espera_es_larga(monkeypatch):
    vistos = []
    llm, esperas = cliente(
        [httpx.Response(429, headers={"x-ratelimit-reset-tokens": "40.9s"}, json={}), respuesta_groq(GENERADA)],
        monkeypatch, vistos,
    )
    llm.generar("m", Triaje(**TRIAJE_OK), [], [])
    modelos = [json.loads(v.content)["model"] for v in vistos]
    assert modelos == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert esperas == []  # no esperó: cambió de modelo de inmediato


def test_generacion_espera_si_la_espera_es_corta(monkeypatch):
    vistos = []
    llm, esperas = cliente(
        [httpx.Response(429, headers={"x-ratelimit-reset-tokens": "3s"}, json={}), respuesta_groq(GENERADA)],
        monkeypatch, vistos,
    )
    llm.generar("m", Triaje(**TRIAJE_OK), [], [])
    assert [json.loads(v.content)["model"] for v in vistos] == ["openai/gpt-oss-120b"] * 2
    assert esperas == [3.5]


def test_acotar_articulo_conserva_parrafos_relevantes_literales():
    from backend.llm import acotar_articulo

    texto = (
        "Son causas de rescisión ficticias:\n\nI.\nPrimera causa sobre engaños.\n\n"
        "II.\nSegunda causa sobre violencia.\n\n"
        "El patrón deberá dar aviso escrito al trabajador de la fecha y causa."
    )
    acotado = acotar_articulo(texto, "no me dieron aviso escrito", max_caracteres=110)
    assert acotado.startswith("Son causas de rescisión ficticias:")
    assert "El patrón deberá dar aviso escrito al trabajador de la fecha y causa." in acotado
    assert "[...]" in acotado and "violencia" not in acotado
    # cada fragmento conservado es literal del original
    assert all(f.strip() in texto for f in acotado.split("[...]") if f.strip())
    assert acotar_articulo("corto", "x", 100) == "corto"


def test_espera_por_saturacion_no_gasta_reintentos_por_json_invalido(monkeypatch):
    """Un 429 no debe consumir el presupuesto de reintentos por contenido: si después de
    esperar el modelo devuelve JSON inválido, todavía debe poder reintentar."""
    json_malo = httpx.Response(400, text='{"error":{"code":"json_validate_failed"}}')
    llm, esperas = cliente(
        [httpx.Response(429, headers={"retry-after": "1"}, json={})] * 2
        + [json_malo, json_malo, respuesta_groq(TRIAJE_OK)],
        monkeypatch,
    )
    assert llm.triaje("x").area == "laboral"
    assert esperas == [1.5, 1.5]


def test_json_invalido_persistente_falla_con_mensaje_claro(monkeypatch):
    json_malo = httpx.Response(400, text='{"error":{"code":"json_validate_failed"}}')
    llm, _ = cliente([json_malo] * 3, monkeypatch)
    with pytest.raises(ErrorLLM, match="json_validate_failed"):
        llm.triaje("x")

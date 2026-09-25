"""Pruebas del canal de WhatsApp (OpenWA), voz (Groq) y chat web, sin red."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as servidor
from backend.canales import openwa, voz
from backend.canales.openwa import ClienteOpenWA, Deduplicador, firma_valida, interpretar, partir
from backend.pipeline import Resultado

SECRETO = "secreto-de-prueba"


def firmar(cuerpo: bytes, secreto: str = SECRETO) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def evento(**data):
    base = {"id": "true_521@c.us_ABC", "from": "521@c.us", "chatId": "521@c.us", "body": "me despidieron",
            "type": "text", "fromMe": False, "isGroup": False}
    return {"event": "message.received", "data": {**base, **data}}


# Firma

def test_firma_valida():
    cuerpo = b'{"a":1}'
    assert firma_valida(cuerpo, firmar(cuerpo), SECRETO)
    assert not firma_valida(cuerpo, firmar(cuerpo, "otro"), SECRETO)
    assert not firma_valida(cuerpo + b" ", firmar(cuerpo), SECRETO)
    assert not firma_valida(cuerpo, None, SECRETO)
    assert not firma_valida(cuerpo, firmar(cuerpo), "")


# Interpretación del payload

def test_interpretar_texto():
    m = interpretar(evento())
    assert (m.tipo, m.chat_id, m.texto) == ("texto", "521@c.us", "me despidieron")


@pytest.mark.parametrize("payload", [
    evento(fromMe=True),
    evento(isGroup=True),
    {"event": "session.status", "data": {}},
    {"event": "message.received", "data": {"body": "sin id"}},
])
def test_interpretar_ignora(payload):
    assert interpretar(payload) is None


def test_interpretar_voz_con_media_incluida():
    m = interpretar(evento(type="ptt", body="", metadata={"media": {"mimetype": "audio/ogg; codecs=opus", "data": "QUJD"}}))
    assert m.tipo == "voz" and m.media_base64 == "QUJD"


def test_interpretar_voz_con_media_omitida():
    m = interpretar(evento(type="ptt", metadata={"media": {"mimetype": "audio/ogg", "omitted": True}}))
    assert m.tipo == "voz" and m.media_base64 is None


def test_interpretar_imagen_es_otro():
    assert interpretar(evento(type="image", metadata={"media": {"mimetype": "image/jpeg"}})).tipo == "otro"


# Partición de mensajes

def test_partir_respeta_parrafos_y_limite():
    parrafos = [f"*Sección {i}*\n" + "x" * 400 for i in range(8)]
    texto = "\n\n".join(parrafos)
    partes = partir(texto, limite=1000)
    assert len(partes) > 1
    assert all(len(p) <= 1000 for p in partes)
    assert "\n\n".join(partes) == texto  # no se pierde ni se altera nada


def test_partir_parrafo_gigante():
    texto = "\n".join("linea " * 20 for _ in range(50))
    partes = partir(texto, limite=500)
    assert all(len(p) <= 500 for p in partes)


def test_partir_texto_corto():
    assert partir("hola") == ["hola"]


def test_deduplicador():
    d = Deduplicador(capacidad=2)
    assert d.es_nuevo("a") and not d.es_nuevo("a")
    d.es_nuevo("b"); d.es_nuevo("c")
    assert d.es_nuevo("a")  # expulsado por capacidad


# Cliente de OpenWA

def test_cliente_envia_con_api_key(monkeypatch):
    monkeypatch.setenv("OPENWA_URL", "http://wa:2785")
    monkeypatch.setenv("OPENWA_SESSION_ID", "s1")
    monkeypatch.setenv("OPENWA_API_KEY", "k1")
    peticiones = []

    def manejador(req):
        peticiones.append(req)
        return httpx.Response(201, json={"messageId": "x"})

    c = ClienteOpenWA(httpx.Client(transport=httpx.MockTransport(manejador)))
    c.enviar_texto("521@c.us", "hola")
    req = peticiones[0]
    assert str(req.url) == "http://wa:2785/api/sessions/s1/messages/send-text"
    assert req.headers["X-API-Key"] == "k1"
    assert json.loads(req.content) == {"chatId": "521@c.us", "text": "hola"}


def test_cliente_descarga_media_json(monkeypatch):
    monkeypatch.setenv("OPENWA_SESSION_ID", "s1")
    transporte = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"mimetype": "audio/ogg", "data": base64.b64encode(b"audio").decode()})
    )
    datos, tipo = ClienteOpenWA(httpx.Client(transport=transporte)).descargar_media("521@c.us", "m1")
    assert datos == b"audio" and tipo == "audio/ogg"


# Groq

def test_transcribir_envia_formulario(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    vista = {}

    def manejador(req):
        vista["auth"] = req.headers["Authorization"]
        vista["cuerpo"] = req.content
        return httpx.Response(200, json={"text": " me corrieron ayer "})

    texto = voz.transcribir(b"OggS...", "audio/ogg; codecs=opus", httpx.Client(transport=httpx.MockTransport(manejador)))
    assert texto == "me corrieron ayer"
    assert vista["auth"] == "Bearer g1"
    assert b'name="language"' in vista["cuerpo"] and b"es" in vista["cuerpo"]
    assert b'filename="nota.ogg"' in vista["cuerpo"]


def test_transcribir_errores(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(voz.ErrorVoz, match="GROQ_API_KEY"):
        voz.transcribir(b"x", "audio/ogg")
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    with pytest.raises(voz.ErrorVoz, match="no soportado"):
        voz.transcribir(b"x", "video/mp4")


# Servidor

class PipelineFalso:
    def __init__(self):
        self.llamadas = []

    def procesar(self, usuario, mensaje):
        self.llamadas.append((usuario, mensaje))
        return Resultado("respuesta", f"respuesta a: {mensaje}", {})


class OpenWAFalso:
    def __init__(self):
        self.enviados = []

    def enviar_texto(self, chat_id, texto):
        self.enviados.append((chat_id, texto))

    def descargar_media(self, chat_id, mensaje_id):
        return b"audio", "audio/ogg"


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("OPENWA_WEBHOOK_SECRET", SECRETO)
    p, wa = PipelineFalso(), OpenWAFalso()
    monkeypatch.setitem(servidor._estado, "pipeline", p)
    monkeypatch.setitem(servidor._estado, "openwa", wa)
    monkeypatch.setattr(servidor, "deduplicador", Deduplicador())
    return TestClient(servidor.app), p, wa


def post_webhook(tc, payload, firma=None):
    cuerpo = json.dumps(payload).encode()
    return tc.post("/webhook/openwa", content=cuerpo,
                   headers={"X-Webhook-Signature": firma or firmar(cuerpo), "Content-Type": "application/json"})


def test_webhook_texto_responde_por_whatsapp(cliente):
    tc, p, wa = cliente
    r = post_webhook(tc, evento())
    assert r.status_code == 200 and r.json()["atendido"]
    assert p.llamadas == [("521@c.us", "me despidieron")]
    assert wa.enviados == [("521@c.us", "respuesta a: me despidieron")]


def test_webhook_firma_invalida(cliente):
    tc, p, wa = cliente
    assert post_webhook(tc, evento(), firma="sha256=00").status_code == 401
    assert p.llamadas == [] and wa.enviados == []


def test_webhook_sin_secreto_configurado(cliente, monkeypatch):
    tc, _, _ = cliente
    monkeypatch.setenv("OPENWA_WEBHOOK_SECRET", "")
    assert post_webhook(tc, evento()).status_code == 503


def test_webhook_duplicado_se_atiende_una_vez(cliente):
    tc, p, _ = cliente
    post_webhook(tc, evento())
    r = post_webhook(tc, evento())
    assert not r.json()["atendido"] and len(p.llamadas) == 1


def test_webhook_voz_transcribe_y_muestra_lo_escuchado(cliente, monkeypatch):
    tc, p, wa = cliente
    monkeypatch.setattr(servidor, "transcribir", lambda audio, tipo: "me corrieron ayer")
    post_webhook(tc, evento(type="ptt", body="", metadata={"media": {"mimetype": "audio/ogg", "omitted": True}}))
    assert p.llamadas == [("521@c.us", "me corrieron ayer")]
    assert wa.enviados[0][1].startswith("_Escuché: «me corrieron ayer»_")


def test_webhook_voz_fallida_pide_texto(cliente, monkeypatch):
    tc, p, wa = cliente

    def falla(audio, tipo):
        raise voz.ErrorVoz("ruido")

    monkeypatch.setattr(servidor, "transcribir", falla)
    post_webhook(tc, evento(type="ptt", metadata={"media": {"mimetype": "audio/ogg", "data": "QUJD"}}))
    assert p.llamadas == [] and wa.enviados == [("521@c.us", servidor.AVISO_VOZ_FALLIDA)]


def test_webhook_imagen_avisa(cliente):
    tc, p, wa = cliente
    post_webhook(tc, evento(type="image"))
    assert p.llamadas == [] and wa.enviados == [("521@c.us", servidor.AVISO_TIPO_NO_SOPORTADO)]


def test_webhook_error_del_pipeline_envia_aviso(cliente):
    tc, p, wa = cliente

    def explota(u, m):
        raise RuntimeError("boom")

    p.procesar = explota
    post_webhook(tc, evento())
    assert wa.enviados == [("521@c.us", servidor.AVISO_ERROR)]


def test_api_chat_web(cliente):
    tc, p, _ = cliente
    r = tc.post("/api/chat", json={"sesion": "abcdefgh1234", "mensaje": "hola"})
    assert r.json() == {"tipo": "respuesta", "texto": "respuesta a: hola"}
    assert p.llamadas == [("web:abcdefgh1234", "hola")]
    assert tc.post("/api/chat", json={"sesion": "x", "mensaje": "hola"}).status_code == 422
    assert tc.post("/api/chat", json={"sesion": "abcdefgh1234", "mensaje": "x" * 2001}).status_code == 422


def test_api_chat_voz(cliente, monkeypatch):
    tc, p, _ = cliente
    monkeypatch.setattr(servidor, "transcribir", lambda audio, tipo: "se llevaron mi carro")
    r = tc.post("/api/chat/voz", data={"sesion": "abcdefgh1234"}, files={"audio": ("n.webm", b"...", "audio/webm")})
    assert r.json()["transcripcion"] == "se llevaron mi carro"
    assert p.llamadas == [("web:abcdefgh1234", "se llevaron mi carro")]


def test_pagina_de_chat(cliente):
    tc, _, _ = cliente
    r = tc.get("/")
    assert r.status_code == 200 and "Primeros Auxilios Legales" in r.text

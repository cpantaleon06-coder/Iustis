"""Servidor: webhook de WhatsApp (OpenWA) y chat web de respaldo.

    uvicorn backend.main:app --port 8000

Rutas:
    GET  /                 chat web para el demo
    POST /api/chat         {"sesion": "...", "mensaje": "..."} del chat web
    POST /api/chat/voz     nota de voz del chat web (multipart: sesion, audio)
    POST /webhook/openwa   webhook de OpenWA (firmado con HMAC)
    GET  /salud            estado del servicio
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.canales.openwa import ClienteOpenWA, Deduplicador, MensajeEntrante, firma_valida, interpretar
from backend.canales.voz import ErrorVoz, transcribir
from backend.limites import desde_entorno
from backend.pipeline import Pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("servidor")

ESTATICOS = Path(__file__).parent / "static"
MAX_CARACTERES = 2000

AVISO_TIPO_NO_SOPORTADO = "Por ahora solo puedo leer mensajes de texto y notas de voz. Cuéntame tu caso con palabras."
AVISO_VOZ_FALLIDA = "No pude entender tu nota de voz. ¿Me lo puedes escribir?"
AVISO_ERROR = "Tuve un problema técnico. Intenta de nuevo en unos minutos."
AVISO_LIMITE = (
    "Recibí muchos mensajes seguidos. Para cuidar el servicio, espera un rato antes de escribir de nuevo. "
    "Si tu caso es urgente, acude directamente a las instituciones que te indiqué."
)

app = FastAPI(title="Iustis", description="Iustis da los Primeros Auxilios Legales y te ayuda a saber dónde acudir")
_estado: dict = {"pipeline": None, "openwa": None}
deduplicador = Deduplicador()
# Cada mensaje cuesta llamadas a Claude: límites por usuario y por IP (ventana de una hora)
limite_usuario = desde_entorno("LIMITE_MENSAJES_USUARIO_HORA", 20)
limite_ip = desde_entorno("LIMITE_MENSAJES_IP_HORA", 60)


def ip_cliente(request: Request) -> str:
    # Detrás de ngrok todas las peticiones llegan desde 127.0.0.1; la IP real viene en X-Forwarded-For
    reenviada = request.headers.get("X-Forwarded-For", "")
    return reenviada.split(",")[0].strip() or (request.client.host if request.client else "desconocida")


def verificar_limite_web(usuario: str, request: Request) -> None:
    if not limite_ip.permitir(ip_cliente(request)) or not limite_usuario.permitir(usuario):
        raise HTTPException(429, AVISO_LIMITE)


def pipeline() -> Pipeline:
    if _estado["pipeline"] is None:
        from backend.llm import crear_llm

        try:
            _estado["pipeline"] = Pipeline(crear_llm())
        except Exception as e:  # llaves faltantes, índice sin construir, etc.
            log.error("No se pudo iniciar el pipeline: %s", e)
            raise HTTPException(503, f"Servicio no disponible: {e}") from e
    return _estado["pipeline"]


def openwa() -> ClienteOpenWA:
    if _estado["openwa"] is None:
        _estado["openwa"] = ClienteOpenWA()
    return _estado["openwa"]


def seudonimo(usuario: str) -> str:
    """Identificador para logs sin exponer el número de teléfono."""
    return hashlib.sha256(usuario.encode()).hexdigest()[:10]


def registrar(usuario: str, resultado) -> None:
    traza = resultado.traza
    log.info(
        "usuario=%s tipo=%s area=%s citas=%s descartes=%d",
        seudonimo(usuario),
        resultado.tipo,
        (traza.get("triaje") or {}).get("area"),
        traza.get("citas_validas"),
        len(traza.get("descartes") or []),
    )


# Chat web

class MensajeWeb(BaseModel):
    sesion: str = Field(min_length=8, max_length=64)
    mensaje: str = Field(min_length=1, max_length=MAX_CARACTERES)


@app.get("/")
def chat_web():
    return FileResponse(ESTATICOS / "chat.html")


@app.post("/api/chat")
def api_chat(m: MensajeWeb, request: Request):
    usuario = f"web:{m.sesion}"
    verificar_limite_web(usuario, request)
    r = pipeline().procesar(usuario, m.mensaje)
    registrar(usuario, r)
    return {"tipo": r.tipo, "texto": r.texto}


@app.post("/api/chat/voz")
def api_chat_voz(request: Request, sesion: str = Form(min_length=8, max_length=64), audio: UploadFile = File(...)):
    verificar_limite_web(f"web:{sesion}", request)
    try:
        texto = transcribir(audio.file.read(), audio.content_type or "")
    except ErrorVoz as e:
        log.warning("voz web: %s", e)
        return {"tipo": "error", "texto": AVISO_VOZ_FALLIDA, "transcripcion": None}
    usuario = f"web:{sesion}"
    r = pipeline().procesar(usuario, texto[:MAX_CARACTERES])
    registrar(usuario, r)
    return {"tipo": r.tipo, "texto": r.texto, "transcripcion": texto}


# WhatsApp (OpenWA)

@app.post("/webhook/openwa")
async def webhook_openwa(request: Request, tareas: BackgroundTasks):
    secreto = os.getenv("OPENWA_WEBHOOK_SECRET", "")
    if not secreto:
        raise HTTPException(503, "OPENWA_WEBHOOK_SECRET no está configurado")
    cuerpo = await request.body()
    if not firma_valida(cuerpo, request.headers.get("X-Webhook-Signature"), secreto):
        raise HTTPException(401, "firma inválida")
    try:
        mensaje = interpretar(json.loads(cuerpo))
    except (ValueError, AttributeError):
        raise HTTPException(400, "payload inválido")
    if mensaje is None or not deduplicador.es_nuevo(mensaje.id):
        return {"ok": True, "atendido": False}
    # Se responde de inmediato para que OpenWA no reintente; el caso se procesa aparte
    tareas.add_task(atender_whatsapp, mensaje)
    return {"ok": True, "atendido": True}


def texto_de_voz(m: MensajeEntrante) -> str:
    if m.media_base64:
        audio, mimetype = base64.b64decode(m.media_base64), m.mimetype
    else:
        audio, mimetype = openwa().descargar_media(m.chat_id, m.id)
        mimetype = m.mimetype or mimetype
    return transcribir(audio, mimetype or "audio/ogg")


def atender_whatsapp(m: MensajeEntrante) -> None:
    cliente = openwa()
    try:
        if not limite_usuario.permitir(m.chat_id):
            cliente.enviar_texto(m.chat_id, AVISO_LIMITE)
            return
        if m.tipo == "otro":
            cliente.enviar_texto(m.chat_id, AVISO_TIPO_NO_SOPORTADO)
            return
        prefijo = ""
        if m.tipo == "voz":
            try:
                texto = texto_de_voz(m)
            except ErrorVoz as e:
                log.warning("voz whatsapp: %s", e)
                cliente.enviar_texto(m.chat_id, AVISO_VOZ_FALLIDA)
                return
            prefijo = f"_Escuché: «{texto}»_\n\n"
        else:
            texto = m.texto
        if not texto:
            return
        r = pipeline().procesar(m.chat_id, texto[:MAX_CARACTERES])
        registrar(m.chat_id, r)
        cliente.enviar_texto(m.chat_id, prefijo + r.texto)
    except Exception:
        log.exception("Error atendiendo a %s", seudonimo(m.chat_id))
        try:
            cliente.enviar_texto(m.chat_id, AVISO_ERROR)
        except Exception:
            log.exception("Tampoco se pudo enviar el aviso de error")


@app.get("/salud")
def salud():
    from rag.config import cargar_config

    v = cargar_config()["verificacion"]
    return {
        "ok": True,
        "llm": os.getenv("LLM_PROVEEDOR", "groq"),
        "embeddings": cargar_config()["embeddings"],
        "llaves": {
            k: bool(os.getenv(k))
            for k in ["GROQ_API_KEY", "OPENWA_API_KEY", "OPENWA_WEBHOOK_SECRET"]
            + (["ANTHROPIC_API_KEY"] if os.getenv("LLM_PROVEEDOR") == "anthropic" else [])
        },
        "umbral_similitud": v["umbral_similitud"],
        "umbral_calibrado": v["umbral_calibrado"],
    }

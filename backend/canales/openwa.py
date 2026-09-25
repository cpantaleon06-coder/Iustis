"""Canal de WhatsApp mediante OpenWA (https://www.open-wa.org), gateway REST autoalojado.

Flujo: OpenWA recibe el mensaje, lo manda por webhook (firmado con HMAC) a
/webhook/openwa, el servidor responde de inmediato y procesa en segundo plano, y la
respuesta se envía con POST /api/sessions/{id}/messages/send-text.

Registro del webhook (una vez, con la URL pública de ngrok):
    python -m backend.canales.openwa --registrar-webhook https://XXXX.ngrok-free.app/webhook/openwa
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import sys
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from rag.config import RAIZ  # noqa: F401  (carga .env)

LIMITE_MENSAJE = 1500  # caracteres por mensaje de WhatsApp para que se lea cómodo en el teléfono
TIPOS_TEXTO = {"text", "chat"}
TIPOS_VOZ = {"ptt", "audio", "voice"}


def firma_valida(cuerpo: bytes, firma: str | None, secreto: str) -> bool:
    """Header X-Webhook-Signature: "sha256=<hex>" (HMAC-SHA256 del cuerpo crudo)."""
    if not firma or not secreto:
        return False
    esperado = "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, firma.strip())


@dataclass
class MensajeEntrante:
    id: str
    chat_id: str
    tipo: str  # texto | voz | otro
    texto: str
    media_base64: str | None
    mimetype: str | None


def interpretar(payload: dict) -> MensajeEntrante | None:
    """Devuelve el mensaje si hay que atenderlo; None para eventos que se ignoran
    (otros eventos, mensajes propios, grupos)."""
    if payload.get("event") != "message.received":
        return None
    d = payload.get("data") or {}
    if d.get("fromMe") or d.get("isGroup"):
        return None
    chat_id = d.get("chatId") or d.get("from")
    if not chat_id or not d.get("id"):
        return None
    tipo_wa = (d.get("type") or "").lower()
    media = ((d.get("metadata") or {}).get("media")) or {}
    mimetype = media.get("mimetype")
    if tipo_wa in TIPOS_TEXTO:
        tipo = "texto"
    elif tipo_wa in TIPOS_VOZ or (mimetype or "").startswith("audio/"):
        tipo = "voz"
    else:
        tipo = "otro"
    return MensajeEntrante(
        id=d["id"],
        chat_id=chat_id,
        tipo=tipo,
        texto=(d.get("body") or "").strip(),
        media_base64=None if media.get("omitted") else media.get("data"),
        mimetype=mimetype,
    )


def partir(texto: str, limite: int = LIMITE_MENSAJE) -> list[str]:
    """Parte en mensajes sin cortar párrafos (una sección nunca queda a la mitad si cabe)."""
    partes, actual = [], ""
    for parrafo in texto.split("\n\n"):
        candidato = f"{actual}\n\n{parrafo}" if actual else parrafo
        if len(candidato) <= limite:
            actual = candidato
            continue
        if actual:
            partes.append(actual)
        while len(parrafo) > limite:  # párrafo más largo que el límite: se corta por líneas
            corte = parrafo.rfind("\n", 0, limite)
            corte = corte if corte > 0 else limite
            partes.append(parrafo[:corte])
            parrafo = parrafo[corte:].lstrip("\n")
        actual = parrafo
    if actual:
        partes.append(actual)
    return partes


class Deduplicador:
    """OpenWA reintenta webhooks; evita contestar dos veces el mismo mensaje."""

    def __init__(self, capacidad: int = 2000):
        self.capacidad = capacidad
        self._vistos: OrderedDict[str, None] = OrderedDict()

    def es_nuevo(self, mensaje_id: str) -> bool:
        if mensaje_id in self._vistos:
            return False
        self._vistos[mensaje_id] = None
        if len(self._vistos) > self.capacidad:
            self._vistos.popitem(last=False)
        return True


class ClienteOpenWA:
    def __init__(self, cliente: httpx.Client | None = None):
        self.base = os.getenv("OPENWA_URL", "http://localhost:2785").rstrip("/")
        self.sesion = os.getenv("OPENWA_SESSION_ID", "")
        clave = os.getenv("OPENWA_API_KEY", "")
        self.http = cliente or httpx.Client(timeout=30)
        self.headers = {"X-API-Key": clave}

    def _url(self, ruta: str) -> str:
        return f"{self.base}/api/sessions/{self.sesion}{ruta}"

    def enviar_texto(self, chat_id: str, texto: str) -> None:
        for parte in partir(texto):
            r = self.http.post(self._url("/messages/send-text"), headers=self.headers, json={"chatId": chat_id, "text": parte})
            r.raise_for_status()

    def descargar_media(self, chat_id: str, mensaje_id: str) -> tuple[bytes, str | None]:
        r = self.http.get(self._url(f"/messages/{chat_id}/{mensaje_id}/media"), headers=self.headers)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            d = r.json()
            d = d.get("data", d) if isinstance(d.get("data"), dict) else d
            return base64.b64decode(d["data"]), d.get("mimetype")
        return r.content, r.headers.get("content-type")

    def registrar_webhook(self, url: str, secreto: str) -> dict:
        r = self.http.post(
            self._url("/webhooks"),
            headers=self.headers,
            json={"url": url, "events": ["message.received"], "secret": secreto},
        )
        r.raise_for_status()
        return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registrar-webhook", metavar="URL_PUBLICA", required=True)
    args = ap.parse_args(argv)
    secreto = os.getenv("OPENWA_WEBHOOK_SECRET", "")
    if not secreto or not os.getenv("OPENWA_SESSION_ID"):
        print("ERROR define OPENWA_SESSION_ID y OPENWA_WEBHOOK_SECRET en .env")
        return 1
    try:
        print(ClienteOpenWA().registrar_webhook(args.registrar_webhook, secreto))
    except httpx.HTTPError as e:
        print(f"ERROR al registrar el webhook: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

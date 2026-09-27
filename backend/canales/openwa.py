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
import secrets
import time
from pathlib import Path
import sys
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from rag.config import RAIZ  # carga .env y da la raíz del proyecto

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

    # Alta de sesión (solo se usa desde el asistente de configuración)

    def crear_sesion(self, nombre: str) -> str:
        r = self.http.post(f"{self.base}/api/sessions", headers=self.headers, json={"name": nombre})
        r.raise_for_status()
        d = r.json()
        d = d.get("data", d) if isinstance(d.get("data"), dict) else d
        return str(d.get("id") or d.get("sessionId") or d.get("name") or nombre)

    def iniciar_sesion(self) -> None:
        r = self.http.post(self._url("/start"), headers=self.headers)
        if r.status_code not in (200, 201, 204, 409):  # 409: ya estaba iniciada
            r.raise_for_status()

    def estado_sesion(self) -> str:
        r = self.http.get(self._url(""), headers=self.headers)
        r.raise_for_status()
        d = r.json()
        d = d.get("data", d) if isinstance(d.get("data"), dict) else d
        return str(d.get("status") or d.get("state") or "desconocido")

    def qr(self) -> str | None:
        r = self.http.get(self._url("/qr"), headers=self.headers)
        if r.status_code != 200:
            return None
        if r.headers.get("content-type", "").startswith("application/json"):
            d = r.json()
            d = d.get("data", d) if isinstance(d.get("data"), dict) else d
            return d.get("qr") or d.get("value") or d.get("url")
        return None

    def registrar_webhook(self, url: str, secreto: str) -> dict:
        r = self.http.post(
            self._url("/webhooks"),
            headers=self.headers,
            json={"url": url, "events": ["message.received"], "secret": secreto},
        )
        r.raise_for_status()
        return r.json()


ESTADOS_CONECTADO = {"connected", "authenticated", "ready", "working", "conectado"}


def configurar(url_publica: str, nombre_sesion: str, escribir_env, dormir=time.sleep, esperar_s: int = 180) -> int:
    """Deja WhatsApp listo salvo el escaneo del QR, que solo puede hacer una persona."""
    cliente = ClienteOpenWA()
    if not cliente.headers.get("X-API-Key"):
        print("ERROR falta OPENWA_API_KEY en .env (la obtienes del panel de OpenWA)")
        return 1

    secreto = os.getenv("OPENWA_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    nuevo_secreto = not os.getenv("OPENWA_WEBHOOK_SECRET")

    try:
        if not cliente.sesion:
            cliente.sesion = cliente.crear_sesion(nombre_sesion)
            print(f"1. Sesión creada: {cliente.sesion}")
        else:
            print(f"1. Usando la sesión existente: {cliente.sesion}")
        cliente.iniciar_sesion()

        estado = cliente.estado_sesion()
        if estado.lower() not in ESTADOS_CONECTADO:
            print(f"\n2. La sesión está en estado '{estado}'. FALTA ESCANEAR EL QR:")
            print(f"   Abre {cliente.base} en el navegador, entra a la sesión '{cliente.sesion}'")
            print("   y escanea el código con el WhatsApp del número dedicado al proyecto.")
            print(f"\n   Esperando hasta {esperar_s} segundos a que quede conectada...")
            for _ in range(esperar_s // 5):
                dormir(5)
                estado = cliente.estado_sesion()
                if estado.lower() in ESTADOS_CONECTADO:
                    break
        if estado.lower() not in ESTADOS_CONECTADO:
            print(f"\n   La sesión sigue en '{estado}'. Escanea el QR y vuelve a correr este comando.")
            return 2
        print(f"2. Sesión conectada ({estado})")

        webhook = cliente.registrar_webhook(url_publica, secreto)
        print(f"3. Webhook registrado en {url_publica}: {webhook}")
    except httpx.HTTPError as e:
        print(f"ERROR hablando con OpenWA: {e}")
        print(f"   Comprueba que OpenWA responda en {cliente.base} (docker compose up -d).")
        return 1

    escribir_env({"OPENWA_SESSION_ID": cliente.sesion, "OPENWA_WEBHOOK_SECRET": secreto})
    print("4. .env actualizado" + (" (se generó un OPENWA_WEBHOOK_SECRET nuevo)" if nuevo_secreto else ""))
    print("\nListo. Escríbele al número desde otro teléfono para probarlo.")
    return 0


def guardar_en_env(valores: dict, ruta: Path | None = None) -> None:
    """Escribe o actualiza claves en .env sin tocar las demás."""
    ruta = ruta or RAIZ / ".env"
    lineas = ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []
    for clave, valor in valores.items():
        nueva = f"{clave}={valor}"
        for i, l in enumerate(lineas):
            if l.startswith(f"{clave}="):
                lineas[i] = nueva
                break
        else:
            lineas.append(nueva)
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Configura el canal de WhatsApp (OpenWA)")
    ap.add_argument("--configurar", metavar="URL_PUBLICA",
                    help="deja todo listo: crea la sesión, espera el escaneo del QR y registra el webhook")
    ap.add_argument("--registrar-webhook", metavar="URL_PUBLICA", help="solo registra el webhook")
    ap.add_argument("--nombre-sesion", default="iustis")
    args = ap.parse_args(argv)

    if args.configurar:
        return configurar(args.configurar, args.nombre_sesion, guardar_en_env)
    if not args.registrar_webhook:
        ap.error("indica --configurar o --registrar-webhook")
    secreto = os.getenv("OPENWA_WEBHOOK_SECRET", "")
    if not secreto or not os.getenv("OPENWA_SESSION_ID"):
        print("ERROR define OPENWA_SESSION_ID y OPENWA_WEBHOOK_SECRET en .env, o usa --configurar")
        return 1
    try:
        print(ClienteOpenWA().registrar_webhook(args.registrar_webhook, secreto))
    except httpx.HTTPError as e:
        print(f"ERROR al registrar el webhook: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

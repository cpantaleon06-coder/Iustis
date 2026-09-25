"""Transcripción de notas de voz con Groq Whisper (API compatible con OpenAI)."""

from __future__ import annotations

import os

import httpx

URL_GROQ = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_BYTES = 25 * 1024 * 1024  # límite del plan gratuito de Groq

EXTENSIONES = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


class ErrorVoz(RuntimeError):
    pass


def transcribir(audio: bytes, mimetype: str, cliente: httpx.Client | None = None) -> str:
    clave = os.getenv("GROQ_API_KEY")
    if not clave:
        raise ErrorVoz("falta GROQ_API_KEY en .env")
    if len(audio) > MAX_BYTES:
        raise ErrorVoz("la nota de voz es demasiado larga")
    tipo = mimetype.split(";")[0].strip().lower()
    ext = EXTENSIONES.get(tipo)
    if ext is None:
        raise ErrorVoz(f"formato de audio no soportado: {mimetype}")

    cliente = cliente or httpx.Client(timeout=60)
    r = cliente.post(
        URL_GROQ,
        headers={"Authorization": f"Bearer {clave}"},
        files={"file": (f"nota.{ext}", audio, tipo)},
        data={
            "model": os.getenv("GROQ_MODELO_WHISPER", "whisper-large-v3-turbo"),
            "language": "es",
            "response_format": "json",
            "temperature": "0",
        },
    )
    if r.status_code != 200:
        raise ErrorVoz(f"Groq respondió {r.status_code}: {r.text[:200]}")
    texto = (r.json().get("text") or "").strip()
    if not texto:
        raise ErrorVoz("no se entendió el audio")
    return texto

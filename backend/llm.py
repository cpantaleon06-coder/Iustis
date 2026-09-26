"""Acceso al modelo de lenguaje. Toda llamada devuelve objetos Pydantic validados o lanza ErrorLLM.

Proveedores (variable LLM_PROVEEDOR en .env):
    groq       por defecto; modelos gpt-oss con salida JSON estricta (plan gratuito disponible)
    anthropic  Claude (requiere ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import copy
import re
import json
import logging
import os
import time
from datetime import date
from typing import Protocol
from xml.sax.saxutils import escape, quoteattr

import httpx
from pydantic import BaseModel, ValidationError

from backend.esquemas import RespuestaGenerada, Triaje
from backend.instituciones import Institucion
from rag.config import RAIZ

DIR_PROMPTS = RAIZ / "prompts"
log = logging.getLogger("llm")


class ErrorLLM(RuntimeError):
    pass


def cargar_prompt(nombre: str) -> str:
    return (DIR_PROMPTS / f"{nombre}.md").read_text(encoding="utf-8")


def acotar_articulo(texto: str, consulta: str, max_caracteres: int) -> str:
    """Si el artículo es más largo que el límite, conserva el primer párrafo y los
    párrafos más relacionados con la consulta, en su orden original, marcando los
    huecos con "[...]". Nunca altera el texto de los párrafos que conserva, así que
    cualquier cita sigue siendo literal del artículo completo."""
    if len(texto) <= max_caracteres:
        return texto
    from rag.texto import tokenizar

    parrafos = [p for p in texto.split("\n\n") if p.strip()]
    claves = set(tokenizar(consulta))
    puntaje = [len(claves & set(tokenizar(p))) for p in parrafos]
    elegidos, usados = {0}, len(parrafos[0])
    for i in sorted(range(1, len(parrafos)), key=lambda i: (-puntaje[i], i)):
        if puntaje[i] == 0:
            break
        if usados + len(parrafos[i]) <= max_caracteres:
            elegidos.add(i)
            usados += len(parrafos[i])
    partes, previo = [], -1
    for i in sorted(elegidos):
        if i != previo + 1:
            partes.append("[...]")
        partes.append(parrafos[i])
        previo = i
    if previo != len(parrafos) - 1:
        partes.append("[...]")
    resultado = "\n\n".join(partes)
    # Tope duro: un primer párrafo enorme no debe rebasar el límite (el prefijo sigue siendo literal)
    if len(resultado) > max_caracteres + 200:
        resultado = resultado[:max_caracteres] + "\n\n[...]"
    return resultado


def contexto_articulos(articulos: list[dict], max_caracteres: int | None = None, consulta: str = "") -> str:
    bloques = []
    for a in articulos:
        attrs = (
            f"id={quoteattr(a['id'])} ley={quoteattr(a['ley'])} "
            f"articulo={quoteattr(a['articulo'])} version={quoteattr(a['fecha_version'])}"
        )
        # El texto va sin escapar: el modelo debe poder copiarlo literal para la cita
        texto = acotar_articulo(a["texto"], consulta, max_caracteres) if max_caracteres else a["texto"]
        bloques.append(f"<articulo {attrs}>\n{texto}\n</articulo>")
    return "<articulos>\n" + "\n".join(bloques) + "\n</articulos>"


def contexto_instituciones(instituciones: list[Institucion]) -> str:
    lineas = [f"- id: {i.id} | cuándo: {i.cuando}" for i in instituciones]
    return "<catalogo_instituciones>\n" + "\n".join(lineas) + "\n</catalogo_instituciones>"


def contenido_triaje(mensaje: str) -> str:
    return f"<fecha_hoy>{date.today().isoformat()}</fecha_hoy>\n<mensaje>\n{escape(mensaje)}\n</mensaje>"


def contenido_generacion(
    mensaje: str,
    triaje: Triaje,
    articulos: list[dict],
    instituciones: list[Institucion],
    max_caracteres: int | None = None,
) -> str:
    consulta = " ".join([mensaje, triaje.consulta_reformulada, *triaje.hechos])
    return "\n\n".join(
        [
            contexto_articulos(articulos, max_caracteres, consulta),
            contexto_instituciones(instituciones),
            "<hechos>\n" + "\n".join(f"- {escape(h)}" for h in triaje.hechos) + "\n</hechos>",
            f"<mensaje_original>\n{escape(mensaje)}\n</mensaje_original>",
        ]
    )


class ClienteLLM(Protocol):
    def triaje(self, mensaje: str) -> Triaje: ...

    def generar(
        self, mensaje: str, triaje: Triaje, articulos: list[dict], instituciones: list[Institucion]
    ) -> RespuestaGenerada: ...


# Groq

def esquema_estricto(modelo: type[BaseModel]) -> dict:
    """Esquema JSON compatible con el modo estricto de Groq: todos los campos
    obligatorios y additionalProperties false en cada objeto."""
    esquema = copy.deepcopy(modelo.model_json_schema())

    def ajustar(nodo):
        if isinstance(nodo, dict):
            nodo.pop("title", None)
            nodo.pop("default", None)
            if nodo.get("type") == "object" and "properties" in nodo:
                nodo["additionalProperties"] = False
                nodo["required"] = list(nodo["properties"])
            for v in nodo.values():
                ajustar(v)
        elif isinstance(nodo, list):
            for v in nodo:
                ajustar(v)

    ajustar(esquema)
    return esquema


def duracion_a_segundos(valor: str | None) -> float | None:
    """Convierte "7", "40.897s", "1m2.5s" o "350ms" a segundos."""
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        pass
    total, encontrado = 0.0, False
    for numero, unidad in re.findall(r"([\d.]+)(ms|h|m|s)", valor):
        encontrado = True
        total += float(numero) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unidad]
    return total if encontrado else None


class LimiteGroq(Exception):
    """El modelo está saturado; lleva la espera sugerida por Groq."""

    def __init__(self, espera: float):
        self.espera = espera


class GroqLLM:
    URL = "https://api.groq.com/openai/v1/chat/completions"
    MAX_REINTENTOS = 3  # reintentos por contenido (JSON inválido, falla de red)
    MAX_ESPERAS = 3     # esperas por saturación, independientes de los reintentos
    MAX_ESPERA_S = 60
    # Si la espera sugerida supera esto y hay otro modelo disponible, se usa el de respaldo
    ESPERA_PARA_RESPALDO_S = 10

    def __init__(self, cliente: httpx.Client | None = None, dormir=time.sleep):
        clave = os.getenv("GROQ_API_KEY")
        if not clave:
            raise ErrorLLM("falta GROQ_API_KEY en .env")
        self.http = cliente or httpx.Client(timeout=90)
        self.headers = {"Authorization": f"Bearer {clave}"}
        self.dormir = dormir
        self.modelo_triaje = os.getenv("GROQ_MODELO_TRIAJE", "openai/gpt-oss-20b")
        self.modelo_respuesta = os.getenv("GROQ_MODELO_RESPUESTA", "openai/gpt-oss-120b")
        # Cada modelo tiene su propia cuota en Groq: el de triaje sirve de respaldo para la respuesta
        self.modelo_respaldo = os.getenv("GROQ_MODELO_RESPALDO", self.modelo_triaje)
        self.max_caracteres_articulo = int(os.getenv("MAX_CARACTERES_ARTICULO", "3000"))
        self.prompt_triaje = cargar_prompt("triaje")
        self.prompt_respuesta = cargar_prompt("respuesta")

    def _completar(
        self,
        formato: type[BaseModel],
        modelo: str,
        sistema: str,
        usuario: str,
        max_tokens: int,
        esfuerzo: str,
        respaldo: str | None = None,
    ):
        """Llama al modelo; si está saturado y la espera es larga, cambia al de respaldo."""
        try:
            return self._llamar(formato, modelo, sistema, usuario, max_tokens, esfuerzo, reintentar=respaldo is None)
        except LimiteGroq as e:
            if respaldo is None:
                raise ErrorLLM("Groq respondió 429 (límite de uso por minuto alcanzado)") from e
            log.warning("%s saturado (espera %.0f s); se usa %s", modelo, e.espera, respaldo)
            try:
                return self._llamar(formato, respaldo, sistema, usuario, max_tokens, esfuerzo, reintentar=True)
            except LimiteGroq as e2:
                raise ErrorLLM("Groq respondió 429 (límite de uso por minuto alcanzado)") from e2

    def _llamar(self, formato, modelo, sistema, usuario, max_tokens, esfuerzo, reintentar: bool):
        cuerpo = {
            "model": modelo,
            "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": formato.__name__, "strict": True, "schema": esquema_estricto(formato)},
            },
            "max_completion_tokens": max_tokens,
            "reasoning_effort": esfuerzo,
            "include_reasoning": False,
        }
        # Dos presupuestos separados: esperar por saturación no es lo mismo que reintentar
        # porque el modelo devolvió un JSON que no cumple el esquema.
        ultimo_error = ""
        esperas = intentos = 0
        while intentos < self.MAX_REINTENTOS and esperas <= self.MAX_ESPERAS:
            try:
                r = self.http.post(self.URL, headers=self.headers, json=cuerpo)
            except httpx.HTTPError as e:
                intentos += 1
                ultimo_error = f"sin conexión con Groq: {e}"
                self.dormir(2 * intentos)
                continue
            if r.status_code == 429:
                sugerida = (
                    duracion_a_segundos(r.headers.get("retry-after"))
                    or duracion_a_segundos(r.headers.get("x-ratelimit-reset-tokens"))
                    or 2.0 * (esperas + 1)
                )
                espera = min(sugerida + 0.5, self.MAX_ESPERA_S)
                if not reintentar and sugerida > self.ESPERA_PARA_RESPALDO_S:
                    raise LimiteGroq(espera)
                esperas += 1
                if esperas > self.MAX_ESPERAS:
                    raise LimiteGroq(espera)
                log.warning("Groq 429 en %s; espera %d de %d, %.1f s", modelo, esperas, self.MAX_ESPERAS, espera)
                self.dormir(espera)
                continue
            intentos += 1
            if r.status_code >= 500:
                ultimo_error = f"Groq respondió {r.status_code} (falla temporal)"
                self.dormir(2.0 * intentos)
                continue
            if r.status_code != 200:
                ultimo_error = f"Groq respondió {r.status_code}: {r.text[:300]}"
                if "json_validate_failed" not in r.text:
                    raise ErrorLLM(ultimo_error)
                log.warning("Groq devolvió JSON inválido en %s; reintento %d", modelo, intentos)
                continue  # el modelo produjo JSON que no cumple el esquema: se reintenta
            eleccion = r.json()["choices"][0]
            if eleccion.get("finish_reason") != "stop":
                raise ErrorLLM(f"respuesta incompleta de Groq (finish_reason={eleccion.get('finish_reason')})")
            try:
                return formato.model_validate_json(eleccion["message"]["content"] or "")
            except ValidationError as e:
                ultimo_error = f"JSON que no cumple el esquema: {e.errors()[:2]}"
        raise ErrorLLM(ultimo_error or "Groq no respondió")

    def triaje(self, mensaje: str) -> Triaje:
        return self._completar(Triaje, self.modelo_triaje, self.prompt_triaje, contenido_triaje(mensaje), 2000, "low")

    def generar(
        self, mensaje: str, triaje: Triaje, articulos: list[dict], instituciones: list[Institucion]
    ) -> RespuestaGenerada:
        contenido = contenido_generacion(mensaje, triaje, articulos, instituciones, self.max_caracteres_articulo)
        respaldo = self.modelo_respaldo if self.modelo_respaldo != self.modelo_respuesta else None
        return self._completar(
            RespuestaGenerada, self.modelo_respuesta, self.prompt_respuesta, contenido, 3000, "medium", respaldo
        )


# Anthropic

class ClaudeLLM:
    def __init__(self, cliente=None):
        import anthropic

        self._anthropic = anthropic
        self.cliente = cliente or anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.modelo_triaje = os.getenv("MODELO_TRIAJE", "claude-haiku-4-5")
        self.modelo_respuesta = os.getenv("MODELO_RESPUESTA", "claude-sonnet-5")
        self.esfuerzo_respuesta = os.getenv("ESFUERZO_RESPUESTA", "medium")
        self.prompt_triaje = cargar_prompt("triaje")
        self.prompt_respuesta = cargar_prompt("respuesta")

    def _parse(self, formato, **kwargs):
        anthropic = self._anthropic
        try:
            r = self.cliente.messages.parse(output_format=formato, **kwargs)
        except anthropic.APIConnectionError as e:
            raise ErrorLLM(f"sin conexión con la API: {e}") from e
        except anthropic.RateLimitError as e:
            raise ErrorLLM("límite de uso de la API alcanzado") from e
        except anthropic.APIStatusError as e:
            raise ErrorLLM(f"error de la API ({e.status_code}): {e.message}") from e
        except TypeError as e:
            # El SDK lanza TypeError (no un error de API) cuando no encuentra credenciales
            if "authentication" in str(e):
                raise ErrorLLM("sin credenciales de Anthropic (define ANTHROPIC_API_KEY en .env)") from e
            raise
        if r.stop_reason != "end_turn" or r.parsed_output is None:
            raise ErrorLLM(f"respuesta no utilizable (stop_reason={r.stop_reason}, request_id={r._request_id})")
        return r.parsed_output

    def triaje(self, mensaje: str) -> Triaje:
        return self._parse(
            Triaje,
            model=self.modelo_triaje,
            max_tokens=1500,
            system=self.prompt_triaje,
            messages=[{"role": "user", "content": contenido_triaje(mensaje)}],
        )

    def generar(
        self, mensaje: str, triaje: Triaje, articulos: list[dict], instituciones: list[Institucion]
    ) -> RespuestaGenerada:
        return self._parse(
            RespuestaGenerada,
            model=self.modelo_respuesta,
            max_tokens=8000,
            output_config={"effort": self.esfuerzo_respuesta},
            system=self.prompt_respuesta,
            messages=[{"role": "user", "content": contenido_generacion(mensaje, triaje, articulos, instituciones)}],
        )


def crear_llm() -> ClienteLLM:
    proveedor = os.getenv("LLM_PROVEEDOR", "groq").lower()
    if proveedor == "groq":
        return GroqLLM()
    if proveedor == "anthropic":
        return ClaudeLLM()
    raise ErrorLLM(f"LLM_PROVEEDOR desconocido: {proveedor}")

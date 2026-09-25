"""Acceso a Claude. Toda llamada devuelve objetos Pydantic validados o lanza ErrorLLM."""

from __future__ import annotations

import os
from datetime import date
from typing import Protocol
from xml.sax.saxutils import escape, quoteattr

import anthropic

from backend.esquemas import RespuestaGenerada, Triaje
from backend.instituciones import Institucion
from rag.config import RAIZ

DIR_PROMPTS = RAIZ / "prompts"


class ErrorLLM(RuntimeError):
    pass


def cargar_prompt(nombre: str) -> str:
    return (DIR_PROMPTS / f"{nombre}.md").read_text(encoding="utf-8")


def contexto_articulos(articulos: list[dict]) -> str:
    bloques = []
    for a in articulos:
        attrs = (
            f"id={quoteattr(a['id'])} ley={quoteattr(a['ley'])} "
            f"articulo={quoteattr(a['articulo'])} version={quoteattr(a['fecha_version'])}"
        )
        # El texto va sin escapar: el modelo debe poder copiarlo literal para la cita
        bloques.append(f"<articulo {attrs}>\n{a['texto']}\n</articulo>")
    return "<articulos>\n" + "\n".join(bloques) + "\n</articulos>"


def contexto_instituciones(instituciones: list[Institucion]) -> str:
    lineas = [f"- id: {i.id} | cuándo: {i.cuando}" for i in instituciones]
    return "<catalogo_instituciones>\n" + "\n".join(lineas) + "\n</catalogo_instituciones>"


class ClienteLLM(Protocol):
    def triaje(self, mensaje: str) -> Triaje: ...

    def generar(
        self, mensaje: str, triaje: Triaje, articulos: list[dict], instituciones: list[Institucion]
    ) -> RespuestaGenerada: ...


class ClaudeLLM:
    def __init__(self, cliente: anthropic.Anthropic | None = None):
        self.cliente = cliente or anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.modelo_triaje = os.getenv("MODELO_TRIAJE", "claude-haiku-4-5")
        self.modelo_respuesta = os.getenv("MODELO_RESPUESTA", "claude-sonnet-5")
        self.esfuerzo_respuesta = os.getenv("ESFUERZO_RESPUESTA", "medium")
        self.prompt_triaje = cargar_prompt("triaje")
        self.prompt_respuesta = cargar_prompt("respuesta")

    def _parse(self, formato, **kwargs):
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
            messages=[
                {
                    "role": "user",
                    "content": f"<fecha_hoy>{date.today().isoformat()}</fecha_hoy>\n<mensaje>\n{escape(mensaje)}\n</mensaje>",
                }
            ],
        )

    def generar(
        self, mensaje: str, triaje: Triaje, articulos: list[dict], instituciones: list[Institucion]
    ) -> RespuestaGenerada:
        contenido = "\n\n".join(
            [
                contexto_articulos(articulos),
                contexto_instituciones(instituciones),
                "<hechos>\n" + "\n".join(f"- {escape(h)}" for h in triaje.hechos) + "\n</hechos>",
                f"<mensaje_original>\n{escape(mensaje)}\n</mensaje_original>",
            ]
        )
        return self._parse(
            RespuestaGenerada,
            model=self.modelo_respuesta,
            max_tokens=8000,
            output_config={"effort": self.esfuerzo_respuesta},
            system=self.prompt_respuesta,
            messages=[{"role": "user", "content": contenido}],
        )

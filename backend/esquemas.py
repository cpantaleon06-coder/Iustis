"""Esquemas de salida estructurada del modelo (validados por Pydantic y por la API)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Area = Literal["transito", "laboral", "constitucional", "civil", "penal", "fuera_de_alcance"]
Urgencia = Literal["alta", "media", "baja"]


class DatoFaltante(BaseModel):
    dato: str = Field(description="Qué información falta, en pocas palabras")
    pregunta: str = Field(description="Pregunta breve y amable para pedírsela a la persona")
    critico: bool = Field(description="True si sin este dato no se puede orientar correctamente")


class DatosLaborales(BaseModel):
    salario: float | None = Field(description="Monto del salario que menciona la persona, sin símbolos; null si no lo dijo")
    periodo_salario: Literal["diario", "semanal", "quincenal", "mensual"] | None
    fecha_ingreso: str | None = Field(description="AAAA-MM-DD; null si no la dijo con día, mes y año")
    fecha_despido: str | None = Field(description="AAAA-MM-DD; resuelve fechas relativas con <fecha_hoy>; null si no la dijo")
    pide_montos: bool = Field(description="True si pregunta cuánto le deben pagar o menciona liquidación o finiquito")


class Triaje(BaseModel):
    es_consulta_legal: bool
    area: Area
    urgencia: Urgencia
    motivo_urgencia: str = Field(description="Por qué se asignó esa urgencia, en una frase")
    hechos: list[str] = Field(description="Hechos concretos que relata la persona, sin interpretar")
    datos_faltantes: list[DatoFaltante]
    consulta_reformulada: str = Field(
        description="La consulta reescrita en lenguaje jurídico claro para buscar artículos aplicables"
    )
    datos_laborales: DatosLaborales | None = Field(description="Solo si area es laboral; null en otro caso")


class CitaModelo(BaseModel):
    articulo_id: str = Field(description="id exacto del artículo tal como aparece en <articulo id=...>")
    texto_literal: str = Field(description="Fragmento copiado carácter por carácter del artículo")


class Derecho(BaseModel):
    explicacion: str = Field(description="El derecho explicado en lenguaje sencillo")
    cita: CitaModelo


class Paso(BaseModel):
    accion: str
    cita: CitaModelo | None = Field(
        description="Obligatoria si el paso depende de una regla legal (plazo, obligación, requisito); null si es un consejo práctico"
    )


class RespuestaGenerada(BaseModel):
    que_esta_pasando: str
    derechos: list[Derecho]
    que_hacer: list[Paso]
    instituciones: list[str] = Field(description="ids del catálogo de instituciones, solo los que apliquen")
    cuando_abogado: str
    sin_respaldo: list[str] = Field(
        description="Partes de la consulta que los artículos proporcionados no permiten contestar"
    )

"""Catálogo de instituciones (data/instituciones/*.yaml).

Los datos de contacto nunca los genera el modelo: el modelo solo elige ids de aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from rag.config import DATA

DIR_INSTITUCIONES = DATA / "instituciones"


@dataclass
class Institucion:
    id: str
    nombre: str
    cuando: str
    contacto: str
    tipo: str
    verificado: bool

    @property
    def pendiente(self) -> bool:
        return not self.verificado or "PLACEHOLDER" in f"{self.nombre}{self.contacto}"


def _leer(ruta: Path) -> list[Institucion]:
    if not ruta.exists():
        return []
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    return [Institucion(**{**i, "verificado": bool(i.get("verificado"))}) for i in datos.get("instituciones") or []]


def catalogo(modulos: list[str], directorio: Path = DIR_INSTITUCIONES) -> dict[str, Institucion]:
    """Instituciones generales más las de los módulos indicados, indexadas por id."""
    todas = _leer(directorio / "general.yaml")
    for m in modulos:
        todas += _leer(directorio / f"{m}.yaml")
    return {i.id: i for i in todas}


def por_tipo(cat: dict[str, Institucion], tipo: str) -> list[Institucion]:
    return [i for i in cat.values() if i.tipo == tipo]

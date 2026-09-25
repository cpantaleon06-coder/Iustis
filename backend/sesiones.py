"""Estado mínimo por conversación, en memoria (suficiente para el demo).

Solo se guarda lo necesario para una pregunta de seguimiento: el mensaje original y
la pregunta que se hizo. Caduca a los 30 minutos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

TTL_SEGUNDOS = 30 * 60


@dataclass
class Pendiente:
    mensaje_original: str
    pregunta: str
    creado: float = field(default_factory=time.time)


class Sesiones:
    def __init__(self, ttl: int = TTL_SEGUNDOS):
        self.ttl = ttl
        self._pendientes: dict[str, Pendiente] = {}

    def tomar_pendiente(self, usuario: str) -> Pendiente | None:
        p = self._pendientes.pop(usuario, None)
        if p and time.time() - p.creado > self.ttl:
            return None
        return p

    def guardar_pendiente(self, usuario: str, mensaje: str, pregunta: str) -> None:
        self._pendientes[usuario] = Pendiente(mensaje, pregunta)

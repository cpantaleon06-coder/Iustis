"""Límite de mensajes por usuario, en memoria (suficiente para el demo).

Cada mensaje cuesta llamadas a Claude; si el chat web se expone con ngrok, cualquiera
con el enlace podría gastar los créditos. Ventana deslizante de una hora.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


class Limitador:
    def __init__(self, maximo: int, ventana_segundos: int = 3600):
        self.maximo = maximo
        self.ventana = ventana_segundos
        self._eventos: dict[str, deque[float]] = defaultdict(deque)
        self._candado = threading.Lock()

    def permitir(self, clave: str, ahora: float | None = None) -> bool:
        ahora = time.monotonic() if ahora is None else ahora
        with self._candado:
            eventos = self._eventos[clave]
            while eventos and ahora - eventos[0] >= self.ventana:
                eventos.popleft()
            if len(eventos) >= self.maximo:
                return False
            eventos.append(ahora)
            return True


def desde_entorno(variable: str, por_defecto: int) -> Limitador:
    return Limitador(int(os.getenv(variable, por_defecto)))

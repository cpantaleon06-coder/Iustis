"""Pruebas del chat web servido en backend/static/chat.html.

Existen porque un salto de línea literal dentro de una cadena de JavaScript rompe la
página entera sin que ninguna prueba de Python se entere: el servidor responde 200 y
el HTML llega completo, pero el navegador no ejecuta nada y el chat aparece vacío.
Ya ocurrió dos veces al editar el archivo con scripts.
"""

import re

import pytest

from rag.config import RAIZ

CHAT = RAIZ / "backend" / "static" / "chat.html"


def texto() -> str:
    return CHAT.read_text(encoding="utf-8")


def cuerpo_del_script() -> str:
    m = re.search(r"<script>(.*)</script>", texto(), re.DOTALL)
    assert m, "no se encontró el bloque <script> del chat"
    return m.group(1)


def cadenas_sin_cerrar(script: str) -> list[str]:
    """Líneas donde queda una comilla abierta, que es como se ve el bug."""
    fallas = []
    for n, linea in enumerate(script.split("\n"), start=1):
        limpia = linea.replace("\\\\", "").replace('\\"', "").replace("\\'", "")
        limpia = re.sub(r"/\[\^[^\]]*\]/|/[^/\s]+/[gimsuy]*", "", limpia)  # expresiones regulares
        for comilla in ('"', "'"):
            if limpia.count(comilla) % 2:
                fallas.append(f"línea {n}: comilla {comilla} sin cerrar -> {linea.strip()[:80]}")
    return fallas


# Primero se comprueba que el detector sirve, y luego se aplica al archivo real

def test_el_detector_encuentra_una_cadena_partida():
    roto = 'const BIENVENIDA = "Hola, soy *Iustis*.\n\n" +\n  "Cuéntame qué te pasó.";'
    assert len(cadenas_sin_cerrar(roto)) == 2  # la línea que abre y la que cierra


def test_el_detector_no_se_queja_del_codigo_correcto():
    bueno = 'const BIENVENIDA = "Hola, soy *Iustis*.\\n\\n" +\n  "Cuéntame qué te pasó.";'
    assert cadenas_sin_cerrar(bueno) == []


def test_ninguna_cadena_del_chat_queda_abierta():
    fallas = cadenas_sin_cerrar(cuerpo_del_script())
    assert not fallas, "\n".join(fallas)


def test_los_saltos_de_linea_van_escapados():
    assert "\\n\\n" in cuerpo_del_script(), "el mensaje de bienvenida separa párrafos con \\n escapado"


def test_la_pagina_lleva_el_nombre_y_la_descripcion():
    t = texto()
    assert "<title>Iustis</title>" in t
    assert "<h1>Iustis</h1>" in t
    assert "Primeros Auxilios Legales" in t  # la descripción, no el nombre
    assert "No sustituye la asesoría de un abogado" in t


def test_hay_ejemplos_para_arrancar_la_conversacion():
    m = re.search(r"const EJEMPLOS = \[(.*?)\];", cuerpo_del_script(), re.DOTALL)
    assert m, "faltan los ejemplos con los que arranca el demo"
    assert len(re.findall(r'"[^"]{20,}"', m.group(1))) >= 3


def test_el_texto_del_usuario_no_se_inserta_como_html():
    """Las burbujas propias usan textContent; solo las del asistente pasan por formato()."""
    script = cuerpo_del_script()
    assert "div.innerHTML = formato(texto); else div.textContent = texto;" in script
    assert 'replace(/&/g, "&amp;")' in script  # formato() escapa antes de aplicar negritas

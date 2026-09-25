"""Extrae el texto de un PDF oficial a data/raw/<fuente>.txt.

Uso:
    python -m ingestion.extraer --pdf LFT.pdf --fuente ley_federal_trabajo
    python -m ingestion.extraer --pdf RT.pdf --docx RT.docx --fuente reglamento_transito_cdmx

Reglas:
  - No se corrige ni se completa texto: solo se quitan los encabezados y pies de página
    que se repiten en la mayoría de las páginas (título de la ley, "Última Reforma...",
    "2 de 457"). Las líneas quitadas se reportan.
  - Se usa PyMuPDF porque conserva las palabras íntegras (pypdf insertaba espacios
    dentro de palabras, lo que rompería las citas literales).
  - Con --docx se hace una verificación cruzada: cada párrafo del DOCX oficial debe
    aparecer en el texto extraído del PDF (comparando sin espacios). Se usa el PDF como
    fuente porque ahí los números de fracción están impresos; en el DOCX son numeración
    automática de Word y se perderían.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

RAIZ = Path(__file__).resolve().parents[1]
MARGEN_SUPERIOR = 10  # líneas no vacías revisadas al inicio de cada página
MARGEN_INFERIOR = 4
UMBRAL_REPETICION = 0.5  # fracción de páginas en que debe repetirse una línea para considerarla encabezado


def _clave(linea: str) -> str:
    """Normaliza una línea de margen: los números de página cambian, el resto no."""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", linea).strip())


def paginas_pdf(ruta: Path, tablas: bool = False, registro: list | None = None) -> list[list[str]]:
    import pymupdf

    with pymupdf.open(ruta) as doc:
        if not tablas:
            return [[l.rstrip() for l in p.get_text().split("\n")] for p in doc]
        return _paginas_con_tablas(doc, registro if registro is not None else [])


# Tablas
#
# Las tablas se extraen celda por celda y el orden de lectura normal las revuelve
# (una multa puede quedar junto a la fila equivocada). Las tablas reales se reescriben
# una fila por línea, con las celdas separadas por " | ", sin cambiar ninguna palabra.

MIN_FILAS, MIN_COLUMNAS = 3, 3


def _celda(c) -> str:
    return re.sub(r"\s+", " ", c or "").strip()


def _unir_encabezado(filas: list[list[str]]) -> list[list[str]]:
    """El encabezado suele venir partido en varias filas con la primera columna vacía."""
    if not filas:
        return filas
    encabezado = list(filas[0])
    i = 1
    while i < len(filas) and not filas[i][0] and any(filas[i]):
        encabezado = [f"{a} {b}".strip() for a, b in zip(encabezado, filas[i])]
        i += 1
    return [encabezado] + filas[i:]


def _filas_utiles(tabla) -> list[list[str]]:
    return [[_celda(c) for c in f] for f in tabla.extract()]


def _es_continuacion(filas: list[list[str]]) -> bool:
    """Fila suelta al inicio de página que solo continúa la primera celda de la tabla anterior."""
    return len(filas) == 1 and bool(filas[0][0]) and not any(filas[0][1:])


def _intersecta(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _paginas_con_tablas(doc, registro: list) -> list[list[str]]:
    paginas: list[list[str]] = []
    pendiente = None  # (índice de página, índice de línea) de la última fila de la tabla previa
    for num, pagina in enumerate(doc):
        tablas = pagina.find_tables().tables
        if not tablas:
            paginas.append([l.rstrip() for l in pagina.get_text().split("\n")])
            pendiente = None
            continue
        reemplazos = []  # (bbox, líneas o None para descartar)
        for t in tablas:
            filas = _filas_utiles(t)
            if pendiente and _es_continuacion(filas):
                p, l = pendiente
                primera, _, resto = paginas[p][l].partition(" | ")
                paginas[p][l] = f"{primera} {filas[0][0]} | {resto}" if resto else f"{primera} {filas[0][0]}"
                reemplazos.append((t.bbox, None))
                registro.append(f"pág {num + 1}: fila de continuación unida a la tabla anterior")
                continue
            filas = _unir_encabezado(filas)
            if len(filas) < MIN_FILAS or t.col_count < MIN_COLUMNAS:
                continue  # no es una tabla real (por ejemplo un párrafo con sangría)
            lineas = [" | ".join(c for c in f if c) for f in filas if any(f)]
            reemplazos.append((t.bbox, lineas))
            registro.append(f"pág {num + 1}: tabla de {len(lineas)} filas reescrita fila por fila")
        if not reemplazos:
            paginas.append([l.rstrip() for l in pagina.get_text().split("\n")])
            pendiente = None
            continue

        lineas_pagina: list[str] = []
        insertadas = set()
        for bloque in pagina.get_text("blocks"):
            rect, texto = bloque[:4], bloque[4]
            destino = next((i for i, (bbox, _) in enumerate(reemplazos) if _intersecta(rect, bbox)), None)
            if destino is None:
                lineas_pagina += [l.rstrip() for l in texto.split("\n")] + [""]
            elif destino not in insertadas:
                insertadas.add(destino)
                if reemplazos[destino][1] is not None:
                    lineas_pagina += [""] + reemplazos[destino][1] + [""]
        # Si la página termina en tabla, su última fila puede continuar en la página siguiente
        ultimo = max((b for b in reemplazos if b[1]), key=lambda b: b[0][3], default=None)
        if ultimo and ultimo[1][-1] in lineas_pagina:
            pendiente = (num, len(lineas_pagina) - 1 - lineas_pagina[::-1].index(ultimo[1][-1]))
        else:
            pendiente = None
        paginas.append(lineas_pagina)
    return paginas


def detectar_margenes(paginas: list[list[str]]) -> set[str]:
    conteo: Counter[str] = Counter()
    for lineas in paginas:
        no_vacias = [l for l in lineas if l.strip()]
        margen = set(no_vacias[:MARGEN_SUPERIOR] + no_vacias[-MARGEN_INFERIOR:])
        conteo.update({_clave(l) for l in margen})
    minimo = max(3, int(len(paginas) * UMBRAL_REPETICION))
    return {k for k, n in conteo.items() if n >= minimo}


def limpiar(paginas: list[list[str]], repetidas: set[str]) -> tuple[str, Counter]:
    quitadas: Counter[str] = Counter()
    salida = []
    for lineas in paginas:
        no_vacias = [i for i, l in enumerate(lineas) if l.strip()]
        zona = set(no_vacias[:MARGEN_SUPERIOR] + no_vacias[-MARGEN_INFERIOR:])
        for i, l in enumerate(lineas):
            if i in zona and _clave(l) in repetidas:
                quitadas[_clave(l)] += 1
                continue
            salida.append(l)
    texto = "\n".join(salida)
    return re.sub(r"\n{3,}", "\n\n", texto).strip() + "\n", quitadas


def ultima_reforma(paginas: list[list[str]]) -> str | None:
    """Fecha de última reforma tal como la imprime el documento (primeras páginas)."""
    inicio = " ".join(" ".join(p) for p in paginas[:2])
    m = re.search(r"[ÚU]ltima\s+[Rr]eforma[^0-9]{0,60}?(\d{1,2}[-/ ]\S+[-/ ]\d{4}|\d{1,2}\s+de\s+\w+\s+de\s+\d{4})", inicio)
    return m.group(0) if m else None


def parrafos_docx(ruta: Path) -> list[str]:
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    raiz = ET.fromstring(zipfile.ZipFile(ruta).read("word/document.xml"))
    return ["".join(t.text or "" for t in p.iter(w + "t")) for p in raiz.iter(w + "p")]


def _compacto(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s))


def verificar_contra_docx(texto: str, docx: Path, minimo: int = 30) -> tuple[int, list[str]]:
    """(párrafos verificados, párrafos del DOCX que no aparecen en el PDF)."""
    pdf = _compacto(texto)
    faltantes, total = [], 0
    for p in parrafos_docx(docx):
        c = _compacto(p)
        if len(c) < minimo:
            continue
        total += 1
        if c not in pdf:
            faltantes.append(p.strip())
    return total, faltantes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extrae el texto de un PDF oficial")
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--fuente", required=True, help="nombre de la fuente (data/raw/<fuente>.txt)")
    ap.add_argument("--docx", type=Path, help="DOCX oficial para verificación cruzada")
    ap.add_argument("--tablas", action="store_true", help="reescribe las tablas fila por fila")
    args = ap.parse_args(argv)

    registro_tablas: list[str] = []
    paginas = paginas_pdf(args.pdf, tablas=args.tablas, registro=registro_tablas)
    repetidas = detectar_margenes(paginas)
    texto, quitadas = limpiar(paginas, repetidas)
    destino = RAIZ / "data" / "raw" / f"{args.fuente}.txt"
    destino.write_text(texto, encoding="utf-8")

    print(f"{args.pdf.name}: {len(paginas)} páginas -> {destino.relative_to(RAIZ)} ({len(texto):,} caracteres)")
    print(f"Última reforma según el documento: {ultima_reforma(paginas) or 'NO ENCONTRADA'}")
    print("Encabezados y pies de página quitados:")
    for linea, n in quitadas.most_common():
        print(f"  {n:>4} x {linea!r}")
    if args.tablas:
        print(f"Tablas: {len(registro_tablas)} cambios")
        for r in registro_tablas:
            print(f"  {r}")
    if args.docx:
        total, faltantes = verificar_contra_docx(texto, args.docx)
        print(f"Verificación contra DOCX: {total - len(faltantes)}/{total} párrafos encontrados en el PDF")
        for f in faltantes[:20]:
            print(f"  FALTA: {f[:110]!r}")
        if len(faltantes) > 20:
            print(f"  ... y {len(faltantes) - 20} más")
    return 0


if __name__ == "__main__":
    sys.exit(main())

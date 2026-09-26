"""Calculadora laboral determinista (código puro, sin modelo).

El código solo hace aritmética. Todos los valores legales vienen de
parametros_laborales.yaml y cada concepto debe tener un fundamento cuya cita se
coteja literalmente contra el corpus. Cada resultado incluye la fórmula paso a paso.

Uso para revisar cifras:
    python -m backend.calculadora.laboral --salario 9000 --periodo mensual \
        --ingreso 2020-01-15 --despido 2026-09-01
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from rag.config import DIR_INDICE
from rag.verifier import Cita, verificar_cita

RUTA_PARAMETROS = Path(__file__).with_name("parametros_laborales.yaml")
CENTAVO = Decimal("0.01")


def dinero(x: Decimal) -> str:
    return f"${x.quantize(CENTAVO, ROUND_HALF_UP):,.2f}"


def num(x: Decimal, decimales: int = 4) -> str:
    s = f"{x:.{decimales}f}".rstrip("0").rstrip(".")
    return s or "0"


class Faltante(Exception):
    """Falta un parámetro legal (null en el YAML) o un dato de la persona."""


@dataclass
class DatosTrabajador:
    salario: Decimal
    periodo: str  # diario | semanal | quincenal | mensual
    fecha_ingreso: date
    fecha_despido: date
    supuesto: str = "despido_injustificado"


@dataclass
class ConceptoCalculado:
    id: str
    nombre: str
    fundamento: dict  # articulo del corpus
    pasos: list[str]
    monto: Decimal
    verificado: bool


@dataclass
class ConceptoNoCalculado:
    id: str
    nombre: str
    motivo: str


@dataclass
class ResultadoCalculo:
    pasos_base: list[str]
    calculados: list[ConceptoCalculado] = field(default_factory=list)
    no_calculados: list[ConceptoNoCalculado] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    supuesto: str = ""

    @property
    def total(self) -> Decimal:
        return sum((c.monto for c in self.calculados), Decimal(0))

    @property
    def todo_verificado(self) -> bool:
        return bool(self.calculados) and all(c.verificado for c in self.calculados)


def cargar_parametros(ruta: Path = RUTA_PARAMETROS) -> dict:
    return yaml.safe_load(ruta.read_text(encoding="utf-8"))


def cargar_corpus_citable(modulo: str, dir_indice: Path = DIR_INDICE) -> dict[str, dict]:
    ruta = dir_indice / modulo / "articulos.jsonl"
    if not ruta.exists():
        return {}
    return {a["id"]: a for a in (json.loads(l) for l in ruta.read_text(encoding="utf-8").splitlines())}


def _requerido(valor, nombre: str):
    if valor is None or (isinstance(valor, str) and valor.strip().upper().startswith("PLACEHOLDER")):
        raise Faltante(f"falta el parámetro {nombre} (pendiente de verificación)")
    return valor


# Tiempo de servicio (aritmética de calendario, sin criterio legal)

def _aniversario(ingreso: date, anio: int) -> date:
    try:
        return ingreso.replace(year=anio)
    except ValueError:  # 29 de febrero en año no bisiesto
        return date(anio, 3, 1)


def antiguedad(ingreso: date, despido: date) -> tuple[int, int, Decimal]:
    """(años cumplidos, días desde el último aniversario, años con fracción)."""
    if despido < ingreso:
        raise Faltante("la fecha de despido es anterior a la de ingreso")
    anios = despido.year - ingreso.year
    if _aniversario(ingreso, despido.year) > despido:
        anios -= 1
    ultimo = _aniversario(ingreso, ingreso.year + anios)
    siguiente = _aniversario(ingreso, ingreso.year + anios + 1)
    dias = (despido - ultimo).days
    fraccion = Decimal(dias) / Decimal((siguiente - ultimo).days)
    return anios, dias, Decimal(anios) + fraccion


def dias_en_anio_del_despido(ingreso: date, despido: date) -> tuple[int, int]:
    """(días trabajados en el año calendario del despido, días de ese año)."""
    inicio = max(date(despido.year, 1, 1), ingreso)
    total = (date(despido.year + 1, 1, 1) - date(despido.year, 1, 1)).days
    return (despido - inicio).days + 1, total


class Calculadora:
    def __init__(self, parametros: dict | None = None, corpus: dict[str, dict] | None = None):
        self.p = parametros or cargar_parametros()
        self.corpus = corpus if corpus is not None else cargar_corpus_citable(self.p["modulo"])

    def salario_diario(self, d: DatosTrabajador) -> tuple[Decimal, str]:
        if d.salario <= 0:
            raise Faltante("el salario debe ser mayor que cero")
        if d.periodo == "diario":
            return d.salario, f"Salario diario reportado: {dinero(d.salario)}"
        if d.periodo not in self.p["divisores_periodo"]:
            raise Faltante(f"periodo de pago desconocido: {d.periodo}")
        divisor = Decimal(str(_requerido(self.p["divisores_periodo"][d.periodo], f"divisores_periodo.{d.periodo}")))
        diario = d.salario / divisor
        return diario, f"Salario diario: {dinero(d.salario)} {d.periodo} ÷ {num(divisor)} = {dinero(diario)}"

    def _fundamento(self, c: dict) -> dict:
        return self._verificar_cita(c.get("fundamento") or {}, "fundamento")

    def _verificar_cita(self, f: dict, campo: str) -> dict:
        art_id = _requerido((f or {}).get("articulo_id"), f"{campo}.articulo_id")
        cita = _requerido((f or {}).get("cita"), f"{campo}.cita")
        if not self.corpus:
            raise Faltante("no hay corpus indexado para verificar el fundamento")
        r = verificar_cita(Cita(art_id, cita), self.corpus)
        if not r.valida:
            raise Faltante(f"el {campo} no se pudo verificar contra el corpus ({r.motivo})")
        return r.articulo

    def _salario_base(self, c: dict, diario: Decimal, pasos: list[str]) -> Decimal:
        tope = (c.get("tope_salario") or {})
        if "multiplo_salario_minimo" not in tope:
            return diario
        multiplo = Decimal(str(_requerido(tope["multiplo_salario_minimo"], "tope_salario.multiplo_salario_minimo")))
        # El tope suele venir de un artículo distinto al del concepto; su cita se verifica igual
        if tope.get("fundamento"):
            self._verificar_cita(tope["fundamento"], "tope_salario.fundamento")
        sm = self.p["salario_minimo_diario"]
        minimo = Decimal(str(_requerido(sm["valor"], "salario_minimo_diario.valor")))
        limite = multiplo * minimo
        if diario > limite:
            pasos.append(
                f"Tope: {num(multiplo)} × salario mínimo {dinero(minimo)} = {dinero(limite)}; se usa {dinero(limite)}"
            )
            return limite
        pasos.append(f"Tope: {num(multiplo)} × salario mínimo {dinero(minimo)} = {dinero(limite)}; no se rebasa")
        return diario

    def _concepto(self, c: dict, d: DatosTrabajador, diario: Decimal, previos: dict[str, ConceptoCalculado]):
        pm = c.get("parametros") or {}
        pasos: list[str] = []
        fundamento = self._fundamento(c)
        tipo = c["tipo"]

        if tipo == "porcentaje_de_concepto":
            base_id = pm.get("concepto_base")
            if base_id not in previos:
                raise Faltante(f"no se pudo calcular el concepto base {base_id}")
            pct = Decimal(str(_requerido(pm.get("porcentaje"), "porcentaje")))
            base = previos[base_id].monto
            monto = base * pct / 100
            pasos.append(f"{num(pct)}% × {dinero(base)} ({previos[base_id].nombre}) = {dinero(monto)}")
            return fundamento, pasos, monto

        salario = self._salario_base(c, diario, pasos)
        if tipo == "dias_fijos":
            dias = Decimal(str(_requerido(pm.get("dias"), "dias")))
            monto = dias * salario
            pasos.append(f"{num(dias)} días × {dinero(salario)} = {dinero(monto)}")
        elif tipo == "dias_por_anio":
            por_anio = Decimal(str(_requerido(pm.get("dias_por_anio"), "dias_por_anio")))
            anios, _, fraccion = antiguedad(d.fecha_ingreso, d.fecha_despido)
            t = fraccion if pm.get("proporcional", True) else Decimal(anios)
            monto = por_anio * t * salario
            pasos.append(f"{num(por_anio)} días × {num(t)} años × {dinero(salario)} = {dinero(monto)}")
        elif tipo == "proporcional_anual":
            anuales = Decimal(str(_requerido(pm.get("dias_anuales"), "dias_anuales")))
            trabajados, total = dias_en_anio_del_despido(d.fecha_ingreso, d.fecha_despido)
            dias = anuales * trabajados / total
            monto = dias * salario
            pasos.append(f"{num(anuales)} días × {trabajados}/{total} días trabajados del año = {num(dias)} días")
            pasos.append(f"{num(dias)} días × {dinero(salario)} = {dinero(monto)}")
        elif tipo == "tabla_por_antiguedad":
            tabla = pm.get("tabla") or []
            if not tabla:
                raise Faltante("falta la tabla de días por antigüedad (pendiente de verificación)")
            anios, dias_desde, _ = antiguedad(d.fecha_ingreso, d.fecha_despido)
            anio_en_curso = anios + 1
            fila = next(
                (f for f in tabla if f["desde_anio"] <= anio_en_curso and (f["hasta_anio"] is None or anio_en_curso <= f["hasta_anio"])),
                None,
            )
            if fila is None:
                raise Faltante(f"la tabla no tiene fila para el año de servicio {anio_en_curso}")
            dias_tabla = Decimal(str(_requerido(fila.get("dias"), "tabla.dias")))
            siguiente = _aniversario(d.fecha_ingreso, d.fecha_ingreso.year + anios + 1)
            ultimo = _aniversario(d.fecha_ingreso, d.fecha_ingreso.year + anios)
            dur = (siguiente - ultimo).days
            dias = dias_tabla * dias_desde / dur
            monto = dias * salario
            pasos.append(
                f"Año de servicio {anio_en_curso}: {num(dias_tabla)} días × {dias_desde}/{dur} días transcurridos = {num(dias)} días"
            )
            pasos.append(f"{num(dias)} días × {dinero(salario)} = {dinero(monto)}")
        else:
            raise Faltante(f"tipo de fórmula desconocido: {tipo}")
        return fundamento, pasos, monto

    def calcular(self, d: DatosTrabajador) -> ResultadoCalculo:
        try:
            diario, paso_diario = self.salario_diario(d)
            anios, dias, fraccion = antiguedad(d.fecha_ingreso, d.fecha_despido)
        except Faltante as e:
            res = ResultadoCalculo(pasos_base=[])
            res.no_calculados.append(ConceptoNoCalculado("base", "Datos base", str(e)))
            return res

        res = ResultadoCalculo(
            pasos_base=[paso_diario, f"Antigüedad: {anios} años y {dias} días = {num(fraccion)} años"],
            advertencias=[a for a in self.p.get("advertencias") or [] if not str(a).startswith("PLACEHOLDER")],
            supuesto=d.supuesto,
        )
        previos: dict[str, ConceptoCalculado] = {}
        for c in self.p["conceptos"]:
            if not c.get("activo", True):
                continue
            supuestos = c.get("supuestos") or []
            if "siempre" not in supuestos and d.supuesto not in supuestos:
                continue
            try:
                fundamento, pasos, monto = self._concepto(c, d, diario, previos)
            except Faltante as e:
                res.no_calculados.append(ConceptoNoCalculado(c["id"], c["nombre"], str(e)))
                continue
            calc = ConceptoCalculado(
                c["id"], c["nombre"], fundamento, pasos, monto.quantize(CENTAVO, ROUND_HALF_UP), bool(c.get("verificado"))
            )
            previos[c["id"]] = calc
            res.calculados.append(calc)
        return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Revisión manual de la calculadora laboral")
    ap.add_argument("--salario", type=Decimal, required=True)
    ap.add_argument("--periodo", default="diario", choices=["diario", "semanal", "quincenal", "mensual"])
    ap.add_argument("--ingreso", type=date.fromisoformat, required=True)
    ap.add_argument("--despido", type=date.fromisoformat, required=True)
    ap.add_argument("--supuesto", default="despido_injustificado")
    args = ap.parse_args(argv)

    from backend.render import render_calculo

    res = Calculadora().calcular(
        DatosTrabajador(args.salario, args.periodo, args.ingreso, args.despido, args.supuesto)
    )
    print(render_calculo(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())

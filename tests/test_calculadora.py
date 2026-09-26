"""Pruebas de la calculadora laboral.

Los parámetros de prueba son valores ABSURDOS a propósito (divisor 10, 7 días, etc.)
para que nadie los confunda con cifras legales reales.
"""

import copy
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from backend.calculadora.laboral import (
    Calculadora,
    DatosTrabajador,
    antiguedad,
    cargar_parametros,
    dias_en_anio_del_despido,
)
from backend.render import render_calculo

CORPUS = {
    "LFP-10": {"id": "LFP-10", "ley": "Ley Ficticia de Pagos", "articulo": "10", "fecha_version": "2000-01-01",
               "texto": "El robot despedido recibirá siete días de lunas por el despido."},
    "LFP-11": {"id": "LFP-11", "ley": "Ley Ficticia de Pagos", "articulo": "11", "fecha_version": "2000-01-01",
               "texto": "Por cada año de servicio corresponden tres días de lunas, con el tope que fije el consejo."},
    "LFP-12": {"id": "LFP-12", "ley": "Ley Ficticia de Pagos", "articulo": "12", "fecha_version": "2000-01-01",
               "texto": "Cada año se pagan cuarenta días de lunas como gratificación anual."},
    "LFP-13": {"id": "LFP-13", "ley": "Ley Ficticia de Pagos", "articulo": "13", "fecha_version": "2000-01-01",
               "texto": "Los días de descanso dependen de los años de servicio según la tabla ficticia."},
    "LFP-14": {"id": "LFP-14", "ley": "Ley Ficticia de Pagos", "articulo": "14", "fecha_version": "2000-01-01",
               "texto": "Sobre los días de descanso se paga un porcentaje adicional ficticio."},
}


CENT = Decimal("0.01")


def fund(art_id, cita):
    return {"articulo_id": art_id, "cita": cita}


PARAMS = {
    "modulo": "prueba",
    "salario_minimo_diario": {"valor": 100, "verificado": True},
    "divisores_periodo": {"semanal": 3, "quincenal": 5, "mensual": 10},
    "advertencias": ["Advertencia ficticia de prueba.", "PLACEHOLDER: no debe mostrarse"],
    "conceptos": [
        {"id": "indemnizacion", "nombre": "Indemnización ficticia", "activo": True, "supuestos": ["despido_injustificado"],
         "tipo": "dias_fijos", "parametros": {"dias": 7},
         "fundamento": fund("LFP-10", "recibirá siete días de lunas por el despido"), "verificado": True},
        {"id": "antiguedad", "nombre": "Prima ficticia", "activo": True, "supuestos": ["siempre"],
         "tipo": "dias_por_anio", "parametros": {"dias_por_anio": 3, "proporcional": True},
         "tope_salario": {"multiplo_salario_minimo": 2},
         "fundamento": fund("LFP-11", "Por cada año de servicio corresponden tres días de lunas"), "verificado": False},
        {"id": "gratificacion", "nombre": "Gratificación ficticia", "activo": True, "supuestos": ["siempre"],
         "tipo": "proporcional_anual", "parametros": {"dias_anuales": 40},
         "fundamento": fund("LFP-12", "Cada año se pagan cuarenta días de lunas"), "verificado": True},
        {"id": "descanso", "nombre": "Descanso ficticio", "activo": True, "supuestos": ["siempre"],
         "tipo": "tabla_por_antiguedad",
         "parametros": {"tabla": [{"desde_anio": 1, "hasta_anio": 5, "dias": 4}, {"desde_anio": 6, "hasta_anio": None, "dias": 9}]},
         "fundamento": fund("LFP-13", "Los días de descanso dependen de los años de servicio"), "verificado": True},
        {"id": "prima_descanso", "nombre": "Prima de descanso ficticia", "activo": True, "supuestos": ["siempre"],
         "tipo": "porcentaje_de_concepto", "parametros": {"porcentaje": 50, "concepto_base": "descanso"},
         "fundamento": fund("LFP-14", "Sobre los días de descanso se paga un porcentaje adicional"), "verificado": True},
        {"id": "inactivo", "nombre": "Inactivo", "activo": False, "supuestos": ["siempre"], "tipo": "dias_fijos",
         "parametros": {"dias": 1}, "fundamento": fund("LFP-10", "x"), "verificado": True},
    ],
}

# 6 años cumplidos el 2026-01-15; del 2026-01-15 al 2026-09-01 hay 229 días de 365
DATOS = DatosTrabajador(Decimal("3000"), "mensual", date(2020, 1, 15), date(2026, 9, 1))


def calcular(params=PARAMS, datos=DATOS):
    return Calculadora(params, CORPUS).calcular(datos)


def por_id(res):
    return {c.id: c for c in res.calculados}


# Aritmética de calendario

def test_antiguedad_exacta():
    anios, dias, fraccion = antiguedad(date(2020, 1, 15), date(2026, 9, 1))
    assert (anios, dias) == (6, 229)
    assert fraccion == Decimal(6) + Decimal(229) / Decimal(365)


def test_antiguedad_ingreso_29_de_febrero():
    anios, dias, _ = antiguedad(date(2020, 2, 29), date(2023, 3, 1))
    assert (anios, dias) == (3, 0)


def test_dias_en_anio_del_despido():
    assert dias_en_anio_del_despido(date(2020, 1, 15), date(2026, 9, 1)) == (244, 365)
    assert dias_en_anio_del_despido(date(2026, 3, 1), date(2026, 3, 10)) == (10, 365)


# Conceptos

def test_salario_diario_con_divisor():
    assert calcular().pasos_base[0] == "Salario diario: $3,000.00 mensual ÷ 10 = $300.00"


def test_dias_fijos():
    c = por_id(calcular())["indemnizacion"]
    assert c.monto == Decimal("2100.00")
    assert c.pasos == ["7 días × $300.00 = $2,100.00"]


def test_dias_por_anio_con_tope():
    c = por_id(calcular())["antiguedad"]
    # salario 300 > tope 2 × 100 = 200
    assert c.pasos[0] == "Tope: 2 × salario mínimo $100.00 = $200.00; se usa $200.00"
    esperado = (Decimal(3) * (Decimal(6) + Decimal(229) / Decimal(365)) * Decimal(200)).quantize(CENT, ROUND_HALF_UP)
    assert c.monto == esperado


def test_proporcional_anual():
    c = por_id(calcular())["gratificacion"]
    assert c.monto == (Decimal(40) * 244 / 365 * 300).quantize(CENT, ROUND_HALF_UP)


def test_tabla_y_porcentaje_de_concepto():
    res = por_id(calcular())
    # año de servicio en curso: 7 → fila de 9 días, proporcional 229/365
    assert res["descanso"].monto == (Decimal(9) * 229 / 365 * 300).quantize(CENT, ROUND_HALF_UP)
    assert res["prima_descanso"].monto == (res["descanso"].monto * Decimal("0.5")).quantize(CENT, ROUND_HALF_UP)


def test_inactivos_y_supuestos():
    ids = por_id(calcular()).keys()
    assert "inactivo" not in ids
    otro = DatosTrabajador(DATOS.salario, DATOS.periodo, DATOS.fecha_ingreso, DATOS.fecha_despido, supuesto="renuncia")
    assert "indemnizacion" not in por_id(calcular(datos=otro))


def test_total_y_verificacion():
    res = calcular()
    assert res.total == sum(c.monto for c in res.calculados)
    assert not res.todo_verificado  # "antiguedad" tiene verificado: false


# Reglas de seguridad

def test_parametro_null_no_calcula():
    p = copy.deepcopy(PARAMS)
    p["conceptos"][0]["parametros"]["dias"] = None
    res = calcular(p)
    assert "indemnizacion" not in por_id(res)
    assert any(n.id == "indemnizacion" and "falta el parámetro dias" in n.motivo for n in res.no_calculados)


def test_fundamento_no_literal_no_calcula():
    p = copy.deepcopy(PARAMS)
    p["conceptos"][0]["fundamento"]["cita"] = "recibirá noventa días de lunas por el despido"
    res = calcular(p)
    assert "indemnizacion" not in por_id(res)
    assert "no se pudo verificar contra el corpus" in res.no_calculados[0].motivo


def test_fundamento_fuera_del_corpus_no_calcula():
    p = copy.deepcopy(PARAMS)
    p["conceptos"][0]["fundamento"]["articulo_id"] = "LFP-99"
    assert "indemnizacion" not in por_id(calcular(p))


def test_sin_corpus_no_calcula_nada():
    res = Calculadora(PARAMS, {}).calcular(DATOS)
    assert res.calculados == []


def test_concepto_base_faltante_arrastra():
    p = copy.deepcopy(PARAMS)
    p["conceptos"][3]["parametros"]["tabla"] = []
    ids = por_id(calcular(p)).keys()
    assert "descanso" not in ids and "prima_descanso" not in ids


def test_divisor_null_no_calcula():
    p = copy.deepcopy(PARAMS)
    p["divisores_periodo"]["mensual"] = None
    res = calcular(p)
    assert res.calculados == [] and "divisores_periodo.mensual" in res.no_calculados[0].motivo


def test_fechas_invertidas():
    d = DatosTrabajador(Decimal(100), "diario", date(2026, 1, 1), date(2025, 1, 1))
    assert calcular(datos=d).calculados == []


def test_yaml_real_no_trae_cifras_legales():
    """El YAML del repositorio no debe traer ningún valor legal: todo es null o PLACEHOLDER."""
    p = cargar_parametros()
    assert p["salario_minimo_diario"]["valor"] is None
    assert all(v is None for v in p["divisores_periodo"].values())
    for c in p["conceptos"]:
        assert c["verificado"] is False
        valores = [v for k, v in c["parametros"].items() if k not in ("proporcional", "concepto_base")]
        assert all(v in (None, []) for v in valores), c["id"]
        assert c["fundamento"]["articulo_id"] == "PLACEHOLDER"
    res = Calculadora(p, CORPUS).calcular(DATOS)
    assert res.calculados == []


# Formato

def test_render_marca_pendientes_y_muestra_formula():
    texto = render_calculo(calcular())
    assert "Escenario: si el despido resulta injustificado." in texto
    assert "• Indemnización ficticia\n  Fundamento: Ley Ficticia de Pagos, art. 10, versión 2000-01-01" in texto
    assert "7 días × $300.00 = $2,100.00" in texto
    assert "[PENDIENTE DE VERIFICACIÓN]" in texto  # la prima ficticia no está verificada
    assert "[INCLUYE CIFRAS PENDIENTES DE VERIFICACIÓN]" in texto
    assert "Advertencia ficticia de prueba." in texto and "PLACEHOLDER" not in texto


def test_render_todo_verificado_sin_marcas():
    p = copy.deepcopy(PARAMS)
    p["conceptos"][1]["verificado"] = True
    texto = render_calculo(calcular(p))
    assert "PENDIENTE" not in texto


def test_render_sin_calculos():
    texto = render_calculo(Calculadora(PARAMS, {}).calcular(DATOS))
    assert "Todavía no puedo estimar montos" in texto
    assert "$" not in texto

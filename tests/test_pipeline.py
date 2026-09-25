"""Pruebas del pipeline completo con un LLM simulado y el corpus ficticio de naves."""

from types import SimpleNamespace

import pytest

from backend.esquemas import CitaModelo, DatoFaltante, Derecho, Paso, RespuestaGenerada, Triaje
from backend.llm import ClaudeLLM, ErrorLLM, contexto_articulos
from backend.pipeline import CUANDO_ABOGADO_GENERICO, Pipeline

CITA_BUENA = CitaModelo(articulo_id="LFN-3", texto_literal="tiene derecho a recibir un aviso escrito con la causa del despido")
CITA_INVENTADA = CitaModelo(articulo_id="LFN-3", texto_literal="tiene derecho a una indemnización de cinco lunas")

CONFIG = {
    "recuperacion": {"k": 3, "rrf_k": 60},
    "verificacion": {"umbral_similitud": 0.2, "umbral_calibrado": False, "cita_min_caracteres": 20},
}

INSTITUCIONES_GENERAL = """
instituciones:
  - {id: emergencias, nombre: "Emergencias Ficticias", cuando: "peligro", contacto: "000", tipo: emergencia, verificado: true}
  - {id: defensoria, nombre: "Defensoría Ficticia", cuando: "general", contacto: "PLACEHOLDER", tipo: canalizacion, verificado: false}
"""
INSTITUCIONES_NAVES = """
instituciones:
  - {id: consejo_robots, nombre: "Consejo Ficticio de Robots", cuando: "despidos de robots", contacto: "111", tipo: canalizacion, verificado: true}
"""


def triaje(**cambios) -> Triaje:
    base = dict(
        es_consulta_legal=True,
        area="transito",
        urgencia="media",
        motivo_urgencia="despido reciente",
        hechos=["un robot fue despedido"],
        datos_faltantes=[],
        consulta_reformulada="robot despedido de estación orbital sin aviso escrito",
        datos_laborales=None,
    )
    return Triaje(**{**base, **cambios})


def respuesta(**cambios) -> RespuestaGenerada:
    base = dict(
        que_esta_pasando="Te despidieron de la estación orbital.",
        derechos=[Derecho(explicacion="Deben darte un aviso escrito.", cita=CITA_BUENA)],
        que_hacer=[Paso(accion="Guarda cualquier mensaje o documento del despido.", cita=None)],
        instituciones=["consejo_robots"],
        cuando_abogado="Si la estación no te da el aviso o te pide firmar algo que no entiendes.",
        sin_respaldo=[],
    )
    return RespuestaGenerada(**{**base, **cambios})


class LLMFalso:
    def __init__(self, triajes, generada=None, error_generar=False):
        self.triajes = list(triajes)
        self.generada = generada or respuesta()
        self.error_generar = error_generar
        self.mensajes_triaje = []
        self.articulos_recibidos = None

    def triaje(self, mensaje):
        self.mensajes_triaje.append(mensaje)
        return self.triajes.pop(0) if len(self.triajes) > 1 else self.triajes[0]

    def generar(self, mensaje, triaje, articulos, instituciones):
        if self.error_generar:
            raise ErrorLLM("falla simulada")
        self.articulos_recibidos = articulos
        return self.generada


@pytest.fixture
def armar(corpus_naves, tmp_path):
    dir_inst = tmp_path / "instituciones"
    dir_inst.mkdir()
    (dir_inst / "general.yaml").write_text(INSTITUCIONES_GENERAL, encoding="utf-8")
    (dir_inst / "naves.yaml").write_text(INSTITUCIONES_NAVES, encoding="utf-8")

    def _armar(llm, config=CONFIG, calculadora=lambda m: None):
        return Pipeline(
            llm,
            recuperador=lambda m: corpus_naves["rec"],
            modulos_de_area=lambda area: ["naves"] if area == "transito" else [],
            config=config,
            dir_instituciones=dir_inst,
            calculadora=calculadora,
        )

    return _armar


MENSAJE = "soy robot y me despidieron de la estación orbital sin aviso escrito"


def test_respuesta_completa_con_cita_verificada(armar):
    r = armar(LLMFalso([triaje()])).procesar("u1", MENSAJE)
    assert r.tipo == "respuesta"
    for seccion in ["*Qué está pasando*", "*Tus derechos*", "*Qué hacer ahora*", "*A dónde acudir*", "*Cuándo necesitas un abogado*"]:
        assert seccion in r.texto
    assert f'"{CITA_BUENA.texto_literal}"' in r.texto
    assert "Ley Ficticia de Naves y Robots, art. 3 (versión 2000-01-01)" in r.texto
    assert "Consejo Ficticio de Robots: 111" in r.texto
    assert r.traza["citas_validas"] == ["LFN-3"]


def test_cita_inventada_se_elimina_y_sin_derechos_se_abstiene(armar):
    llm = LLMFalso([triaje()], respuesta(derechos=[Derecho(explicacion="Te deben cinco lunas.", cita=CITA_INVENTADA)]))
    r = armar(llm).procesar("u1", MENSAJE)
    assert r.tipo == "abstencion"
    assert "cinco lunas" not in r.texto
    assert r.traza["descartes"][0]["motivo"].startswith("no aparece literalmente")
    assert "Defensoría Ficticia" in r.texto and "[PENDIENTE DE VERIFICACIÓN]" in r.texto


def test_mezcla_de_citas_conserva_solo_la_verificada(armar):
    gen = respuesta(
        derechos=[
            Derecho(explicacion="Deben darte un aviso escrito.", cita=CITA_BUENA),
            Derecho(explicacion="Te deben cinco lunas.", cita=CITA_INVENTADA),
        ]
    )
    r = armar(LLMFalso([triaje()], gen)).procesar("u1", MENSAJE)
    assert r.tipo == "respuesta"
    assert "cinco lunas" not in r.texto
    assert "Omití parte de la respuesta" in r.texto


def test_cita_a_articulo_no_recuperado_se_rechaza(armar):
    otra = CitaModelo(articulo_id="LFN-99", texto_literal="tiene derecho a recibir un aviso escrito")
    r = armar(LLMFalso([triaje()], respuesta(derechos=[Derecho(explicacion="x", cita=otra)]))).procesar("u1", MENSAJE)
    assert r.tipo == "abstencion"
    assert "no estaba entre los recuperados" in r.traza["descartes"][0]["motivo"]


def test_paso_sin_cita_con_plazo_se_descarta(armar):
    gen = respuesta(
        que_hacer=[
            Paso(accion="Presenta tu queja dentro de 15 días.", cita=None),
            Paso(accion="Anota la fecha y la hora del despido.", cita=None),
        ]
    )
    r = armar(LLMFalso([triaje()], gen)).procesar("u1", MENSAJE)
    assert "15 días" not in r.texto
    assert "Anota la fecha" in r.texto


def test_paso_con_cita_valida_se_conserva_con_fuente(armar):
    cita4 = CitaModelo(articulo_id="LFN-4", texto_literal="podrá acudir ante el Consejo Ficticio de Robots dentro del plazo de veinte ciclos")
    gen = respuesta(que_hacer=[Paso(accion="Acude al Consejo dentro del plazo.", cita=cita4)])
    llm = LLMFalso([triaje(consulta_reformulada="robot despedido consejo plazo aviso escrito")], gen)
    r = armar(llm).procesar("u1", MENSAJE)
    assert "LFN-4" in [a["id"] for a in llm.articulos_recibidos]
    assert "Acude al Consejo dentro del plazo. (Ley Ficticia de Naves y Robots, art. 4" in r.texto


def test_cuando_abogado_con_cifra_se_sustituye(armar):
    gen = respuesta(cuando_abogado="Si pasan más de 30 días sin respuesta.")
    r = armar(LLMFalso([triaje()], gen)).procesar("u1", MENSAJE)
    assert "30 días" not in r.texto and CUANDO_ABOGADO_GENERICO in r.texto


def test_institucion_fuera_de_catalogo_se_ignora(armar):
    gen = respuesta(instituciones=["inventada"])
    r = armar(LLMFalso([triaje()], gen)).procesar("u1", MENSAJE)
    assert "inventada" not in r.texto
    assert "Consejo Ficticio de Robots" in r.texto  # canalización por defecto del catálogo


def test_mensaje_no_legal_da_bienvenida(armar):
    r = armar(LLMFalso([triaje(es_consulta_legal=False, area="fuera_de_alcance")])).procesar("u1", "hola")
    assert r.tipo == "no_legal" and "Primeros Auxilios Legales" in r.texto


def test_area_sin_modulo_se_abstiene_y_canaliza(armar):
    r = armar(LLMFalso([triaje(area="penal")])).procesar("u1", "me acusan de algo")
    assert r.tipo == "abstencion"
    assert "Todavía no tengo cargada la ley" in r.texto
    assert "Defensoría Ficticia" in r.texto


def test_umbral_no_alcanzado_se_abstiene(armar):
    config = {**CONFIG, "verificacion": {**CONFIG["verificacion"], "umbral_similitud": 0.99}}
    llm = LLMFalso([triaje(consulta_reformulada="receta de mole")])
    r = armar(llm, config).procesar("u1", "receta de mole poblano")
    assert r.tipo == "abstencion"
    assert llm.articulos_recibidos is None  # nunca se llamó a la generación


def test_dato_critico_faltante_pregunta_una_sola_vez(armar):
    falta = DatoFaltante(dato="fecha", pregunta="¿Cuándo te despidieron?", critico=True)
    llm = LLMFalso([triaje(datos_faltantes=[falta]), triaje(datos_faltantes=[falta])])
    p = armar(llm)
    r1 = p.procesar("u1", MENSAJE)
    assert r1.tipo == "pregunta" and "¿Cuándo te despidieron?" in r1.texto
    r2 = p.procesar("u1", "ayer")
    assert r2.tipo == "respuesta"
    assert MENSAJE in llm.mensajes_triaje[1] and "ayer" in llm.mensajes_triaje[1]


def test_sesiones_son_por_usuario(armar):
    falta = DatoFaltante(dato="fecha", pregunta="¿Cuándo?", critico=True)
    p = armar(LLMFalso([triaje(datos_faltantes=[falta])]))
    assert p.procesar("u1", MENSAJE).tipo == "pregunta"
    assert p.procesar("u2", MENSAJE).tipo == "pregunta"


def test_urgencia_alta_no_pregunta_y_antepone_emergencias(armar):
    falta = DatoFaltante(dato="fecha", pregunta="¿Cuándo?", critico=True)
    r = armar(LLMFalso([triaje(urgencia="alta", datos_faltantes=[falta])])).procesar("u1", MENSAJE)
    assert r.tipo == "respuesta"
    assert r.texto.startswith("*Si estás en peligro ahora mismo")
    assert "Emergencias Ficticias: 000" in r.texto


def test_error_del_llm_da_mensaje_seguro(armar):
    r = armar(LLMFalso([triaje()], error_generar=True)).procesar("u1", MENSAJE)
    assert r.tipo == "error" and "problema técnico" in r.texto


# Cliente de Claude (sin red)

def test_contexto_articulos_no_altera_el_texto():
    art = {"id": "X-1", "ley": 'Ley "A" & B', "articulo": "1", "fecha_version": "2000", "texto": "a < b & c"}
    ctx = contexto_articulos([art])
    assert "a < b & c" in ctx
    assert 'ley=\'Ley "A" &amp; B\'' in ctx


class _ClienteFalso:
    def __init__(self, stop_reason, parsed):
        self.messages = SimpleNamespace(parse=lambda **kw: SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, _request_id="req_x"))


def test_claude_llm_rechaza_respuesta_truncada():
    llm = ClaudeLLM(_ClienteFalso("max_tokens", None))
    with pytest.raises(ErrorLLM, match="max_tokens"):
        llm.triaje("hola")


def test_claude_llm_devuelve_objeto_validado():
    t = triaje()
    assert ClaudeLLM(_ClienteFalso("end_turn", t)).triaje("hola") is t


# Calculadora integrada (parámetros ABSURDOS de prueba)

def _calculadora_prueba(corpus_naves):
    from backend.calculadora.laboral import Calculadora

    corpus = {a["id"]: a for a in corpus_naves["rec"].articulos}
    params = {
        "modulo": "naves",
        "salario_minimo_diario": {"valor": None},
        "divisores_periodo": {"mensual": 10},
        "advertencias": [],
        "conceptos": [
            {"id": "c1", "nombre": "Concepto ficticio", "supuestos": ["siempre"], "tipo": "dias_fijos",
             "parametros": {"dias": 7}, "verificado": False,
             "fundamento": {"articulo_id": "LFN-3", "cita": "tiene derecho a recibir un aviso escrito"}},
        ],
    }
    return Calculadora(params, corpus)


def test_montos_los_calcula_el_codigo(armar, corpus_naves):
    from backend.esquemas import DatosLaborales

    dl = DatosLaborales(salario=3000, periodo_salario="mensual", fecha_ingreso="2020-01-15",
                        fecha_despido="2026-09-01", pide_montos=True)
    calc = _calculadora_prueba(corpus_naves)
    r = armar(LLMFalso([triaje(datos_laborales=dl)]), calculadora=lambda m: calc).procesar("u1", MENSAJE)
    assert r.tipo == "respuesta"
    assert "*Cuánto te podría corresponder (estimación)*" in r.texto
    assert "  7 días × $300.00 = $2,100.00\n  = $2,100.00 [PENDIENTE DE VERIFICACIÓN]" in r.texto
    assert r.traza["calculo"]["calculados"] == {"c1": "2100.00"}


def test_montos_pedidos_sin_datos_pide_los_datos(armar, corpus_naves):
    from backend.esquemas import DatosLaborales
    from backend.pipeline import PIDE_DATOS_CALCULO

    dl = DatosLaborales(salario=None, periodo_salario=None, fecha_ingreso=None, fecha_despido=None, pide_montos=True)
    calc = _calculadora_prueba(corpus_naves)
    r = armar(LLMFalso([triaje(datos_laborales=dl)]), calculadora=lambda m: calc).procesar("u1", MENSAJE)
    assert PIDE_DATOS_CALCULO in r.texto and "$" not in r.texto


def test_claude_llm_sin_credenciales_es_error_controlado():
    def sin_llave(**kw):
        raise TypeError('"Could not resolve authentication method. Expected one of api_key..."')

    llm = ClaudeLLM(SimpleNamespace(messages=SimpleNamespace(parse=sin_llave)))
    with pytest.raises(ErrorLLM, match="ANTHROPIC_API_KEY"):
        llm.triaje("hola")

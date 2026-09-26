"""Orquestación: triaje, recuperación, compuerta 1, generación, compuerta 2, formato.

Invariante: ningún derecho ni paso con contenido legal llega al usuario sin una cita
que el código haya cotejado literalmente contra el artículo recuperado. Si no queda
ningún derecho verificado, la respuesta es una abstención con canalización.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Literal

from backend import render
from backend.calculadora.laboral import Calculadora, DatosTrabajador
from backend.esquemas import CitaModelo, DatosLaborales, RespuestaGenerada, Triaje
from ingestion.ingest import cargar_modulos
from backend.instituciones import DIR_INSTITUCIONES, Institucion, catalogo, por_tipo
from backend.llm import ClienteLLM, ErrorLLM
from backend.sesiones import Sesiones
from rag.config import cargar_config
from rag.retriever import Recuperado, Recuperador, modulos_por_area, recuperador_para
from rag.verifier import Cita, evaluar_recuperacion, verificar_cita

log = logging.getLogger("pipeline")

# Un paso sin cita no puede mencionar plazos, cifras ni artículos (sería una afirmación legal sin respaldo)
CONTENIDO_LEGAL_SIN_CITA = re.compile(
    r"\d+\s*(?:d[ií]as?|mes(?:es)?|años?|horas?|%|por\s*ciento|salarios?)|art[ií]culo|\bart\.|\$\s*\d",
    re.IGNORECASE,
)

CUANDO_ABOGADO_GENERICO = (
    "Si recibes un citatorio o una demanda, si la otra parte no cumple, o si no estás seguro de "
    "cómo aplica esto a tu caso, busca asesoría con un abogado o con alguna de las instituciones indicadas."
)

Tipo = Literal["respuesta", "pregunta", "abstencion", "no_legal", "error"]


@dataclass
class Resultado:
    tipo: Tipo
    texto: str
    traza: dict = field(default_factory=dict)


def calculadora_de_modulo(modulo: str) -> Calculadora | None:
    """Calculadora asociada a un módulo en data/modules.yaml (campo "calculadora")."""
    conf = cargar_modulos().get(modulo, {})
    if conf.get("calculadora") == "laboral":
        return Calculadora()
    return None


def datos_para_calculo(dl: DatosLaborales | None) -> DatosTrabajador | None:
    if dl is None or dl.salario is None or dl.periodo_salario is None:
        return None
    try:
        ingreso = date.fromisoformat(dl.fecha_ingreso or "")
        despido = date.fromisoformat(dl.fecha_despido or "")
    except ValueError:
        return None
    return DatosTrabajador(Decimal(str(dl.salario)), dl.periodo_salario, ingreso, despido)


PIDE_DATOS_CALCULO = (
    "*Cuánto te podría corresponder*\nPara estimarlo necesito tu salario y cada cuánto te pagan, "
    "tu fecha de ingreso y la fecha del despido (día, mes y año)."
)


def consulta_de_busqueda(mensaje: str, triaje: Triaje) -> str:
    """Texto con el que se busca: la reformulación jurídica más el mensaje original
    (el original conserva referencias explícitas como "artículo 48")."""
    return f"{triaje.consulta_reformulada}\n{mensaje}"


class Pipeline:
    def __init__(
        self,
        llm: ClienteLLM,
        recuperador: Callable[[str], Recuperador] = recuperador_para,
        modulos_de_area: Callable[[str], list[str]] = modulos_por_area,
        config: dict | None = None,
        dir_instituciones=DIR_INSTITUCIONES,
        sesiones: Sesiones | None = None,
        calculadora: Callable[[str], Calculadora | None] = calculadora_de_modulo,
    ):
        self.calculadora = calculadora
        self.llm = llm
        self.recuperador = recuperador
        self.modulos_de_area = modulos_de_area
        self.config = config or cargar_config()
        self.dir_instituciones = dir_instituciones
        self.sesiones = sesiones or Sesiones()

    def procesar(self, usuario: str, mensaje: str) -> Resultado:
        traza: dict = {}
        pendiente = self.sesiones.tomar_pendiente(usuario)
        ya_pregunto = pendiente is not None
        if pendiente:
            mensaje = f"{pendiente.mensaje_original}\n(Pregunta: {pendiente.pregunta} Respuesta: {mensaje})"
        traza["mensaje"] = mensaje

        general = catalogo([], self.dir_instituciones)
        try:
            triaje = self.llm.triaje(mensaje)
        except ErrorLLM as e:
            return self._error(e, "triaje", traza, general)
        traza["triaje"] = triaje.model_dump()

        if not triaje.es_consulta_legal:
            return Resultado("no_legal", render.BIENVENIDA, traza)

        modulos = [] if triaje.area == "fuera_de_alcance" else self.modulos_de_area(triaje.area)
        traza["modulos"] = modulos
        cat = catalogo(modulos, self.dir_instituciones)
        urgente = por_tipo(cat, "emergencia") if triaje.urgencia == "alta" else None
        canalizacion = por_tipo(cat, "canalizacion")

        # Sin módulo para el área no tiene sentido pedir más datos: se canaliza de inmediato
        if not modulos:
            traza["abstencion"] = "área sin módulo"
            return Resultado(
                "abstencion",
                render.render_abstencion("Todavía no tengo cargada la ley que aplica a tu caso.", canalizacion, urgente),
                traza,
            )

        criticos = [d for d in triaje.datos_faltantes if d.critico]
        if criticos and not ya_pregunto and triaje.urgencia != "alta":
            self.sesiones.guardar_pendiente(usuario, mensaje, criticos[0].pregunta)
            return Resultado("pregunta", render.render_pregunta(criticos[0].pregunta), traza)

        consulta = consulta_de_busqueda(mensaje, triaje)
        k = self.config["recuperacion"]["k"]
        recuperados: list[Recuperado] = []
        for m in modulos:
            recuperados += self.recuperador(m).buscar(consulta, k=k)
        recuperados.sort(key=lambda r: (not r.referencia_explicita, -r.similitud))
        v = self.config["verificacion"]
        decision = evaluar_recuperacion(recuperados[:k], v["umbral_similitud"], v["umbral_calibrado"])
        traza["recuperacion"] = {
            "contestar": decision.contestar,
            "motivo": decision.motivo,
            "umbral": decision.umbral,
            "umbral_calibrado": decision.umbral_calibrado,
            "articulos": [{"id": r.id, "similitud": round(r.similitud, 4)} for r in recuperados[:k]],
        }
        if not decision.contestar:
            return Resultado(
                "abstencion",
                render.render_abstencion(
                    "No encontré en la ley que tengo cargada un artículo que responda con seguridad tu caso.",
                    canalizacion,
                    urgente,
                ),
                traza,
            )

        articulos = [r.articulo for r in decision.contexto]
        try:
            generada = self.llm.generar(mensaje, triaje, articulos, list(cat.values()))
        except ErrorLLM as e:
            return self._error(e, "generacion", traza, cat)

        verificada, descartes = verificar_respuesta(generada, {a["id"]: a for a in articulos}, cat, v["cita_min_caracteres"])
        traza["citas_validas"] = [art["id"] for _, art in verificada.derechos] + [
            art["id"] for _, art in verificada.que_hacer if art
        ]
        traza["descartes"] = descartes

        # Basta con que quede alguna cita verificada, esté en los derechos o en los pasos:
        # a veces el modelo expresa la regla como un paso a seguir y no como un derecho.
        if not verificada.derechos and not any(art for _, art in verificada.que_hacer):
            traza["abstencion"] = "ninguna cita verificable"
            return Resultado(
                "abstencion",
                render.render_abstencion(
                    "No pude respaldar una respuesta con el texto exacto de la ley.", canalizacion, urgente
                ),
                traza,
            )
        if not verificada.instituciones:
            verificada.instituciones = canalizacion
        verificada.calculo = self._calculo(modulos, triaje, traza)
        return Resultado("respuesta", render.render_respuesta(verificada, urgente), traza)

    def _calculo(self, modulos: list[str], triaje: Triaje, traza: dict) -> str | None:
        """Montos por código puro (nunca por el modelo). None si no aplica."""
        calc = next((c for c in map(self.calculadora, modulos) if c is not None), None)
        dl = triaje.datos_laborales
        if calc is None or dl is None:
            return None
        datos = datos_para_calculo(dl)
        if datos is None:
            traza["calculo"] = "faltan datos"
            return PIDE_DATOS_CALCULO if dl.pide_montos else None
        res = calc.calcular(datos)
        traza["calculo"] = {
            "calculados": {c.id: str(c.monto) for c in res.calculados},
            "no_calculados": {n.id: n.motivo for n in res.no_calculados},
            "todo_verificado": res.todo_verificado,
        }
        return render.render_calculo(res)

    def _error(self, e: Exception, etapa: str, traza: dict, cat: dict[str, Institucion]) -> Resultado:
        log.error("Error en %s: %s", etapa, e)
        traza["error"] = f"{etapa}: {e}"
        return Resultado(
            "error",
            render.render_abstencion(
                "Tuve un problema técnico y no pude revisar tu caso en este momento. Intenta de nuevo en unos minutos.",
                por_tipo(cat, "canalizacion"),
            ),
            traza,
        )


def verificar_respuesta(
    gen: RespuestaGenerada, contexto: dict[str, dict], cat: dict[str, Institucion], min_caracteres: int
) -> tuple[render.RespuestaVerificada, list[dict]]:
    """Compuerta 2 aplicada a la respuesta completa. Devuelve la respuesta depurada y lo descartado."""
    descartes = []

    def cotejar(c: CitaModelo):
        return verificar_cita(Cita(c.articulo_id, c.texto_literal), contexto, min_caracteres)

    derechos = []
    for d in gen.derechos:
        r = cotejar(d.cita)
        if r.valida:
            derechos.append((d, r.articulo))
        else:
            descartes.append({"seccion": "derechos", "id": d.cita.articulo_id, "motivo": r.motivo})

    pasos = []
    for p in gen.que_hacer:
        if p.cita is None:
            if CONTENIDO_LEGAL_SIN_CITA.search(p.accion):
                descartes.append({"seccion": "que_hacer", "id": None, "motivo": "paso con contenido legal sin cita"})
            else:
                pasos.append((p, None))
            continue
        r = cotejar(p.cita)
        if r.valida:
            pasos.append((p, r.articulo))
        else:
            descartes.append({"seccion": "que_hacer", "id": p.cita.articulo_id, "motivo": r.motivo})

    cuando_abogado = gen.cuando_abogado
    if CONTENIDO_LEGAL_SIN_CITA.search(cuando_abogado):
        descartes.append({"seccion": "cuando_abogado", "id": None, "motivo": "plazo o cifra sin cita"})
        cuando_abogado = CUANDO_ABOGADO_GENERICO

    instituciones = [cat[i] for i in dict.fromkeys(gen.instituciones) if i in cat]
    desconocidas = [i for i in gen.instituciones if i not in cat]
    if desconocidas:
        descartes.append({"seccion": "instituciones", "id": desconocidas, "motivo": "id fuera del catálogo"})

    return (
        render.RespuestaVerificada(
            que_esta_pasando=gen.que_esta_pasando,
            derechos=derechos,
            que_hacer=pasos,
            instituciones=instituciones,
            cuando_abogado=cuando_abogado,
            sin_respaldo=gen.sin_respaldo,
            hubo_descartes=any(d["seccion"] in ("derechos", "que_hacer") for d in descartes),
        ),
        descartes,
    )

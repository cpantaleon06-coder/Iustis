"""Formato de salida fijo para WhatsApp (negritas con *asteriscos*, cursivas con _guiones bajos_)."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.esquemas import Derecho, Paso
from backend.instituciones import Institucion

AVISO = "_Orientación general basada en la ley citada. No sustituye la asesoría de un abogado._"


@dataclass
class RespuestaVerificada:
    que_esta_pasando: str
    derechos: list[tuple[Derecho, dict]]
    que_hacer: list[tuple[Paso, dict | None]]
    instituciones: list[Institucion]
    cuando_abogado: str
    sin_respaldo: list[str] = field(default_factory=list)
    hubo_descartes: bool = False
    calculo: str | None = None  # sección ya formateada por render_calculo


def cita_fuente(art: dict) -> str:
    return f"{art['ley']}, art. {art['articulo']}, versión {art['fecha_version']}"


def linea_institucion(i: Institucion) -> str:
    marca = " [PENDIENTE DE VERIFICACIÓN]" if i.pendiente else ""
    return f"• {i.nombre}: {i.contacto}{marca}"


def bloque_urgente(emergencias: list[Institucion]) -> str:
    lineas = ["*Si estás en peligro ahora mismo, pide ayuda primero:*"]
    lineas += [linea_institucion(i) for i in emergencias]
    return "\n".join(lineas)


def render_respuesta(r: RespuestaVerificada, urgente: list[Institucion] | None = None) -> str:
    partes = []
    if urgente:
        partes.append(bloque_urgente(urgente))
    partes.append(f"*Qué está pasando*\n{r.que_esta_pasando}")

    derechos = ["*Tus derechos*"]
    for d, art in r.derechos:
        derechos.append(f"• {d.explicacion}\n  \"{d.cita.texto_literal}\"\n  ({cita_fuente(art)})")
    partes.append("\n".join(derechos))

    if r.que_hacer:
        pasos = ["*Qué hacer ahora*"]
        for n, (p, art) in enumerate(r.que_hacer, start=1):
            linea = f"{n}. {p.accion}"
            if art:
                linea += f" ({cita_fuente(art)})"
            pasos.append(linea)
        partes.append("\n".join(pasos))

    if r.calculo:
        partes.append(r.calculo)

    if r.instituciones:
        partes.append("\n".join(["*A dónde acudir*"] + [linea_institucion(i) for i in r.instituciones]))

    partes.append(f"*Cuándo necesitas un abogado*\n{r.cuando_abogado}")

    if r.sin_respaldo or r.hubo_descartes:
        nota = ["*Lo que no puedo confirmar*"]
        nota += [f"• {s}" for s in r.sin_respaldo]
        if r.hubo_descartes:
            nota.append("• Omití parte de la respuesta porque no pude verificarla contra el texto de la ley.")
        nota.append("Para esto, consulta con alguna de las instituciones de arriba.")
        partes.append("\n".join(nota))

    partes.append(AVISO)
    return "\n\n".join(partes)


def render_abstencion(
    motivo: str, canalizacion: list[Institucion], urgente: list[Institucion] | None = None
) -> str:
    partes = []
    if urgente:
        partes.append(bloque_urgente(urgente))
    partes.append(
        f"{motivo}\n\nPara no darte información equivocada, prefiero no responder algo que no puedo "
        "respaldar con un artículo de ley."
    )
    if canalizacion:
        partes.append("\n".join(["*A dónde acudir*"] + [linea_institucion(i) for i in canalizacion]))
    partes.append(AVISO)
    return "\n\n".join(partes)


def render_pregunta(pregunta: str, urgente: list[Institucion] | None = None) -> str:
    texto = f"Para orientarte bien necesito un dato más:\n\n{pregunta}"
    return f"{bloque_urgente(urgente)}\n\n{texto}" if urgente else texto


BIENVENIDA = (
    "Hola, soy *Primeros Auxilios Legales*. Cuéntame en tus palabras qué te pasó "
    "(por ejemplo: \"me despidieron sin darme nada por escrito\" o \"se llevaron mi carro al corralón\") "
    "y te digo qué derechos tienes, qué hacer y a dónde acudir."
)


ESCENARIOS = {"despido_injustificado": "si el despido resulta injustificado"}
PENDIENTE = "[PENDIENTE DE VERIFICACIÓN]"


def render_calculo(res) -> str:
    """Sección de montos a partir de un ResultadoCalculo (backend.calculadora.laboral)."""
    from backend.calculadora.laboral import dinero

    titulo = "*Cuánto te podría corresponder (estimación)*"
    if not res.calculados:
        base = next((n for n in res.no_calculados if n.id == "base"), None)
        motivo = base.motivo if base else "la calculadora tiene parámetros pendientes de verificación"
        return f"{titulo}\nTodavía no puedo estimar montos: {motivo}."

    lineas = [titulo]
    if res.supuesto in ESCENARIOS:
        lineas.append(f"Escenario: {ESCENARIOS[res.supuesto]}.")
    lineas += res.pasos_base
    for c in res.calculados:
        lineas.append(f"\n• {c.nombre}\n  Fundamento: {cita_fuente(c.fundamento)}")
        lineas += [f"  {p}" for p in c.pasos]
        lineas.append(f"  = {dinero(c.monto)}" + ("" if c.verificado else f" {PENDIENTE}"))
    total = f"\n*Total estimado: {dinero(res.total)}*"
    if not res.todo_verificado:
        total += " [INCLUYE CIFRAS PENDIENTES DE VERIFICACIÓN]"
    lineas.append(total)
    if res.no_calculados:
        lineas.append("No pude calcular: " + "; ".join(f"{n.nombre} ({n.motivo})" for n in res.no_calculados))
    lineas += [f"_{a}_" for a in res.advertencias]
    lineas.append("_Es una estimación con los datos que me diste, no una liquidación oficial._")
    return "\n".join(lineas)

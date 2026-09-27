# Iustis

**Iustis da los Primeros Auxilios Legales y te ayuda a saber dónde acudir.**

Asistente de triaje legal de primer contacto por **WhatsApp** (y chat web), sin instalar nada. La persona describe su problema con texto o nota de voz y recibe, en un formato fijo:

1. **Qué está pasando**
2. **Tus derechos**, con el artículo citado textualmente
3. **Qué hacer ahora**
4. **A dónde acudir**
5. **Cuándo necesitas un abogado**

En despidos, además, una **estimación de montos calculada por código** (no por el modelo), con la fórmula paso a paso.

Proyecto para LexHack 2026. Mercado inicial: México. Corre sin costo: modelos de Groq (plan gratuito) y embeddings locales.

---

## Para los jueces: la regla de oro y cómo se hace cumplir

> Ninguna respuesta jurídica sale sin una cita de fuente recuperada. Si no hay artículo que la respalde, el asistente no contesta esa parte y canaliza a una institución.

Esta regla no depende de que el modelo "obedezca" un prompt: la hace cumplir **código determinista** en tres puntos.

| Control | Qué hace | Dónde |
|---|---|---|
| Compuerta 1 (antes de generar) | Si ningún artículo recuperado se parece lo suficiente a la consulta, no se llama al modelo de respuesta: el asistente declara que no puede confirmarlo y canaliza. | [rag/verifier.py](rag/verifier.py) `evaluar_recuperacion` |
| Compuerta 2 (después de generar) | Cada cita se coteja **carácter por carácter** contra el texto del artículo. Si el artículo no estaba entre los recuperados o la cita no es literal, ese derecho se elimina. Si no queda ningún derecho verificado, la respuesta completa se convierte en abstención. | [rag/verifier.py](rag/verifier.py) `verificar_cita`, [backend/pipeline.py](backend/pipeline.py) `verificar_respuesta` |
| Auditoría independiente | El banco de pruebas vuelve a extraer cada cita del texto final que ve el usuario y la coteja contra el corpus. Una sola cita no literal hace fallar la corrida. | [tests/bench/run_bench.py](tests/bench/run_bench.py) `auditar_salida` |

Otras garantías:

- **Montos por código, no por el modelo.** La calculadora laboral ([backend/calculadora/](backend/calculadora/)) solo hace aritmética. Cada parámetro legal vive en un YAML con su fundamento, y la cita del fundamento también se verifica literalmente contra el corpus. Mientras el equipo no marque un concepto como verificado, la cifra sale con la etiqueta `[PENDIENTE DE VERIFICACIÓN]`.
- **Instituciones de un catálogo.** El modelo nunca escribe nombres ni teléfonos: solo elige identificadores de [data/instituciones/](data/instituciones/).
- **Pasos sin cita.** Un paso de "qué hacer" sin cita que mencione plazos, cifras o artículos se descarta.
- **Corpus oficial y trazable.** Los textos vienen de fuentes oficiales, con URL y huella SHA-256 en [data/raw/fuentes_oficiales.zip](data/raw/fuentes_oficiales.zip).

## Cómo funciona

```
WhatsApp (OpenWA) ─┐
Chat web ──────────┴─> backend/ (FastAPI)
   1. Nota de voz -> texto (Groq Whisper)
   2. Triaje (Groq, gpt-oss-20b, JSON con esquema estricto): área, urgencia, datos faltantes
        - urgencia alta: bloque de emergencia primero
        - falta un dato crítico: una sola pregunta de seguimiento
        - área sin módulo cargado: abstención y canalización
   3. Recuperación híbrida por módulo (embeddings locales multilingual-e5 + BM25, fusión RRF)
   4. Compuerta 1: umbral de similitud
   5. Respuesta (Groq, gpt-oss-120b con respaldo en gpt-oss-20b) solo con los artículos recuperados
   6. Compuerta 2: cotejo literal de cada cita
   7. Calculadora laboral (código puro), si aplica
   8. Formato fijo de 5 secciones
```

**Índice por fragmentos:** los modelos de embeddings solo leen el inicio de textos largos, así que cada artículo se indexa en fragmentos de párrafos consecutivos (con el encabezado del artículo) y su similitud es la de su mejor fragmento. Las citas siempre se verifican contra el artículo completo.

**Por qué búsqueda híbrida:** la gente describe su problema en lenguaje coloquial ("me corrieron"), donde gana la búsqueda semántica, pero también usa términos exactos ("artículo 48", "corralón", "finiquito"), donde gana BM25. Si la consulta menciona un artículo por número ("art. 39-A"), ese artículo va primero.

## Corpus actual

| Módulo | Fuente | Versión | Artículos |
|---|---|---|---|
| `transito_cdmx` | Reglamento de Tránsito de la Ciudad de México ([Consejería Jurídica CDMX](https://data.consejeria.cdmx.gob.mx/index.php/leyes/reglamentos/1029-reglamentodetransitodelaciudaddemexico)) | Última reforma G.O. CDMX 30-06-2026 | 70 |
| `laboral_despido` | Ley Federal del Trabajo ([Cámara de Diputados](https://www.diputados.gob.mx/LeyesBiblio/pdf/LFT.pdf)) | Última reforma DOF 14-05-2026 | 1266 |

El texto se extrajo de los PDF oficiales y, en el caso del Reglamento, se verificó párrafo por párrafo contra el DOCX oficial (1474 de 1487 párrafos coinciden; los 13 restantes son del catálogo de señales en los anexos, que no forman parte del corpus). Las tablas (por ejemplo, las sanciones del artículo 6 del Reglamento) se reescriben una fila por línea sin cambiar palabras. Los transitorios y anexos no se incluyen.

## Levantarlo en minutos

Requisitos: Python 3.12 o superior.

```bash
git clone https://github.com/cpantaleon06-coder/Iustis.git
cd Iustis
python -m venv .venv
.venv\Scripts\activate          # en macOS o Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Copia `.env.example` como `.env` y llena al menos:

| Variable | Para qué |
|---|---|
| `GROQ_API_KEY` | Triaje, respuesta y notas de voz. Groq tiene plan gratuito: https://console.groq.com |

No se necesita ninguna otra llave: los embeddings corren localmente en el CPU. Claude (`LLM_PROVEEDOR=anthropic`) y Voyage (`proveedor: voyage` en `rag/config.yaml`) quedan como opciones configurables.

El corpus ya viene procesado en `data/processed/`. Construye el índice (la primera vez descarga el modelo de embeddings, alrededor de 1 GB) y levanta el servidor:

```bash
python -m rag.descargar_modelo      # baja el modelo de embeddings; reanuda si la conexión se corta
python -m rag.index --todos         # indexa ambos módulos (unos minutos en CPU)
uvicorn backend.main:app --port 8000
```

Abre http://localhost:8000 y ya está funcionando: el **chat web** es la forma principal de usar Iustis y no necesita nada más. Trae ejemplos clicables para empezar, acepta notas de voz y se ve bien en el teléfono. `GET /salud` muestra qué llaves faltan y si el umbral está calibrado.

Para que otras personas lo prueben desde su propio teléfono, expón el puerto con `ngrok http 8000` y comparte la URL. Los límites de `LIMITE_MENSAJES_*` protegen la cuota de Groq mientras esté abierto.

Para probar sin servidor, desde la terminal:

```bash
python -m backend.cli --traza "me despidieron ayer sin darme nada por escrito"
```

## WhatsApp con OpenWA (opcional)

El chat web ya cubre el uso completo. Este canal añade WhatsApp, que es el objetivo del
producto pero requiere un número dedicado y escanear un código QR.

[OpenWA](https://www.open-wa.org) es un gateway REST autoalojado para WhatsApp. **No es oficial de Meta**: usen un número dedicado al proyecto, no uno personal.

1. Levanta OpenWA (requiere Docker):
   ```bash
   git clone https://github.com/rmyndharis/OpenWA.git
   cd OpenWA
   docker compose -f docker-compose.dev.yml up -d
   ```
2. En el dashboard (http://localhost:2785) obtén la API key, crea una sesión y escanea el QR.
3. Llena `OPENWA_API_KEY`, `OPENWA_SESSION_ID` y un `OPENWA_WEBHOOK_SECRET` inventado en `.env`.
4. Con el servidor corriendo, registra el webhook. Si OpenWA corre en Docker en la misma máquina, puedes usar `http://host.docker.internal:8000/webhook/openwa`; si no, expón el puerto con `ngrok http 8000` y usa esa URL:
   ```bash
   python -m backend.canales.openwa --registrar-webhook https://TU-URL.ngrok-free.app/webhook/openwa
   ```

Los webhooks se validan con HMAC (`X-Webhook-Signature`); sin secreto configurado se rechazan todos. Hay un límite de mensajes por hora por usuario y por IP (`LIMITE_MENSAJES_*` en `.env`) para proteger los créditos si el chat se expone públicamente.

## Banco de pruebas y calibración

Las preguntas van en [tests/bench/preguntas.yaml](tests/bench/preguntas.yaml): pregunta, si debe responder o abstenerse, y qué artículos debe citar.

```bash
python -m tests.bench.run_bench --validar     # revisa el banco sin llamar a ningún modelo
python -m rag.calibrar --escribir             # fija el umbral de abstención con el banco
python -m tests.bench.run_bench --comparar    # corre todo y compara con la corrida anterior
```

El reporte cuenta respuestas con la cita correcta, abstenciones correctas e indebidas, respuestas que no debían darse, citas frenadas por la compuerta y **citas sin respaldo en la salida (debe ser 0)**.

Pruebas automatizadas (no llaman a ninguna API):

```bash
pytest
```

## Agregar un módulo

Cada área del derecho es un módulo de corpus independiente. Para agregar uno (por ejemplo, arrendamiento):

1. Descarga el texto oficial y extráelo:
   ```bash
   python -m ingestion.extraer --pdf ley.pdf --fuente ley_de_ejemplo --tablas
   ```
2. Copia [data/raw/_plantilla.meta.yaml](data/raw/_plantilla.meta.yaml) como `data/raw/ley_de_ejemplo.meta.yaml` y llénalo.
3. Da de alta el módulo en [data/modules.yaml](data/modules.yaml) con su `area`.
4. Ingesta e indexa:
   ```bash
   python -m ingestion.ingest --modulo mi_modulo
   python -m rag.index --modulo mi_modulo
   ```
5. Agrega sus instituciones en `data/instituciones/mi_modulo.yaml`.

La ingesta reporta artículos incompletos (no se indexan), advertencias, derogados, saltos de numeración y duplicados. Nunca completa texto que falte.

## Estructura

```
backend/        servidor, pipeline, triaje y respuesta, calculadora, canales (OpenWA, voz, web)
data/           corpus crudo (raw) y procesado, registro de módulos, catálogo de instituciones
ingestion/      extracción de PDF oficiales y troceo por artículo
prompts/        plantillas de triaje y respuesta
rag/            índice, recuperación híbrida, compuertas de verificación, calibración
tests/          pruebas automatizadas y banco de preguntas
```

## Pendiente de verificación por el equipo

Estos datos legales se dejaron en blanco a propósito y el sistema los marca como pendientes hasta que el equipo los verifique:

- Parámetros de la calculadora laboral (días, porcentajes, topes, salario mínimo, divisores): [backend/calculadora/parametros_laborales.yaml](backend/calculadora/parametros_laborales.yaml).
- Nombres y contactos de instituciones: [data/instituciones/](data/instituciones/).
- Umbral de abstención: se calibra con el banco de pruebas.

## Limitaciones conocidas

- La compuerta 2 coteja las citas de forma exacta, pero en los textos libres ("qué está pasando", "cuándo necesitas un abogado") las afirmaciones legales sin cita solo se detectan por heurística (plazos, cifras, la palabra "artículo"). El banco de pruebas es la red para esos casos.
- Las sesiones y los límites de uso viven en memoria: se reinician con el servidor.
- OpenWA solo cubre WhatsApp; no hay canal SMS.
- El plan gratuito de Groq permite 8,000 tokens por minuto por modelo: alcanza para una conversación a la vez. Ante un límite, el sistema espera lo que indica Groq o pasa la respuesta al modelo de respaldo; si ambos están saturados, responde con un aviso y canaliza.
- PyMuPDF (extracción de PDF) tiene licencia AGPL; conviene revisarlo antes de un uso comercial.

## Aviso

Esta herramienta ofrece orientación general basada en la ley citada. No sustituye la asesoría de un abogado.

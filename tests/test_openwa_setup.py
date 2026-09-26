"""Pruebas del asistente de configuración de WhatsApp (sin red ni OpenWA real)."""

import httpx

from backend.canales import openwa

CLIENTE_REAL = httpx.Client  # se guarda antes de parchear, para no llamarse a sí mismo


def cliente_falso(rutas, monkeypatch, vistos=None):
    """rutas: dict de (método, sufijo de ruta) -> Response o lista de Responses."""
    monkeypatch.setenv("OPENWA_URL", "http://wa:2785")
    monkeypatch.setenv("OPENWA_API_KEY", "k1")

    def manejador(req):
        if vistos is not None:
            vistos.append((req.method, str(req.url)))
        for (metodo, sufijo), resp in rutas.items():
            if req.method == metodo and str(req.url).endswith(sufijo):
                return resp.pop(0) if isinstance(resp, list) else resp
        return httpx.Response(404, json={})

    monkeypatch.setattr(openwa.httpx, "Client", lambda **kw: CLIENTE_REAL(transport=httpx.MockTransport(manejador)))


def test_configura_todo_cuando_la_sesion_ya_esta_conectada(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENWA_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENWA_WEBHOOK_SECRET", raising=False)
    vistos = []
    cliente_falso({
        ("POST", "/api/sessions"): httpx.Response(201, json={"id": "s-nueva"}),
        ("POST", "/start"): httpx.Response(200, json={}),
        ("GET", "/api/sessions/s-nueva"): httpx.Response(200, json={"status": "connected"}),
        ("POST", "/webhooks"): httpx.Response(201, json={"id": "wh1"}),
    }, monkeypatch, vistos)

    guardado = {}
    assert openwa.configurar("https://x.ngrok-free.app/webhook/openwa", "demo", guardado.update) == 0
    assert guardado["OPENWA_SESSION_ID"] == "s-nueva"
    assert len(guardado["OPENWA_WEBHOOK_SECRET"]) >= 32  # se generó uno seguro
    assert any("/webhooks" in u for _, u in vistos)


def test_espera_el_qr_y_se_rinde_con_codigo_propio(monkeypatch):
    monkeypatch.delenv("OPENWA_SESSION_ID", raising=False)
    cliente_falso({
        ("POST", "/api/sessions"): httpx.Response(201, json={"id": "s1"}),
        ("POST", "/start"): httpx.Response(200, json={}),
        ("GET", "/api/sessions/s1"): httpx.Response(200, json={"status": "qr"}),
    }, monkeypatch)
    guardado = {}
    # esperar_s pequeño para no alargar la prueba; dormir no duerme de verdad
    assert openwa.configurar("https://x/webhook", "demo", guardado.update, dormir=lambda s: None, esperar_s=10) == 2
    assert guardado == {}  # no escribe .env si no quedó conectada


def test_sesion_se_conecta_mientras_espera(monkeypatch):
    monkeypatch.delenv("OPENWA_SESSION_ID", raising=False)
    cliente_falso({
        ("POST", "/api/sessions"): httpx.Response(201, json={"id": "s1"}),
        ("POST", "/start"): httpx.Response(200, json={}),
        ("GET", "/api/sessions/s1"): [
            httpx.Response(200, json={"status": "qr"}),
            httpx.Response(200, json={"status": "qr"}),
            httpx.Response(200, json={"status": "ready"}),
        ],
        ("POST", "/webhooks"): httpx.Response(201, json={"id": "wh1"}),
    }, monkeypatch)
    guardado = {}
    assert openwa.configurar("https://x/webhook", "demo", guardado.update, dormir=lambda s: None, esperar_s=60) == 0
    assert guardado["OPENWA_SESSION_ID"] == "s1"


def test_sin_api_key_falla_rapido(monkeypatch):
    monkeypatch.setenv("OPENWA_API_KEY", "")
    assert openwa.configurar("https://x/webhook", "demo", lambda v: None) == 1


def test_openwa_caido_da_mensaje_util(monkeypatch):
    monkeypatch.delenv("OPENWA_SESSION_ID", raising=False)
    monkeypatch.setenv("OPENWA_URL", "http://wa:2785")
    monkeypatch.setenv("OPENWA_API_KEY", "k1")

    def cae(req):
        raise httpx.ConnectError("conexión rechazada")

    monkeypatch.setattr(openwa.httpx, "Client", lambda **kw: CLIENTE_REAL(transport=httpx.MockTransport(cae)))
    assert openwa.configurar("https://x/webhook", "demo", lambda v: None) == 1


# Escritura de .env

def test_guardar_en_env_actualiza_sin_borrar_lo_demas(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=secreto\nOPENWA_SESSION_ID=vieja\n", encoding="utf-8")
    openwa.guardar_en_env({"OPENWA_SESSION_ID": "nueva", "OPENWA_WEBHOOK_SECRET": "abc"}, env)
    lineas = env.read_text(encoding="utf-8").splitlines()
    assert "GROQ_API_KEY=secreto" in lineas  # no se pierde
    assert "OPENWA_SESSION_ID=nueva" in lineas and "OPENWA_SESSION_ID=vieja" not in lineas
    assert "OPENWA_WEBHOOK_SECRET=abc" in lineas


def test_guardar_en_env_crea_el_archivo_si_no_existe(tmp_path):
    env = tmp_path / ".env"
    openwa.guardar_en_env({"A": "1"}, env)
    assert env.read_text(encoding="utf-8") == "A=1\n"

"""Prueba del descargador de modelos: reanuda con Range si la conexión se corta."""

import httpx

from rag import descargar_modelo as dm

CONTENIDO = bytes(range(256)) * 40  # 10,240 bytes


class CorteDeRed(httpx.TransportError):
    pass


def test_reanuda_tras_un_corte(tmp_path, monkeypatch):
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)
    monkeypatch.setattr(dm, "TAMANO_BLOQUE", 1000)
    rangos = []
    cortes = {"n": 0}

    def manejador(req):
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(CONTENIDO))})
        rango = req.headers.get("Range")
        rangos.append(rango)
        inicio = int(rango.split("=")[1].rstrip("-")) if rango else 0
        if cortes["n"] == 0:
            cortes["n"] += 1

            def cuerpo_cortado():
                yield CONTENIDO[:4000]
                raise CorteDeRed("conexión cortada")

            return httpx.Response(200, content=cuerpo_cortado())
        return httpx.Response(206, content=CONTENIDO[inicio:])

    destino = tmp_path / "model.safetensors"
    with httpx.Client(transport=httpx.MockTransport(manejador)) as http:
        dm.descargar_archivo("https://ejemplo/model.safetensors", destino, http)
    assert destino.read_bytes() == CONTENIDO
    assert rangos == [None, "bytes=4000-"]  # el segundo intento pidió solo lo que faltaba
    assert not (tmp_path / "model.safetensors.parcial").exists()


def test_archivo_completo_no_se_descarga_otra_vez(tmp_path):
    destino = tmp_path / "config.json"
    destino.write_bytes(b"{}")
    pedidos = []

    def manejador(req):
        pedidos.append(req.method)
        return httpx.Response(200, headers={"content-length": "2"})

    with httpx.Client(transport=httpx.MockTransport(manejador)) as http:
        dm.descargar_archivo("https://ejemplo/config.json", destino, http)
    assert pedidos == ["HEAD"]

"""Framing, concurrencia y ownership del socket Unix de control."""

import os
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path

from parlar.config import SOCKET_PATH
from parlar.control import ServidorControl, normalizar_comando


def peticion(ruta, partes, *, cerrar_escritura=False):
    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cliente.settimeout(1)
    cliente.connect(str(ruta))
    for parte in partes:
        cliente.sendall(parte)
    if cerrar_escritura:
        cliente.shutdown(socket.SHUT_WR)
    respuesta = cliente.recv(8192).decode("utf-8").strip()
    cliente.close()
    return respuesta


class PruebasControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-control-")
        self.ruta = Path(self.tmp.name) / "control.sock"
        self.recibidos = []
        self.servidor = None

    def iniciar(self):
        def manejar(cmd):
            self.recibidos.append(cmd)
            return ("ERR comando desconocido" if cmd == "desconocido"
                    else f"OK {cmd}")

        self.servidor = ServidorControl(
            manejar, self.ruta)
        self.servidor.iniciar()

    def tearDown(self):
        if self.servidor:
            self.servidor.detener()
        self.tmp.cleanup()

    def test_startup_permisos_split_utf8_y_eof_legacy(self):
        self.iniciar()
        self.assertEqual(stat.S_IMODE(os.stat(self.ruta).st_mode), 0o600)
        payload = "modo español\n".encode()
        respuesta = peticion(self.ruta, [bytes([byte]) for byte in payload])
        self.assertEqual(respuesta, "OK modo español")
        self.assertEqual(self.recibidos, ["modo español"])
        self.assertEqual(
            peticion(self.ruta, [b"estado"], cerrar_escritura=True),
            "OK estado")

    def test_malformed_vacio_grande_multilinea_y_desconocido(self):
        self.iniciar()
        self.assertEqual(peticion(self.ruta, [b"\n"]), "ERR vacío")
        self.assertEqual(
            peticion(self.ruta, [b"x" * 4097]), "ERR demasiado largo")
        self.assertEqual(
            peticion(self.ruta, [b"\xff\n"]), "ERR UTF-8 inválido")
        self.assertEqual(
            peticion(self.ruta, [b"uno\ndos\n"]), "ERR múltiples líneas")
        self.assertEqual(
            peticion(self.ruta, [b"desconocido\n"]),
            "ERR comando desconocido")

    def test_cliente_silencioso_no_bloquea_segundo(self):
        self.iniciar()
        silencioso = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silencioso.connect(str(self.ruta))
        inicio = time.monotonic()
        respuesta = peticion(self.ruta, [b"detener\n"])
        demora = time.monotonic() - inicio
        self.assertEqual(respuesta, "OK detener")
        self.assertLess(demora, 0.2)
        silencioso.close()

    def test_segunda_instancia_stale_y_archivo_regular(self):
        self.iniciar()
        segunda = ServidorControl(lambda cmd: "OK", self.ruta)
        with self.assertRaisesRegex(RuntimeError, "instancia activa"):
            segunda.iniciar()
        self.servidor.detener()
        self.servidor = None

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.ruta))
        stale.close()
        recuperado = ServidorControl(lambda cmd: "OK", self.ruta)
        recuperado.iniciar()
        recuperado.detener()

        self.ruta.write_text("ajeno", encoding="utf-8")
        conflicto = ServidorControl(lambda cmd: "OK", self.ruta)
        with self.assertRaisesRegex(RuntimeError, "no es socket"):
            conflicto.iniciar()
        self.assertEqual(self.ruta.read_text(encoding="utf-8"), "ajeno")

    def test_shutdown_solo_elimina_endpoint_propio(self):
        self.iniciar()
        propio = self.ruta.with_suffix(".propio")
        os.rename(self.ruta, propio)
        reemplazo = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reemplazo.bind(str(self.ruta))
        self.servidor.detener()
        self.servidor = None
        self.assertTrue(self.ruta.exists())
        reemplazo.close()
        self.ruta.unlink()
        propio.unlink()

    def test_fallback_global_incluye_uid(self):
        if str(SOCKET_PATH).startswith("/tmp/"):
            self.assertIn(str(os.getuid()), SOCKET_PATH.name)

    def test_matriz_aliases_y_valores_normalizados(self):
        casos = {
            " iniciar ": ["iniciar"],
            "START": ["iniciar"],
            "detener": ["detener"],
            "stop": ["detener"],
            "cancelar": ["cancelar"],
            "CANCEL": ["cancelar"],
            "alternar": ["alternar"],
            "toggle": ["alternar"],
            "estado": ["estado"],
            "status": ["estado"],
            "modo frase": ["modo", "utterance"],
            "MODE STREAMING": ["modo", "streaming"],
            "reescritura ninguna": ["reescritura", "none"],
            "rewrite concise": ["reescritura", "concise"],
            "rewrite correo": ["reescritura", "email"],
            "salir": ["salir"],
            "QUIT": ["salir"],
        }
        for entrada, esperado in casos.items():
            with self.subTest(entrada=entrada):
                self.assertEqual(normalizar_comando(entrada), esperado)

        self.assertEqual(normalizar_comando(""), [])
        self.assertNotEqual(
            normalizar_comando("cancel"), normalizar_comando("stop"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Framing, concurrencia y ownership del socket Unix de control."""

import contextlib
import io
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import parlar.control as control_mod
from parlar.config import SOCKET_PATH
from parlar.control import (
    GuardiaInstancia,
    InstanciaActivaError,
    ServidorControl,
    normalizar_comando,
)


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
        with self.assertRaisesRegex(InstanciaActivaError, "instancia activa"):
            segunda.iniciar()
        self.assertFalse(segunda._corriendo)
        self.assertIsNone(segunda._sock)
        self.assertIsNone(segunda._lock_fd)
        self.assertEqual(peticion(self.ruta, [b"estado\n"]), "OK estado")
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

    def test_guardia_preflight_se_usa_sin_retomar_el_lock(self):
        guardia = GuardiaInstancia(self.ruta)
        guardia.adquirir()
        fd = guardia.fileno()
        servidor = ServidorControl(
            lambda cmd: f"OK {cmd}", self.ruta,
            guardia_instancia=guardia,
        )
        self.servidor = servidor

        with mock.patch.object(
                guardia, "adquirir", wraps=guardia.adquirir) as adquirir:
            servidor.iniciar()

        adquirir.assert_not_called()
        self.assertEqual(servidor._lock_fd, fd)
        self.assertEqual(peticion(self.ruta, [b"estado\n"]), "OK estado")
        servidor.detener()
        self.servidor = None
        self.assertFalse(guardia.adquirida)

        siguiente = GuardiaInstancia(self.ruta)
        siguiente.adquirir()
        siguiente.liberar()

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


class PruebasEstadoParlarctl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-estado-")
        self.ruta = Path(self.tmp.name) / "control.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def ejecutar(self, *argumentos):
        stdout = io.StringIO()
        stderr = io.StringIO()
        codigo = 0
        with (
            mock.patch.object(control_mod, "SOCKET_PATH", self.ruta),
            mock.patch.object(sys, "argv", ["parlarctl", *argumentos]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                control_mod.parlarctl_main()
            except SystemExit as exc:
                codigo = exc.code
        return codigo, stdout.getvalue(), stderr.getvalue()

    def test_sin_daemon_y_lock_stale_reporta_detenido(self):
        esperado = "el daemon de ParlAR no está corriendo\n"
        self.assertEqual(
            self.ejecutar("estado"), (1, "", esperado))

        Path(f"{self.ruta}.lock").write_text("stale", encoding="utf-8")
        self.assertEqual(
            self.ejecutar("estado"), (1, "", esperado))

    def test_lock_ocupado_sin_socket_reporta_inicio(self):
        guardia = GuardiaInstancia(self.ruta)
        guardia.adquirir()
        fd = guardia.fileno()
        try:
            esperado = "el daemon de ParlAR se está iniciando\n"
            self.assertEqual(
                self.ejecutar("estado"), (1, "", esperado))
            self.assertEqual(
                self.ejecutar("iniciar"), (1, "", esperado))
            self.assertEqual(guardia.fileno(), fd)
            with self.assertRaises(InstanciaActivaError):
                GuardiaInstancia(self.ruta).adquirir()
        finally:
            guardia.liberar()

        self.assertEqual(
            self.ejecutar("estado"),
            (1, "", "el daemon de ParlAR no está corriendo\n"),
        )

    def test_socket_disponible_devuelve_estado_ipc_real(self):
        servidor = ServidorControl(lambda cmd: f"OK {cmd}", self.ruta)
        servidor.iniciar()
        try:
            self.assertEqual(
                self.ejecutar("estado"), (0, "OK estado\n", ""))
        finally:
            servidor.detener()

    def test_socket_no_disponible_con_lock_es_transicion_de_shutdown(self):
        guardia = GuardiaInstancia(self.ruta)
        guardia.adquirir()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.ruta))
        stale.close()
        try:
            self.assertEqual(
                self.ejecutar("estado"),
                (1, "", "el daemon de ParlAR está iniciando o cerrando\n"),
            )
        finally:
            guardia.liberar()

        self.assertEqual(
            self.ejecutar("estado"),
            (1, "", "el daemon de ParlAR no está corriendo\n"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

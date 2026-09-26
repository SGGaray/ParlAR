"""Regresiones de credenciales del peer para el transporte GuionAR."""

import contextlib
import io
import json
import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parlar.cliente_guionar import ClienteGuionAR, ruta_socket_por_defecto


_UCRED = struct.Struct("=iII")


def credenciales(uid):
    return _UCRED.pack(1234, uid, 4321)


class SocketFalso:
    def __init__(self, peer=None, error=None):
        self.peer = peer
        self.error = error
        self.operaciones = []
        self.enviados = []
        self.cerrado = False

    def settimeout(self, valor):
        self.operaciones.append(("timeout", valor))

    def connect(self, ruta):
        self.operaciones.append(("connect", ruta))

    def getsockopt(self, nivel, opcion, cantidad):
        self.operaciones.append(("peercred", nivel, opcion, cantidad))
        if self.error is not None:
            raise self.error
        return self.peer

    def sendall(self, datos):
        self.operaciones.append(("send", datos))
        self.enviados.append(datos)

    def close(self):
        self.operaciones.append(("close",))
        self.cerrado = True


class PruebasCredencialesGuionAR(unittest.TestCase):
    def cliente_con_sockets(self, *sockets):
        cliente = ClienteGuionAR("/tmp/guionar-test-peercred.sock")
        parche = mock.patch(
            "parlar.cliente_guionar.socket.socket", side_effect=sockets)
        return cliente, parche

    def test_peer_mismo_uid_se_valida_antes_del_primer_byte(self):
        sock = SocketFalso(credenciales(os.getuid()))
        cliente, parche = self.cliente_con_sockets(sock)
        with parche:
            self.assertTrue(cliente.escribir_texto("texto legítimo"))

        nombres = [operacion[0] for operacion in sock.operaciones]
        self.assertLess(nombres.index("connect"), nombres.index("peercred"))
        self.assertLess(nombres.index("peercred"), nombres.index("send"))
        self.assertIs(cliente._sock, sock)

    def test_uid_diferente_rechaza_y_no_envia_snapshot_ni_texto(self):
        sock = SocketFalso(credenciales(os.getuid() + 1))
        cliente, parche = self.cliente_con_sockets(sock)
        cliente._vad_deseado = True
        cliente._parcial_deseado = "PARLAR_SEC002_SNAPSHOT"
        with parche, contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cliente.escribir_texto("PARLAR_SEC002_FINAL"))

        self.assertEqual(sock.enviados, [])
        self.assertTrue(sock.cerrado)
        self.assertIsNone(cliente._sock)

    def test_getsockopt_fallido_y_credenciales_malformadas_fail_closed(self):
        casos = (
            SocketFalso(error=OSError("peercred inyectado")),
            SocketFalso(b"mal"),
        )
        for sock in casos:
            with self.subTest(error=repr(sock.error), peer=sock.peer):
                cliente, parche = self.cliente_con_sockets(sock)
                with parche, contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(cliente.evento_vad(True))
                self.assertEqual(sock.enviados, [])
                self.assertTrue(sock.cerrado)
                self.assertIsNone(cliente._sock)

    def test_so_peercred_no_disponible_falla_cerrado(self):
        sock = SocketFalso(credenciales(os.getuid()))
        cliente, parche = self.cliente_con_sockets(sock)
        with parche, mock.patch.object(socket, "SO_PEERCRED", None), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cliente.escribir_texto("no autenticado"))
        self.assertEqual(sock.enviados, [])
        self.assertFalse(any(
            operacion[0] == "peercred" for operacion in sock.operaciones))
        self.assertTrue(sock.cerrado)
        self.assertIsNone(cliente._sock)

    def test_snapshot_valido_se_conserva_despues_de_validar(self):
        sock = SocketFalso(credenciales(os.getuid()))
        cliente, parche = self.cliente_con_sockets(sock)
        cliente._vad_deseado = True
        cliente._parcial_deseado = "hipótesis"
        with parche:
            self.assertTrue(cliente._conectar())

        mensajes = [json.loads(datos) for datos in sock.enviados]
        self.assertEqual(mensajes, [
            {"type": "vad", "data": True},
            {"type": "partial", "data": "hipótesis"},
        ])
        nombres = [operacion[0] for operacion in sock.operaciones]
        self.assertLess(nombres.index("peercred"), nombres.index("send"))

    def test_reconexion_valida_y_luego_uid_diferente_se_rechaza(self):
        primero = SocketFalso(credenciales(os.getuid()))
        segundo = SocketFalso(credenciales(os.getuid() + 1))
        cliente, parche = self.cliente_con_sockets(primero, segundo)
        with parche, contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(cliente.escribir_texto("primero"))
            cliente._desconectar()
            self.assertFalse(cliente.escribir_texto("segundo"))

        self.assertEqual(len(primero.enviados), 1)
        self.assertEqual(segundo.enviados, [])
        self.assertIsNone(cliente._sock)

    def test_reconexion_invalida_y_luego_mismo_uid_se_acepta(self):
        primero = SocketFalso(credenciales(os.getuid() + 1))
        segundo = SocketFalso(credenciales(os.getuid()))
        cliente, parche = self.cliente_con_sockets(primero, segundo)
        with parche, contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cliente.escribir_texto("primero"))
            self.assertTrue(cliente.escribir_texto("segundo"))

        self.assertEqual(primero.enviados, [])
        self.assertEqual(len(segundo.enviados), 1)
        self.assertIs(cliente._sock, segundo)

    def test_peer_invalido_no_filtra_sentinels_en_logs(self):
        snapshot = "PARLAR_SEC002_PRIVATE_SNAPSHOT"
        final = "PARLAR_SEC002_PRIVATE_FINAL"
        sock = SocketFalso(credenciales(os.getuid() + 1))
        cliente, parche = self.cliente_con_sockets(sock)
        cliente._vad_deseado = True
        cliente._parcial_deseado = snapshot
        salida, error = io.StringIO(), io.StringIO()
        with parche, contextlib.redirect_stdout(salida), \
                contextlib.redirect_stderr(error):
            self.assertFalse(cliente.escribir_texto(final))

        logs = salida.getvalue() + error.getvalue()
        self.assertNotIn(snapshot, logs)
        self.assertNotIn(final, logs)

    def test_af_unix_real_mismo_uid_y_rutas_default_se_conservan(self):
        with tempfile.TemporaryDirectory(prefix="parlar-guionar-peer-") as tmp:
            os.chmod(tmp, 0o700)
            ruta = Path(tmp) / "guionar.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(ruta))
            listener.listen(1)
            listener.settimeout(1)
            cliente = ClienteGuionAR(str(ruta))
            try:
                self.assertTrue(cliente.escribir_texto("mismo uid"))
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(1)
                    self.assertEqual(json.loads(conn.recv(8192)), {
                        "type": "text", "data": "mismo uid",
                    })
            finally:
                cliente.cerrar()
                listener.close()

            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertEqual(
                    ruta_socket_por_defecto(), str(Path(tmp) / "guionar.sock"))
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    ruta_socket_por_defecto(),
                    f"/tmp/guionar-{os.getuid()}.sock",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)

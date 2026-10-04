"""Regresiones del transporte best-effort hacia GuionAR."""

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from parlar.cliente_guionar import HELLO, ClienteGuionAR


class Receptor:
    def __init__(self, ruta):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(ruta))
        self.sock.listen(4)
        self.sock.settimeout(3)

    def recibir(self, cantidad, conn=None):
        if conn is None:
            conn, _ = self.sock.accept()
        conn.settimeout(1)
        datos = bytearray()
        while datos.count(b"\n") < cantidad:
            trozo = conn.recv(8192)
            if not trozo:
                break
            datos.extend(trozo)
        return conn, [json.loads(linea) for linea in datos.splitlines()]

    def cerrar(self):
        self.sock.close()


class PruebasGuionAR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-guionar-")
        self.ruta = Path(self.tmp.name) / "guionar.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_eventos_sin_conexion_no_se_reproducen_y_vad_deduplica(self):
        cliente = ClienteGuionAR(str(self.ruta))
        cliente.iniciar()
        self.assertFalse(cliente.evento_vad(True))
        self.assertFalse(cliente.enviar_parcial("hipótesis"))

        receptor = Receptor(self.ruta)
        conn, mensajes = receptor.recibir(1)
        self.assertEqual(mensajes, [HELLO])
        self.assertTrue(cliente.evento_vad(True))
        self.assertTrue(cliente.evento_vad(True))
        _, mensajes = receptor.recibir(1, conn)
        self.assertEqual(mensajes, [{"type": "vad", "data": True}])
        conn.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            conn.recv(1)
        conn.close()
        cliente.cerrar()
        receptor.cerrar()

    def test_reconexion_sin_snapshot_y_close_terminal(self):
        receptor = Receptor(self.ruta)
        cliente = ClienteGuionAR(str(self.ruta))
        cliente.iniciar()
        conn1, _ = receptor.recibir(1)
        self.assertTrue(cliente.evento_vad(True))
        conn1.close()
        with cliente._lock:
            cliente._desconectar()
        conn2, mensajes = receptor.recibir(1)
        self.assertEqual(mensajes, [HELLO])
        self.assertTrue(cliente.enviar_parcial("actual"))
        _, mensajes = receptor.recibir(1, conn2)
        self.assertEqual(mensajes, [{"type": "partial", "data": "actual"}])
        cliente.cerrar()
        self.assertFalse(cliente.evento_vad(False))
        self.assertFalse(cliente.enviar_parcial("posterior"))
        self.assertFalse(cliente.escribir_texto("posterior"))
        self.assertFalse(cliente.iniciar())
        conn2.close()
        receptor.cerrar()

    def test_send_vs_close_serializado_unicode_y_limite(self):
        receptor = Receptor(self.ruta)
        cliente = ClienteGuionAR(str(self.ruta))
        cliente.iniciar()
        conn, mensajes = receptor.recibir(1)
        self.assertEqual(mensajes, [HELLO])
        entrada = "á🙂" + "x" * 2100
        inicio = threading.Barrier(2)
        original = cliente._enviar_conectado

        def enviar_lento(obj):
            inicio.wait()
            return original(obj)

        cliente._enviar_conectado = enviar_lento
        resultado = []
        hilo = threading.Thread(
            target=lambda: resultado.append(cliente.escribir_texto(entrada)))
        hilo.start()
        inicio.wait()
        cliente.cerrar()
        hilo.join(1)
        _, mensajes = receptor.recibir(1, conn)
        self.assertEqual(len(mensajes[0]["data"]), 2000)
        self.assertTrue(mensajes[0]["data"].startswith("á🙂"))
        self.assertEqual(resultado, [True])
        self.assertFalse(cliente.escribir_texto("no reconecta"))
        conn.close()
        receptor.cerrar()


if __name__ == "__main__":
    unittest.main(verbosity=2)

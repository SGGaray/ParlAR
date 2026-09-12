"""Regresiones de latencia y backpressure del liveness GuionAR."""

import errno
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from parlar.capturador_audio import CapturadorMic
from parlar.cliente_guionar import ClienteGuionAR


class PeerGuionAR:
    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.ruta))
        self.listener.listen(1)
        self.listener.settimeout(1)
        self.conn = None

    def aceptar_lineas(self, cantidad=1):
        self.conn, _ = self.listener.accept()
        self.conn.settimeout(1)
        datos = bytearray()
        while datos.count(b"\n") < cantidad:
            datos.extend(self.conn.recv(8192))
        return [json.loads(linea) for linea in datos.splitlines()]

    def cerrar(self):
        if self.conn is not None:
            self.conn.close()
        self.listener.close()
        try:
            self.ruta.unlink()
        except FileNotFoundError:
            pass


class PruebasLivenessGuionAR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-guionar-live-")
        self.ruta = Path(self.tmp.name) / "guionar.sock"
        self.cliente = None
        self.peers = []

    def tearDown(self):
        if self.cliente is not None:
            self.cliente.cerrar()
        for peer in self.peers:
            try:
                peer.cerrar()
            except OSError:
                pass
        self.tmp.cleanup()

    def peer(self):
        peer = PeerGuionAR(self.ruta)
        self.peers.append(peer)
        return peer

    def conectar_parcial(self, texto="hipotesis estable"):
        peer = self.peer()
        self.cliente = ClienteGuionAR(str(self.ruta))
        self.assertTrue(self.cliente.enviar_parcial(texto))
        self.assertEqual(peer.aceptar_lineas(), [
            {"type": "partial", "data": texto},
        ])
        return peer

    def test_100_estados_deduplicados_no_pagan_timeout(self):
        peer = self.conectar_parcial()
        inicio = time.perf_counter()
        for _ in range(100):
            self.assertTrue(
                self.cliente.enviar_parcial("hipotesis estable"))
        demora = time.perf_counter() - inicio

        # Holgado frente a la regresión observada: 100 * 50 ms ~= 5 s.
        self.assertLess(demora, 0.5)
        peer.conn.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            peer.conn.recv(1)
        peer.listener.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            peer.listener.accept()

    def test_probe_distingue_sin_datos_de_reset_pipe_y_eof(self):
        cliente = ClienteGuionAR(str(self.ruta))
        sock = mock.Mock()
        cliente._sock = sock

        with mock.patch("parlar.cliente_guionar.select.select",
                        return_value=([], [], [])):
            self.assertTrue(cliente._conexion_viva())
        sock.recv.assert_not_called()

        with mock.patch("parlar.cliente_guionar.select.select",
                        return_value=([sock], [], [])):
            for codigo in (errno.EAGAIN, errno.EWOULDBLOCK):
                with self.subTest(codigo=codigo):
                    sock.recv.side_effect = BlockingIOError(codigo, "sin datos")
                    self.assertTrue(cliente._conexion_viva())
            for error in (
                    ConnectionResetError(errno.ECONNRESET, "reset"),
                    BrokenPipeError(errno.EPIPE, "pipe")):
                with self.subTest(error=type(error).__name__):
                    sock.recv.side_effect = error
                    self.assertFalse(cliente._conexion_viva())
            sock.recv.side_effect = None
            sock.recv.return_value = b""
            self.assertFalse(cliente._conexion_viva())
            sock.recv.return_value = b"dato inesperado"
            self.assertTrue(cliente._conexion_viva())

        cliente.cerrar()

    def test_pipeline_1000_frames_sin_drops_por_liveness(self):
        self.conectar_parcial()
        mic = CapturadorMic(16000, 320, max_cola=500)
        with mic._callback_lock:
            mic._preparar_generacion(1)
        terminado = threading.Event()
        errores = []
        llamadas_stt = 0

        def worker():
            nonlocal llamadas_stt
            try:
                while (not terminado.is_set()
                       or mic.estado_captura().queue_depth):
                    item = mic.leer_frame(0.05)
                    if item is None:
                        continue
                    llamadas_stt += 1  # STT falso e instantáneo.
                    if not self.cliente.enviar_parcial(
                            "hipotesis estable"):
                        raise AssertionError("falló GuionAR sano")
            except BaseException as exc:
                errores.append(exc)

        hilo = threading.Thread(target=worker)
        inicio = time.perf_counter()
        hilo.start()
        frame = bytes(640)
        # 20 s lógicos de audio, comprimidos 4x para exigir 200 iter/s.
        for indice in range(1000):
            restante = inicio + indice * 0.005 - time.perf_counter()
            if restante > 0:
                time.sleep(restante)
            mic._callback(1, frame, 320, None, None)
        terminado.set()
        hilo.join(5)
        demora = time.perf_counter() - inicio
        self.assertFalse(hilo.is_alive())
        self.assertEqual(errores, [])

        estado = mic.estado_captura()
        self.assertEqual(estado.frames_capturados, 1000)
        self.assertEqual(estado.frames_entregados, 1000)
        self.assertEqual(estado.frames_descartados, 0)
        self.assertEqual(estado.device_overflows, 0)
        self.assertEqual(estado.queue_depth, 0)
        self.assertLess(estado.max_queue_depth, 500)
        self.assertEqual(estado.discontinuidades, 0)
        self.assertEqual(llamadas_stt, 1000)
        self.assertGreater(llamadas_stt / demora, 50)

    def test_peer_lento_conserva_timeout_y_puede_recuperar(self):
        peer = self.conectar_parcial("inicio")
        with self.cliente._lock:
            self.assertEqual(self.cliente._sock.gettimeout(), 0.05)
            self.cliente._sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)

        fallo = None
        for indice in range(1000):
            inicio = time.perf_counter()
            ok = self.cliente.enviar_parcial(
                f"{indice:04d}:" + "x" * 1995)
            demora = time.perf_counter() - inicio
            if not ok:
                fallo = demora
                break
        self.assertIsNotNone(fallo, "el send buffer reducido nunca se llenó")
        self.assertLess(fallo, 0.3)

        peer.cerrar()
        self.peers.remove(peer)
        nuevo = self.peer()
        self.assertTrue(self.cliente.enviar_parcial("recuperado"))
        self.assertEqual(nuevo.aceptar_lineas(), [
            {"type": "partial", "data": "recuperado"},
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)

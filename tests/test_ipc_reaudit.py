"""Regresiones de restart, ownership y ciclo de clientes IPC."""

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from parlar.cliente_guionar import ClienteGuionAR
from parlar.control import ServidorControl


class ListenerGuionAR:
    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.ruta))
        self.sock.listen(4)
        self.sock.settimeout(1)

    def aceptar(self):
        conn, _ = self.sock.accept()
        conn.settimeout(1)
        return conn

    @staticmethod
    def recibir(conn, cantidad):
        datos = bytearray()
        limite = time.monotonic() + 1
        while datos.count(b"\n") < cantidad:
            if time.monotonic() >= limite:
                raise AssertionError("snapshot GuionAR incompleto")
            trozo = conn.recv(8192)
            if not trozo:
                raise AssertionError("GuionAR cerró antes del snapshot")
            datos.extend(trozo)
        lineas = datos.splitlines()
        return [json.loads(linea) for linea in lineas]

    def cerrar(self, conn=None):
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        self.sock.close()
        try:
            self.ruta.unlink()
        except FileNotFoundError:
            pass


class PruebasRestartGuionAR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-guionar-reaudit-")
        self.ruta = Path(self.tmp.name) / "guionar.sock"
        self.clientes = []
        self.listeners = []

    def tearDown(self):
        for cliente in self.clientes:
            cliente.cerrar()
        for listener, conn in self.listeners:
            try:
                listener.cerrar(conn)
            except OSError:
                pass
        self.tmp.cleanup()

    def cliente(self):
        cliente = ClienteGuionAR(str(self.ruta))
        self.clientes.append(cliente)
        return cliente

    def listener(self):
        listener = ListenerGuionAR(self.ruta)
        entrada = [listener, None]
        self.listeners.append(entrada)
        return entrada

    def reiniciar_peer(self, anterior):
        listener, conn = anterior
        listener.cerrar(conn)
        self.listeners.remove(anterior)
        return self.listener()

    def test_vad_igual_detecta_restart_real(self):
        a = self.listener()
        cliente = self.cliente()
        self.assertTrue(cliente.evento_vad(True))
        a[1] = a[0].aceptar()
        self.assertEqual(a[0].recibir(a[1], 1), [
            {"type": "vad", "data": True}])

        b = self.reiniciar_peer(a)
        self.assertTrue(cliente.evento_vad(True))
        b[1] = b[0].aceptar()
        self.assertEqual(b[0].recibir(b[1], 1), [
            {"type": "vad", "data": True}])

    def test_partial_unicode_igual_detecta_restart_real(self):
        a = self.listener()
        cliente = self.cliente()
        self.assertTrue(cliente.enviar_parcial("hipótesis á🙂"))
        a[1] = a[0].aceptar()
        a[0].recibir(a[1], 1)

        b = self.reiniciar_peer(a)
        self.assertTrue(cliente.enviar_parcial("hipótesis á🙂"))
        b[1] = b[0].aceptar()
        self.assertEqual(b[0].recibir(b[1], 1), [
            {"type": "partial", "data": "hipótesis á🙂"}])

    def test_vad_y_partial_iguales_reponen_snapshot_sin_final(self):
        a = self.listener()
        cliente = self.cliente()
        self.assertTrue(cliente.evento_vad(True))
        self.assertTrue(cliente.enviar_parcial("actual"))
        a[1] = a[0].aceptar()
        a[0].recibir(a[1], 2)

        b = self.reiniciar_peer(a)
        self.assertTrue(cliente.evento_vad(True))
        b[1] = b[0].aceptar()
        mensajes = b[0].recibir(b[1], 2)
        self.assertEqual(mensajes, [
            {"type": "vad", "data": True},
            {"type": "partial", "data": "actual"},
        ])
        self.assertTrue(cliente.enviar_parcial("actual"))
        b[1].settimeout(0.05)
        with self.assertRaises(socket.timeout):
            b[1].recv(1)
        self.assertFalse(any(m["type"] == "text" for m in mensajes))

    def test_estado_distinto_tras_restart_actualiza_snapshot(self):
        a = self.listener()
        cliente = self.cliente()
        cliente.evento_vad(True)
        cliente.enviar_parcial("viejo")
        a[1] = a[0].aceptar()
        a[0].recibir(a[1], 2)

        b = self.reiniciar_peer(a)
        self.assertTrue(cliente.evento_vad(False))
        b[1] = b[0].aceptar()
        self.assertEqual(b[0].recibir(b[1], 2), [
            {"type": "vad", "data": False},
            {"type": "partial", "data": "viejo"},
        ])
        self.assertTrue(cliente.enviar_parcial("nuevo"))
        self.assertEqual(b[0].recibir(b[1], 1), [
            {"type": "partial", "data": "nuevo"}])

    def test_peer_lento_eagain_no_duplica_ni_reconecta(self):
        listener = self.listener()
        cliente = self.cliente()
        self.assertTrue(cliente.evento_vad(True))
        listener[1] = listener[0].aceptar()
        listener[0].recibir(listener[1], 1)

        # El peer no responde: MSG_PEEK no bloqueante obtiene EAGAIN y la
        # conexión viva conserva dedup sin polling ni conexión adicional.
        for _ in range(20):
            self.assertTrue(cliente.evento_vad(True))
        listener[1].settimeout(0.05)
        with self.assertRaises(socket.timeout):
            listener[1].recv(1)
        listener[0].sock.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            listener[0].sock.accept()

    def test_ausencia_prolongada_y_close_terminal(self):
        a = self.listener()
        cliente = self.cliente()
        cliente.enviar_parcial("á🙂")
        a[1] = a[0].aceptar()
        a[0].recibir(a[1], 1)
        listener, conn = a
        listener.cerrar(conn)
        self.listeners.remove(a)

        self.assertFalse(cliente.enviar_parcial("á🙂"))
        self.assertFalse(cliente.enviar_parcial("á🙂"))
        b = self.listener()
        self.assertTrue(cliente.enviar_parcial("á🙂"))
        b[1] = b[0].aceptar()
        self.assertEqual(b[0].recibir(b[1], 1), [
            {"type": "partial", "data": "á🙂"}])

        cliente.cerrar()
        self.assertFalse(cliente.evento_vad(False))
        self.assertFalse(cliente.enviar_parcial("posterior"))
        self.assertFalse(cliente.escribir_texto("posterior"))

    def test_final_historico_no_se_reproduce_al_reconectar(self):
        a = self.listener()
        cliente = self.cliente()
        cliente.evento_vad(True)
        cliente.escribir_texto("final histórico")
        a[1] = a[0].aceptar()
        iniciales = a[0].recibir(a[1], 2)
        self.assertEqual([m["type"] for m in iniciales], ["vad", "text"])

        b = self.reiniciar_peer(a)
        self.assertTrue(cliente.evento_vad(True))
        b[1] = b[0].aceptar()
        snapshot = b[0].recibir(b[1], 2)
        self.assertEqual(snapshot, [
            {"type": "vad", "data": True},
            {"type": "partial", "data": ""},
        ])
        self.assertFalse(any(m["type"] == "text" for m in snapshot))


class ServidorPausado(ServidorControl):
    def __init__(self, *args, enlazado, continuar, **kwargs):
        super().__init__(*args, **kwargs)
        self.enlazado = enlazado
        self.continuar = continuar

    def _activar_listener(self):
        self.enlazado.set()
        if not self.continuar.wait(2):
            raise TimeoutError("barrera bind/listen no liberada")
        super()._activar_listener()


class ServidorObservable(ServidorControl):
    def __init__(self, *args, esperados=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.esperados = esperados
        self.lectores = 0
        self.finalizados = 0
        self.lock_observacion = threading.Lock()
        self.todos_leyendo = threading.Event()
        self.todos_finalizados = threading.Event()
        self.primera_aceptacion = None
        self.ultima_finalizacion = None

    def _leer_comando(self, conn, aceptado_en=None):
        with self.lock_observacion:
            self.lectores += 1
            if self.primera_aceptacion is None:
                self.primera_aceptacion = aceptado_en
            if self.lectores == self.esperados:
                self.todos_leyendo.set()
        return super()._leer_comando(conn, aceptado_en)

    def _atender_cliente(self, conn, aceptado_en):
        try:
            super()._atender_cliente(conn, aceptado_en)
        finally:
            with self.lock_observacion:
                self.finalizados += 1
                self.ultima_finalizacion = time.monotonic()
                if self.finalizados == self.esperados:
                    self.todos_finalizados.set()


def peticion(ruta, comando):
    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cliente.settimeout(2)
    try:
        cliente.connect(str(ruta))
        cliente.sendall((comando + "\n").encode("utf-8"))
        return cliente.recv(8192).decode("utf-8").strip()
    finally:
        cliente.close()


class PruebasControlReaudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-control-reaudit-")
        self.ruta = Path(self.tmp.name) / "control.sock"
        self.servidores = []

    def tearDown(self):
        for servidor in self.servidores:
            servidor.detener()
        self.tmp.cleanup()

    def registrar(self, servidor):
        self.servidores.append(servidor)
        return servidor

    def test_race_bind_listen_exactamente_un_owner_10x(self):
        for vuelta in range(10):
            with self.subTest(vuelta=vuelta):
                enlazado = threading.Event()
                continuar = threading.Event()
                a = self.registrar(ServidorPausado(
                    lambda cmd: f"A {cmd}", self.ruta,
                    enlazado=enlazado, continuar=continuar))
                errores = []
                hilo_a = threading.Thread(
                    target=lambda: self._iniciar_capturando(a, errores))
                hilo_a.start()
                self.assertTrue(enlazado.wait(1))

                b = ServidorControl(lambda cmd: f"B {cmd}", self.ruta)
                with self.assertRaisesRegex(RuntimeError, "instancia activa"):
                    b.iniciar()
                continuar.set()
                hilo_a.join(2)
                self.assertFalse(hilo_a.is_alive())
                self.assertEqual(errores, [])
                self.assertEqual(peticion(self.ruta, "estado"), "A estado")
                a.detener()
                self.servidores.remove(a)

    @staticmethod
    def _iniciar_capturando(servidor, errores):
        try:
            servidor.iniciar()
        except BaseException as exc:
            errores.append(exc)

    def test_stale_lock_socket_permisos_y_restart(self):
        lock = Path(f"{self.ruta}.lock")
        lock.write_text("contenido no confiable", encoding="utf-8")
        os.chmod(lock, 0o666)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.ruta))
        stale.close()

        primero = self.registrar(ServidorControl(lambda cmd: "OK", self.ruta))
        primero.iniciar()
        self.assertEqual(stat.S_IMODE(os.stat(self.ruta).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)
        primero.detener()
        self.servidores.remove(primero)
        self.assertFalse(self.ruta.exists())
        self.assertFalse(lock.exists())

        segundo = self.registrar(ServidorControl(lambda cmd: "OK2", self.ruta))
        segundo.iniciar()
        self.assertEqual(peticion(self.ruta, "estado"), "OK2")

    def test_archivo_regular_y_reemplazos_ajenos_se_preservan(self):
        self.ruta.write_text("ajeno", encoding="utf-8")
        conflicto = ServidorControl(lambda cmd: "OK", self.ruta)
        with self.assertRaisesRegex(RuntimeError, "no es socket"):
            conflicto.iniciar()
        self.assertEqual(self.ruta.read_text(encoding="utf-8"), "ajeno")
        self.ruta.unlink()

        servidor = self.registrar(ServidorControl(lambda cmd: "OK", self.ruta))
        servidor.iniciar()
        socket_propio = self.ruta.with_suffix(".propio")
        lock = Path(f"{self.ruta}.lock")
        lock_propio = Path(f"{lock}.propio")
        os.rename(self.ruta, socket_propio)
        os.rename(lock, lock_propio)
        reemplazo_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reemplazo_socket.bind(str(self.ruta))
        lock.write_text("reemplazo", encoding="utf-8")

        servidor.detener()
        self.servidores.remove(servidor)
        self.assertTrue(self.ruta.exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), "reemplazo")
        reemplazo_socket.close()
        self.ruta.unlink()
        socket_propio.unlink()
        lock.unlink()
        lock_propio.unlink()

    def test_slowloris_libera_slots_y_cliente_legitimo_5x(self):
        duraciones = []
        for vuelta in range(5):
            with self.subTest(vuelta=vuelta):
                servidor = self.registrar(ServidorObservable(
                    lambda cmd: f"OK {cmd}", self.ruta, esperados=8))
                servidor.iniciar()
                lentos = []
                for _ in range(8):
                    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    cliente.connect(str(self.ruta))
                    lentos.append(cliente)
                self.assertTrue(servidor.todos_leyendo.wait(1))

                terminar = threading.Event()
                emisores = [threading.Thread(
                    target=self._goteo, args=(cliente, terminar))
                    for cliente in lentos]
                for hilo in emisores:
                    hilo.start()
                self.assertTrue(servidor.todos_finalizados.wait(1.5))
                terminar.set()
                for hilo in emisores:
                    hilo.join(1)
                for cliente in lentos:
                    cliente.close()

                duracion = (servidor.ultima_finalizacion
                            - servidor.primera_aceptacion)
                duraciones.append(duracion)
                self.assertGreater(duracion, 0.75)
                self.assertLess(duracion, 1.35)
                self.assertEqual(peticion(self.ruta, "estado"), "OK estado")
                servidor.detener()
                self.servidores.remove(servidor)
        self.assertEqual(len(duraciones), 5)

    @staticmethod
    def _goteo(cliente, terminar):
        while not terminar.is_set():
            try:
                cliente.sendall(b"x")
            except OSError:
                return
            terminar.wait(0.12)

    def test_stop_cierra_parciales_sin_dispatch_5x(self):
        for vuelta in range(5):
            with self.subTest(vuelta=vuelta):
                ejecutados = []
                servidor = self.registrar(ServidorObservable(
                    lambda cmd: ejecutados.append(cmd) or "OK",
                    self.ruta, esperados=1))
                servidor.iniciar()
                cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                cliente.connect(str(self.ruta))
                cliente.sendall(b"esta")
                self.assertTrue(servidor.todos_leyendo.wait(1))
                inicio = time.monotonic()
                servidor.detener()
                demora = time.monotonic() - inicio
                self.servidores.remove(servidor)
                self.assertLess(demora, 1)
                self.assertEqual(ejecutados, [])
                self.assertEqual(servidor._hilos_clientes, set())
                self.assertEqual(servidor._conexiones_activas, set())
                cliente.close()

    def test_comando_admitido_puede_terminar_y_stop_lo_espera(self):
        entro = threading.Event()
        liberar = threading.Event()
        ejecutados = []

        def manejar(cmd):
            ejecutados.append(cmd)
            entro.set()
            self.assertTrue(liberar.wait(2))
            return "OK"

        servidor = self.registrar(ServidorControl(manejar, self.ruta))
        servidor.iniciar()
        cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cliente.connect(str(self.ruta))
        cliente.sendall(b"estado\n")
        self.assertTrue(entro.wait(1))
        hilo_stop = threading.Thread(target=servidor.detener)
        hilo_stop.start()
        hilo_stop.join(0.05)
        self.assertTrue(hilo_stop.is_alive())
        liberar.set()
        hilo_stop.join(1)
        self.assertFalse(hilo_stop.is_alive())
        self.servidores.remove(servidor)
        self.assertEqual(ejecutados, ["estado"])
        cliente.close()

    def test_stop_concurrente_con_handler_que_tambien_detiene_no_deadlock(self):
        entro = threading.Event()
        permitir_stop_interno = threading.Event()
        fase_externa = threading.Event()
        servidor = None

        def manejar(cmd):
            entro.set()
            self.assertTrue(permitir_stop_interno.wait(2))
            servidor.detener()
            return "OK"

        servidor = self.registrar(ServidorControl(manejar, self.ruta))
        cerrar_original = servidor._cerrar_listener

        def cerrar_observado():
            cerrar_original()
            fase_externa.set()

        servidor._cerrar_listener = cerrar_observado
        servidor.iniciar()
        cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cliente.connect(str(self.ruta))
        cliente.sendall(b"salir\n")
        self.assertTrue(entro.wait(1))

        stop_externo = threading.Thread(target=servidor.detener)
        stop_externo.start()
        self.assertTrue(fase_externa.wait(1))
        permitir_stop_interno.set()
        stop_externo.join(2)
        self.assertFalse(stop_externo.is_alive())
        self.servidores.remove(servidor)
        cliente.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

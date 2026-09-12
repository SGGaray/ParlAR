"""Ownership persistente, shutdown acotado y cleanup best-effort real."""

import fcntl
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from parlar.app import EstadoApp
from parlar.control import ServidorControl
from tests.test_lifecycle import FrasesFalsas, crear_app


def peticion(ruta, comando):
    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cliente.settimeout(2)
    try:
        cliente.connect(str(ruta))
        cliente.sendall((comando + "\n").encode("utf-8"))
        return cliente.recv(4096).decode("utf-8").strip()
    finally:
        cliente.close()


def iniciar_capturando(servidor, errores):
    try:
        servidor.iniciar()
    except BaseException as exc:
        errores.append(exc)


def flock_disponible(ruta):
    fd = os.open(ruta, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


class InicioFallido(ServidorControl):
    def _activar_listener(self):
        self.fd_durante_fallo = self._lock_fd
        raise OSError("listen inyectado")


class PruebasOwnershipPersistente(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-ownership-")
        self.ruta = Path(self.tmp.name) / "control.sock"
        self.lock = Path(f"{self.ruta}.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tres_actores_compiten_sobre_un_inode_20x(self):
        ganadores = []
        for vuelta in range(20):
            with self.subTest(vuelta=vuelta):
                a = ServidorControl(lambda _cmd: "A", self.ruta)
                a.iniciar()
                inode_reserva = os.stat(self.lock).st_ino

                b = ServidorControl(lambda _cmd: "B", self.ruta)
                c = ServidorControl(lambda _cmd: "C", self.ruta)
                errores_b = []
                errores_c = []
                b_abierto = threading.Event()
                liberar_b = threading.Event()
                inodes = {}
                flock_real = fcntl.flock

                def flock_observado(fd, operacion):
                    nombre = threading.current_thread().name
                    if operacion & fcntl.LOCK_NB and nombre in (
                            "actor-b", "actor-c"):
                        inodes[nombre] = os.fstat(fd).st_ino
                    if nombre == "actor-b" and operacion & fcntl.LOCK_NB:
                        b_abierto.set()
                        if not liberar_b.wait(2):
                            raise TimeoutError("B no liberado")
                    return flock_real(fd, operacion)

                with mock.patch(
                        "parlar.control.fcntl.flock",
                        side_effect=flock_observado):
                    hilo_b = threading.Thread(
                        target=iniciar_capturando, args=(b, errores_b),
                        name="actor-b")
                    hilo_b.start()
                    self.assertTrue(b_abierto.wait(1))
                    a.detener()

                    if vuelta % 2:
                        liberar_b.set()
                        hilo_b.join(2)
                        hilo_c = threading.Thread(
                            target=iniciar_capturando, args=(c, errores_c),
                            name="actor-c")
                        hilo_c.start()
                    else:
                        hilo_c = threading.Thread(
                            target=iniciar_capturando, args=(c, errores_c),
                            name="actor-c")
                        hilo_c.start()
                        hilo_c.join(2)
                        liberar_b.set()

                    hilo_b.join(2)
                    hilo_c.join(2)

                self.assertFalse(hilo_b.is_alive())
                self.assertFalse(hilo_c.is_alive())
                self.assertEqual(
                    {inode_reserva, inodes["actor-b"], inodes["actor-c"]},
                    {inode_reserva})
                corriendo = [servidor for servidor in (b, c)
                             if servidor._corriendo]
                self.assertEqual(len(corriendo), 1)
                ganador = "B" if corriendo[0] is b else "C"
                ganadores.append(ganador)
                self.assertEqual(peticion(self.ruta, "estado"), ganador)
                errores = errores_b + errores_c
                self.assertEqual(len(errores), 1)
                self.assertIsInstance(errores[0], RuntimeError)
                self.assertIn("instancia activa", str(errores[0]))
                b.detener()
                c.detener()
                self.assertTrue(self.lock.exists())
                self.assertEqual(os.stat(self.lock).st_ino, inode_reserva)
                self.assertTrue(flock_disponible(self.lock))

        self.assertEqual(ganadores.count("B"), 10)
        self.assertEqual(ganadores.count("C"), 10)

    def test_stale_restart_lock_persistente_y_permisos(self):
        self.lock.write_text("contenido no confiable", encoding="utf-8")
        os.chmod(self.lock, 0o666)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.ruta))
        stale.close()

        umask_anterior = os.umask(0)
        try:
            primero = ServidorControl(lambda _cmd: "primero", self.ruta)
            primero.iniciar()
        finally:
            os.umask(umask_anterior)
        inode = os.stat(self.lock).st_ino
        self.assertEqual(stat.S_IMODE(os.stat(self.lock).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.ruta).st_mode), 0o600)
        primero.detener()

        self.assertFalse(self.ruta.exists())
        self.assertTrue(self.lock.exists())
        self.assertEqual(os.stat(self.lock).st_ino, inode)
        self.assertTrue(flock_disponible(self.lock))

        segundo = ServidorControl(lambda _cmd: "segundo", self.ruta)
        segundo.iniciar()
        self.assertEqual(os.stat(self.lock).st_ino, inode)
        self.assertEqual(peticion(self.ruta, "estado"), "segundo")
        segundo.detener()

    def test_lock_wrong_type_no_se_reemplaza(self):
        self.lock.mkdir()
        servidor = ServidorControl(lambda _cmd: "OK", self.ruta)
        with self.assertRaisesRegex(RuntimeError, "abrir el lock"):
            servidor.iniciar()
        self.assertTrue(self.lock.is_dir())


class PruebasCleanupControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-cleanup-")
        self.ruta = Path(self.tmp.name) / "control.sock"
        self.lock = Path(f"{self.ruta}.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def _unlink_fallido(self):
        unlink_real = os.unlink

        def unlink(path):
            if Path(path) == self.ruta:
                raise PermissionError("unlink inyectado")
            return unlink_real(path)

        return unlink

    def _assert_fd_liberado(self, servidor, fd):
        self.assertIsNone(servidor._lock_fd)
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertTrue(flock_disponible(self.lock))

    def test_unlink_fallido_en_stop_libera_fd_y_flock(self):
        servidor = ServidorControl(lambda _cmd: "OK", self.ruta)
        servidor.iniciar()
        fd = servidor._lock_fd
        with mock.patch(
                "parlar.control.os.unlink",
                side_effect=self._unlink_fallido()):
            with self.assertRaises(PermissionError):
                servidor.detener()
        self._assert_fd_liberado(servidor, fd)
        self.assertFalse(servidor._corriendo)
        self.assertIsNone(servidor._identidad)

    def test_lstat_fallido_en_stop_libera_fd_y_flock(self):
        servidor = ServidorControl(lambda _cmd: "OK", self.ruta)
        servidor.iniciar()
        fd = servidor._lock_fd
        lstat_real = os.lstat

        def lstat(path):
            if Path(path) == self.ruta:
                raise PermissionError("lstat inyectado")
            return lstat_real(path)

        with mock.patch("parlar.control.os.lstat", side_effect=lstat):
            with self.assertRaises(PermissionError):
                servidor.detener()
        self._assert_fd_liberado(servidor, fd)

    def test_doble_fallo_preserva_primero_y_agrega_liberacion(self):
        servidor = ServidorControl(lambda _cmd: "OK", self.ruta)
        servidor.iniciar()
        fd = servidor._lock_fd
        flock_real = fcntl.flock

        def flock(fd_lock, operacion):
            if operacion == fcntl.LOCK_UN:
                raise OSError("unlock inyectado")
            return flock_real(fd_lock, operacion)

        with mock.patch(
                "parlar.control.os.unlink",
                side_effect=self._unlink_fallido()), mock.patch(
                    "parlar.control.fcntl.flock", side_effect=flock):
            with self.assertRaises(PermissionError) as error:
                servidor.detener()
        notas = getattr(error.exception, "__notes__", [])
        self.assertTrue(any("liberación del lock" in nota for nota in notas))
        self._assert_fd_liberado(servidor, fd)

    def test_startup_rollback_preserva_error_y_libera_flock(self):
        servidor = InicioFallido(lambda _cmd: "OK", self.ruta)
        with mock.patch(
                "parlar.control.os.unlink",
                side_effect=self._unlink_fallido()):
            with self.assertRaisesRegex(OSError, "listen inyectado") as error:
                servidor.iniciar()
        notas = getattr(error.exception, "__notes__", [])
        self.assertTrue(any("PermissionError" in nota for nota in notas))
        self._assert_fd_liberado(servidor, servidor.fd_durante_fallo)
        self.assertIsNone(servidor._identidad)


class PruebasShutdownControl(unittest.TestCase):
    def _app_bloqueada(self, ruta):
        frases = FrasesFalsas(bloquear=True)
        app, mic, *_ = crear_app(frases=frases)
        app.control = ServidorControl(app._atender_comando, ruta)
        app._iniciar_trabajador()
        app.control.iniciar()
        self.assertTrue(app.iniciar_grabacion())
        mic.enviar(8)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(1))
        return app, frases

    @staticmethod
    def _shutdown_workers():
        return [hilo for hilo in threading.enumerate()
                if hilo.name == "shutdown-finalizer" and hilo.is_alive()]

    def _esperar_cierre(self, app, frases):
        frases.liberar.set()
        self.assertTrue(app._shutdown_completo.wait(2))
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        limite = time.monotonic() + 1
        while self._shutdown_workers() and time.monotonic() < limite:
            time.sleep(0.005)
        self.assertEqual(self._shutdown_workers(), [])
        self.assertFalse(app._trabajador_hilo.is_alive())
        self.assertIsNone(app.control._hilo)
        self.assertFalse(app.control._corriendo)
        self.assertEqual(app.control._hilos_clientes, set())
        self.assertTrue(Path(f"{app.control.ruta}.lock").exists())

    def test_bursts_reales_comparten_un_shutdown_worker(self):
        for cantidad in (1, 5, 25, 100):
            with self.subTest(cantidad=cantidad), tempfile.TemporaryDirectory(
                    prefix="parlar-shutdown-") as tmp:
                ruta = Path(tmp) / "control.sock"
                app, frases = self._app_bloqueada(ruta)
                respuestas = [peticion(ruta, "salir")
                              for _ in range(cantidad)]
                self.assertEqual(respuestas, ["OK chau"] * cantidad)
                self.assertEqual(app.estado, EstadoApp.SHUTTING_DOWN)
                self.assertLessEqual(len(self._shutdown_workers()), 1)
                with app.control._hilos_lock:
                    self.assertLessEqual(len(app.control._hilos_clientes), 8)
                self._esperar_cierre(app, frases)
                cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    with self.assertRaises(OSError):
                        cliente.connect(str(ruta))
                finally:
                    cliente.close()

    def test_requests_durante_shutdown_conservan_estado_terminal(self):
        with tempfile.TemporaryDirectory(prefix="parlar-terminal-") as tmp:
            ruta = Path(tmp) / "control.sock"
            app, frases = self._app_bloqueada(ruta)
            self.assertEqual(peticion(ruta, "salir"), "OK chau")
            self.assertTrue(peticion(ruta, "estado").startswith("cerrando "))
            self.assertEqual(peticion(ruta, "salir"), "OK chau")
            self.assertEqual(
                peticion(ruta, "iniciar"),
                "ERR la aplicación se está cerrando")
            self.assertEqual(peticion(ruta, "detener"), "ERR shutting_down")
            self.assertLessEqual(len(self._shutdown_workers()), 1)
            self._esperar_cierre(app, frases)


if __name__ == "__main__":
    unittest.main(verbosity=2)

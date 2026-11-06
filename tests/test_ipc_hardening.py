"""H-08: autenticación de peer, directorio privado y rutas del socket de control.

Todas las pruebas usan directorios temporales propios; ninguna toca el runtime
real del usuario, /tmp compartido, el micrófono ni modelos.
"""

import contextlib
import io
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import parlar.control as control_mod
import parlar.ipc_seguro as ipc
from parlar import __main__ as entrada
from parlar import tray_gtk
from parlar.config import Config
from parlar.control import (
    GuardiaInstancia,
    InstanciaActivaError,
    ServidorControl,
    enviar_comando,
    lock_instancia_ocupado,
)
from parlar.ipc_seguro import (
    MAX_BYTES_RUTA_SOCKET,
    ErrorDirectorioInseguro,
    PeerNoAutorizadoError,
    asegurar_directorio_privado,
    resolver_ruta_socket,
    ruta_socket_legada,
    uid_peer,
    verificar_peer,
)
from parlar.launcher import GuardiaVentanaSettings
from parlar.restart import (
    CoordinadorReinicio,
    EstadoResultadoReinicio,
    GuardiaCoordinadorReinicio,
)

UID = os.getuid()
OTRO_UID = UID + 4242


def ucred(uid, pid=1234, gid=0):
    return struct.pack("=iII", pid, uid, gid)


class SocketFalso:
    def __init__(self, datos=None, error=None):
        self.datos = datos
        self.error = error

    def getsockopt(self, *_argumentos):
        if self.error is not None:
            raise self.error
        return self.datos


class ListenerCrudo:
    """Endpoint que acepta una conexión y registra lo recibido (impostor)."""

    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self.recibido = bytearray()
        self.aceptado = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.ruta))
        self.sock.listen(4)
        self.sock.settimeout(3)
        self.hilo = threading.Thread(target=self._bucle, daemon=True)
        self.hilo.start()

    def _bucle(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.aceptado.set()
        conn.settimeout(0.5)
        try:
            while True:
                trozo = conn.recv(4096)
                if not trozo:
                    break
                self.recibido.extend(trozo)
        except OSError:
            pass
        finally:
            conn.close()

    def cerrar(self):
        self.sock.close()
        self.hilo.join(3)


def directorio(base, nombre, modo=0o700):
    ruta = Path(base) / nombre
    ruta.mkdir()
    os.chmod(ruta, modo)
    return ruta


class PruebasResolucionRuta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-h08-ruta-")
        self.base = Path(self.tmp.name)
        self.run_user = self.base / "run-user"
        self.run_user.mkdir()
        self.raiz_tmp = self.base / "tmp-compartido"
        self.raiz_tmp.mkdir()
        os.chmod(self.raiz_tmp, 0o1777)

    def tearDown(self):
        self.tmp.cleanup()

    def resolver(self, entorno, uid=UID):
        return resolver_ruta_socket(
            entorno=entorno, uid=uid,
            raiz_run_user=self.run_user, raiz_tmp=self.raiz_tmp)

    def fallback(self, uid=UID):
        return self.raiz_tmp / f"parlar-{uid}" / "parlar.sock"

    def test_xdg_privado_se_respeta_aunque_viva_bajo_tmp(self):
        xdg = directorio(self.base, "xdg")
        self.assertEqual(
            self.resolver({"XDG_RUNTIME_DIR": str(xdg)}), xdg / "parlar.sock")

    def test_xdg_ausente_usa_run_user_privado(self):
        propio = directorio(self.run_user, str(UID))
        self.assertEqual(self.resolver({}), propio / "parlar.sock")

    def test_xdg_ausente_sin_run_user_usa_subdirectorio_por_uid(self):
        ruta = self.resolver({})
        self.assertEqual(ruta, self.fallback())
        # Propiedad real: nunca un socket suelto en el directorio compartido.
        self.assertNotEqual(ruta.parent, self.raiz_tmp)
        self.assertEqual(ruta.parent.name, f"parlar-{UID}")

    def test_xdg_invalido_cae_al_respaldo(self):
        inseguro = directorio(self.base, "xdg-0755", 0o755)
        grupo = directorio(self.base, "xdg-0770", 0o770)
        real = directorio(self.base, "xdg-real")
        enlace = self.base / "xdg-enlace"
        enlace.symlink_to(real, target_is_directory=True)
        archivo = self.base / "xdg-archivo"
        archivo.write_text("x")
        casos = {
            "permisos 0755": str(inseguro),
            "permisos 0770": str(grupo),
            "symlink": str(enlace),
            "archivo": str(archivo),
            "inexistente": str(self.base / "no-existe"),
            "relativo": "run/user",
            "vacío": "",
        }
        for nombre, valor in casos.items():
            with self.subTest(nombre):
                self.assertEqual(
                    self.resolver({"XDG_RUNTIME_DIR": valor}),
                    self.fallback())

    def test_xdg_de_otro_usuario_se_rechaza(self):
        xdg = directorio(self.base, "xdg")
        self.assertEqual(
            self.resolver({"XDG_RUNTIME_DIR": str(xdg)}, uid=OTRO_UID),
            self.fallback(OTRO_UID))

    def test_run_user_inseguro_se_rechaza(self):
        directorio(self.run_user, str(UID), 0o755)
        self.assertEqual(self.resolver({}), self.fallback())

    def test_ruta_que_excede_sun_path_cae_al_respaldo(self):
        profundo = self.base
        while len(os.fsencode(str(profundo / "parlar.sock"))) <= (
                MAX_BYTES_RUTA_SOCKET):
            profundo = profundo / ("d" * 20)
        profundo.mkdir(parents=True)
        os.chmod(profundo, 0o700)
        self.assertEqual(
            self.resolver({"XDG_RUNTIME_DIR": str(profundo)}),
            self.fallback())
        self.assertTrue(ipc.longitud_socket_valida(self.fallback()))

    def test_ruta_legada_reproduce_la_formula_previa(self):
        xdg = directorio(self.base, "xdg-0755", 0o755)
        self.assertEqual(
            ruta_socket_legada(entorno={"XDG_RUNTIME_DIR": str(xdg)}),
            xdg / "parlar.sock")
        self.assertEqual(
            ruta_socket_legada(entorno={}, uid=UID),
            Path(f"/tmp/parlar-{UID}.sock"))


class PruebasDirectorioPrivado(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-h08-dir-")
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crea_0700_si_falta(self):
        ruta = self.base / f"parlar-{UID}"
        with mock.patch("os.umask", return_value=0):
            asegurar_directorio_privado(ruta)
        info = os.lstat(ruta)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode) & 0o077, 0)
        self.assertEqual(info.st_uid, UID)
        asegurar_directorio_privado(ruta)  # idempotente

    def test_rechaza_permisos_amplios_sin_corregirlos(self):
        for modo in (0o755, 0o750, 0o707, 0o1777):
            with self.subTest(modo=oct(modo)):
                ruta = self.base / f"d-{modo:o}"
                ruta.mkdir()
                os.chmod(ruta, modo)
                with self.assertRaises(ErrorDirectorioInseguro):
                    asegurar_directorio_privado(ruta)
                self.assertEqual(stat.S_IMODE(os.lstat(ruta).st_mode), modo)

    def test_rechaza_symlink_aunque_apunte_a_directorio_privado(self):
        real = directorio(self.base, "real")
        enlace = self.base / "enlace"
        enlace.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ErrorDirectorioInseguro):
            asegurar_directorio_privado(enlace)

    def test_rechaza_archivo_regular(self):
        archivo = self.base / "archivo"
        archivo.write_text("x")
        with self.assertRaises(ErrorDirectorioInseguro):
            asegurar_directorio_privado(archivo)

    def test_rechaza_directorio_de_otro_usuario(self):
        ruta = directorio(self.base, "ajeno")
        with (
            mock.patch("os.getuid", return_value=OTRO_UID),
            self.assertRaisesRegex(ErrorDirectorioInseguro, "otro usuario"),
        ):
            asegurar_directorio_privado(ruta)

    def test_padre_inexistente_falla_cerrado(self):
        with self.assertRaises(ErrorDirectorioInseguro):
            asegurar_directorio_privado(self.base / "no" / "existe")


class PruebasCredencialesPeer(unittest.TestCase):
    def test_socketpair_real_es_del_mismo_uid(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(uid_peer(a), UID)
            verificar_peer(b)
        finally:
            a.close()
            b.close()

    def test_uid_ajeno_root_o_malformado_se_rechazan(self):
        casos = {
            "otro uid": SocketFalso(ucred(OTRO_UID)),
            "root": SocketFalso(ucred(0)),
            "corto": SocketFalso(b"\x00" * 4),
            "error": SocketFalso(error=OSError("EBADF")),
        }
        for nombre, falso in casos.items():
            with (
                self.subTest(nombre),
                self.assertRaises(PeerNoAutorizadoError),
            ):
                verificar_peer(falso)

    def test_sin_so_peercred_falla_cerrado(self):
        with (
            mock.patch.object(socket, "SO_PEERCRED", None, create=True),
            self.assertRaises(PeerNoAutorizadoError),
        ):
            verificar_peer(SocketFalso(ucred(UID)))

    def test_peer_no_autorizado_es_oserror_para_clientes_existentes(self):
        self.assertTrue(issubclass(PeerNoAutorizadoError, PermissionError))
        self.assertFalse(issubclass(
            PeerNoAutorizadoError,
            (ConnectionRefusedError, FileNotFoundError)))


class BaseControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-h08-ctl-")
        self.base = Path(self.tmp.name)
        self.ruta = self.base / "control.sock"
        self.recibidos = []
        self.servidores = []
        self.listeners = []

    def tearDown(self):
        for servidor in self.servidores:
            try:
                servidor.detener()
            except Exception:
                pass
        for listener in self.listeners:
            listener.cerrar()
        self.tmp.cleanup()

    def servidor(self, ruta=None, respuesta="OK"):
        def manejar(cmd):
            self.recibidos.append(cmd)
            return f"{respuesta} {cmd}"

        servidor = ServidorControl(manejar, ruta or self.ruta)
        servidor.iniciar()
        self.servidores.append(servidor)
        return servidor

    def impostor(self, ruta):
        listener = ListenerCrudo(ruta)
        self.listeners.append(listener)
        return listener


def peticion_cruda(ruta, comando, timeout=1.0):
    cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cliente.settimeout(timeout)
    try:
        cliente.connect(str(ruta))
        cliente.sendall((comando + "\n").encode("utf-8"))
        return cliente.recv(4096).decode("utf-8").strip()
    finally:
        cliente.close()


class PruebasServidor(BaseControl):
    def test_peer_autorizado_es_atendido(self):
        self.servidor()
        self.assertEqual(peticion_cruda(self.ruta, "estado"), "OK estado")
        self.assertEqual(stat.S_IMODE(os.lstat(self.ruta).st_mode), 0o600)

    def test_peer_de_otro_uid_se_cierra_sin_ejecutar_ni_consumir_cupos(self):
        self.servidor()
        stderr = io.StringIO()
        with mock.patch.object(ipc, "uid_peer", return_value=OTRO_UID), \
                contextlib.redirect_stderr(stderr):
            for _ in range(control_mod._MAX_CLIENTES + 4):
                try:
                    respuesta = peticion_cruda(self.ruta, "salir")
                except (ConnectionResetError, BrokenPipeError):
                    respuesta = ""  # cierre con datos sin leer: RST
                self.assertEqual(respuesta, "")
        self.assertEqual(self.recibidos, [])
        self.assertEqual(
            stderr.getvalue().count("peer no autorizado"), 1)
        # Ningún rechazo retuvo un cupo: el dueño sigue siendo atendido.
        self.assertEqual(peticion_cruda(self.ruta, "estado"), "OK estado")

    def test_directorio_compartido_impide_el_inicio_sin_crear_nada(self):
        compartido = directorio(self.base, "compartido", 0o1777)
        ruta = compartido / "control.sock"
        servidor = ServidorControl(lambda cmd: "OK", ruta)
        with self.assertRaises(ErrorDirectorioInseguro):
            servidor.iniciar()
        self.assertEqual(list(compartido.iterdir()), [])

    def test_directorio_por_symlink_impide_el_inicio(self):
        real = directorio(self.base, "real")
        enlace = self.base / "enlace"
        enlace.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ErrorDirectorioInseguro):
            ServidorControl(lambda cmd: "OK", enlace / "control.sock").iniciar()
        self.assertEqual(list(real.iterdir()), [])

    def test_ruta_demasiado_larga_falla_y_libera_el_lock(self):
        profundo = self.base
        while len(os.fsencode(str(profundo / "control.sock"))) <= (
                MAX_BYTES_RUTA_SOCKET):
            profundo = profundo / ("d" * 20)
        profundo.mkdir(parents=True)
        os.chmod(profundo, 0o700)
        ruta = profundo / "control.sock"
        with self.assertRaisesRegex(RuntimeError, "límite de AF_UNIX"):
            ServidorControl(lambda cmd: "OK", ruta).iniciar()
        guardia = GuardiaInstancia(ruta)
        guardia.adquirir()
        guardia.liberar()

    def test_symlink_preexistente_en_la_ruta_no_se_sigue_ni_borra(self):
        destino = self.base / "destino.sock"
        ajeno = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ajeno.bind(str(destino))
        try:
            self.ruta.symlink_to(destino)
            with self.assertRaisesRegex(RuntimeError, "no es socket"):
                ServidorControl(lambda cmd: "OK", self.ruta).iniciar()
            self.assertTrue(self.ruta.is_symlink())
            self.assertTrue(stat.S_ISSOCK(os.lstat(destino).st_mode))
        finally:
            ajeno.close()

    def test_lock_symlink_preexistente_se_rechaza(self):
        victima = self.base / "victima"
        victima.write_text("intacto")
        Path(f"{self.ruta}.lock").symlink_to(victima)
        with self.assertRaises(RuntimeError):
            ServidorControl(lambda cmd: "OK", self.ruta).iniciar()
        self.assertEqual(victima.read_text(), "intacto")

    def test_socket_huerfano_se_reemplaza(self):
        huerfano = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        huerfano.bind(str(self.ruta))
        huerfano.close()
        self.servidor()
        self.assertEqual(peticion_cruda(self.ruta, "estado"), "OK estado")

    def test_ciclo_inicio_cierre_reinicio_en_directorio_de_respaldo(self):
        respaldo = self.base / f"parlar-{UID}"
        ruta = respaldo / "parlar.sock"
        primero = ServidorControl(lambda cmd: f"uno {cmd}", ruta)
        primero.iniciar()
        try:
            self.assertEqual(stat.S_IMODE(os.lstat(respaldo).st_mode), 0o700)
            self.assertEqual(peticion_cruda(ruta, "estado"), "uno estado")
            with self.assertRaises(InstanciaActivaError):
                ServidorControl(lambda cmd: "dos", ruta).iniciar()
        finally:
            primero.detener()
        self.assertFalse(ruta.exists())
        segundo = ServidorControl(lambda cmd: f"dos {cmd}", ruta)
        segundo.iniciar()
        try:
            self.assertEqual(peticion_cruda(ruta, "estado"), "dos estado")
        finally:
            segundo.detener()


class PruebasCliente(BaseControl):
    def parches(self, legado=None):
        pila = contextlib.ExitStack()
        pila.enter_context(mock.patch.object(control_mod, "SOCKET_PATH", self.ruta))
        pila.enter_context(mock.patch.object(
            control_mod, "_ruta_legada_para",
            side_effect=lambda ruta: legado))
        return pila

    def test_cliente_rechaza_impostor_antes_de_enviar_datos(self):
        impostor = self.impostor(self.ruta)
        with (
            self.parches(),
            mock.patch.object(ipc, "uid_peer", return_value=OTRO_UID),
            self.assertRaises(PeerNoAutorizadoError),
        ):
            enviar_comando("hotkey-suspender v1 secreto 5000", timeout=0.5)
        impostor.aceptado.wait(1)
        impostor.cerrar()
        self.assertEqual(bytes(impostor.recibido), b"")

    def test_cliente_habla_con_daemon_propio(self):
        self.servidor()
        with self.parches():
            self.assertEqual(enviar_comando("estado"), "OK estado")

    def test_parlarctl_reporta_impostor_sin_traceback(self):
        self.impostor(self.ruta)
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.parches(), mock.patch.object(
                ipc, "uid_peer", return_value=OTRO_UID), \
                mock.patch.object(sys, "argv", ["parlarctl", "estado"]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as salida:
            control_mod.parlarctl_main()
        self.assertEqual(salida.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "el socket de control de ParlAR no pertenece a este usuario\n")

    def test_ruta_legada_alcanza_instancia_previa(self):
        legado = self.base / "legado.sock"
        self.servidor(legado, respuesta="VIEJA")
        with self.parches(legado):
            self.assertEqual(enviar_comando("salir"), "VIEJA salir")

    def test_impostor_en_ruta_legada_equivale_a_ausencia(self):
        legado = self.base / "legado.sock"
        impostor = self.impostor(legado)
        with (
            self.parches(legado),
            mock.patch.object(ipc, "uid_peer", return_value=OTRO_UID),
            self.assertRaises(FileNotFoundError),
        ):
            enviar_comando("abrir-configuracion", timeout=0.5)
        impostor.aceptado.wait(1)
        impostor.cerrar()
        self.assertEqual(bytes(impostor.recibido), b"")

    def test_lock_legado_ocupado_se_detecta_y_bloquea_duplicado(self):
        legado = self.base / "legado.sock"
        vieja = GuardiaInstancia(legado)
        vieja.adquirir()
        try:
            with self.parches(legado):
                self.assertTrue(lock_instancia_ocupado())
                with self.assertRaises(InstanciaActivaError):
                    GuardiaInstancia(self.ruta).adquirir()
        finally:
            vieja.liberar()
        with self.parches(legado):
            self.assertFalse(lock_instancia_ocupado())
            nueva = GuardiaInstancia(self.ruta)
            nueva.adquirir()
            nueva.liberar()

    def test_lock_legado_ajeno_no_cuenta_como_instancia(self):
        legado = self.base / "legado.sock"
        ajena = GuardiaInstancia(legado)
        ajena.adquirir()
        try:
            # Un lock plantado por otro UID (simulado) no prueba una instancia
            # propia: no debe bloquear el arranque ni el launcher.
            with self.parches(legado), mock.patch.object(
                    control_mod.os, "getuid", return_value=OTRO_UID):
                self.assertFalse(lock_instancia_ocupado())
        finally:
            ajena.liberar()

    def test_legado_solo_aplica_a_la_ruta_por_defecto(self):
        principal = self.base / "principal.sock"
        legado = self.base / "legado.sock"
        with mock.patch.object(control_mod._config, "SOCKET_PATH", principal), \
                mock.patch.object(control_mod, "SOCKET_PATH_LEGADO", legado):
            self.assertEqual(control_mod._ruta_legada_para(principal), legado)
            self.assertIsNone(control_mod._ruta_legada_para(self.ruta))
        with mock.patch.object(control_mod._config, "SOCKET_PATH", principal), \
                mock.patch.object(control_mod, "SOCKET_PATH_LEGADO", None):
            self.assertIsNone(control_mod._ruta_legada_para(principal))


class PruebasClientesAuxiliares(BaseControl):
    def test_tray_gtk_rechaza_peer_ajeno_sin_enviar(self):
        impostor = self.impostor(self.ruta)
        with (
            mock.patch.object(tray_gtk.os, "getuid", return_value=OTRO_UID),
            self.assertRaises(PermissionError),
        ):
            tray_gtk.enviar_comando(self.ruta, "salir", timeout=0.5)
        impostor.aceptado.wait(1)
        impostor.cerrar()
        self.assertEqual(bytes(impostor.recibido), b"")

    def test_tray_gtk_habla_con_daemon_propio(self):
        self.servidor()
        self.assertEqual(
            tray_gtk.enviar_comando(self.ruta, "estado-tray"),
            "OK estado-tray")

    def test_settings_y_restart_no_crean_locks_en_directorio_compartido(self):
        compartido = directorio(self.base, "compartido", 0o1777)
        with self.assertRaises(ErrorDirectorioInseguro):
            GuardiaVentanaSettings(
                compartido / "x.settings.lock"
            ).adquirir_o_solicitar_presentacion()
        coordinador = CoordinadorReinicio(
            guardia=GuardiaCoordinadorReinicio(compartido / "x.restart.lock"),
            enviar=mock.Mock(side_effect=AssertionError("no debe enviar")))
        resultado = coordinador.ejecutar()
        self.assertEqual(resultado.estado, EstadoResultadoReinicio.FALLIDO)
        self.assertEqual(
            coordinador.reintentar("manual").estado,
            EstadoResultadoReinicio.FALLIDO)
        self.assertEqual(list(compartido.iterdir()), [])

    def test_entry_point_reporta_directorio_inseguro_sin_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(entrada.Config, "load", return_value=Config()),
            mock.patch.object(sys, "argv", ["parlar"]),
            mock.patch.object(
                GuardiaInstancia, "adquirir_para_entry_point",
                side_effect=ErrorDirectorioInseguro("detalle")),
            mock.patch.object(entrada, "preparar_runtime_nvidia") as nvidia,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as salida,
        ):
            entrada.main()
        self.assertEqual(salida.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "[control] detalle\n")
        nvidia.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

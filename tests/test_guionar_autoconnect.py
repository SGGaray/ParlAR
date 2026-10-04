"""Integración automática ParlAR → GuionAR: autoconnect, reconnect y hello.

Usa el ClienteGuionAR real contra un servidor Unix compatible con GuionAR.
No carga micrófono, Whisper, CUDA, PortAudio ni listeners globales.
"""

import contextlib
import dataclasses
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import parlar.config as config_mod
from parlar import cliente_guionar as modulo
from parlar.__main__ import _crear_parser, main
from parlar.app import App
from parlar.cliente_guionar import (
    CONECTADO,
    DESCONECTADO,
    HELLO,
    ClienteGuionAR,
    ClienteNulo,
    crear_cliente,
)
from parlar.config import Config
from parlar.coordinador_salida import crear_coordinador_salida
from parlar.entrega import EstadoEntrega, ResultadoDistribucion
from parlar.estado_operativo import EstadoOperativoStore, serializar_estado
from parlar.settings_backend import (
    construir_configuracion_candidata,
    representar_estado_parlar,
    snapshot_configuracion,
)
from parlar.settings_window import texto_estado_guionar


REPO = Path(__file__).resolve().parent.parent


def esperar(condicion, timeout=3.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.01)
    return condicion()


def hilos_guionar():
    return [h for h in threading.enumerate()
            if h.name == "parlar-guionar" and h.is_alive()]


class GuionARMock:
    """Servidor mínimo con el contrato de GuionAR: JSON por líneas."""

    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.ruta))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.conexiones = []
        self._conns = []
        self._cv = threading.Condition()
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._aceptar, daemon=True)
        self._hilo.start()

    def _aceptar(self):
        while not self._parar.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            mensajes = []
            with self._cv:
                self.conexiones.append(mensajes)
                self._conns.append(conn)
                self._cv.notify_all()
            threading.Thread(
                target=self._leer, args=(conn, mensajes), daemon=True).start()

    def _leer(self, conn, mensajes):
        pendiente = b""
        try:
            while True:
                datos = conn.recv(65536)
                if not datos:
                    return
                pendiente += datos
                while b"\n" in pendiente:
                    linea, pendiente = pendiente.split(b"\n", 1)
                    with self._cv:
                        mensajes.append(json.loads(linea))
                        self._cv.notify_all()
        except OSError:
            return

    def esperar(self, predicado, timeout=3.0):
        with self._cv:
            return self._cv.wait_for(predicado, timeout)

    def mensajes(self, indice):
        with self._cv:
            return list(self.conexiones[indice])

    def lineas(self, indice):
        """Cantidad recibida; el cliente puede conectar antes del accept."""
        if len(self.conexiones) <= indice:
            return 0
        return len(self.conexiones[indice])

    def cortar_conexiones(self):
        with self._cv:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def detener(self):
        self._parar.set()
        self._hilo.join(2)
        self.listener.close()
        self.cortar_conexiones()
        try:
            self.ruta.unlink()
        except FileNotFoundError:
            pass


class BaseGuionAR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-guionar-auto-")
        os.chmod(self.tmp.name, 0o700)
        self.ruta = Path(self.tmp.name) / "guionar.sock"
        self.clientes = []
        self.servidores = []

    def tearDown(self):
        for cliente in self.clientes:
            cliente.cerrar()
        for servidor in self.servidores:
            servidor.detener()
        self.tmp.cleanup()

    def cliente(self, iniciar=True):
        cliente = ClienteGuionAR(str(self.ruta))
        self.clientes.append(cliente)
        if iniciar:
            self.assertTrue(cliente.iniciar())
        return cliente

    def servidor(self):
        servidor = GuionARMock(self.ruta)
        self.servidores.append(servidor)
        return servidor

    def detener(self, servidor):
        servidor.detener()
        self.servidores.remove(servidor)


class PruebasAutoconnect(BaseGuionAR):
    def test_arranca_sin_servidor_sin_bloquear_ni_encolar(self):
        cliente = self.cliente()
        inicio = time.perf_counter()
        for indice in range(1000):
            self.assertFalse(cliente.enviar_parcial(f"p{indice}"))
            self.assertFalse(cliente.evento_vad(indice % 2 == 0))
            self.assertFalse(cliente.enviar_limpiar())
            resultado = cliente.escribir_texto(f"t{indice}")
            self.assertFalse(resultado)
            self.assertEqual(resultado.estado, EstadoEntrega.SKIPPED)
        self.assertLess(time.perf_counter() - inicio, 0.5)
        self.assertEqual(cliente.estado, DESCONECTADO)

        # Lo enviado durante la ausencia no aparece al conectar.
        servidor = self.servidor()
        self.assertTrue(servidor.esperar(
            lambda: servidor.conexiones and servidor.conexiones[0]))
        self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
        self.assertTrue(cliente.enviar_parcial("nuevo"))
        self.assertTrue(servidor.esperar(
            lambda: servidor.lineas(0) >= 2))
        time.sleep(0.05)
        self.assertEqual(servidor.mensajes(0), [
            HELLO, {"type": "partial", "data": "nuevo"}])

    def test_hello_unico_y_antes_de_partial_text_vad(self):
        servidor = self.servidor()
        cliente = self.cliente()
        self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
        self.assertTrue(cliente.evento_vad(True))
        self.assertTrue(cliente.enviar_parcial("hola"))
        self.assertTrue(cliente.escribir_texto("hola mundo"))
        self.assertTrue(cliente.enviar_limpiar())
        self.assertTrue(cliente.evento_vad(False))
        self.assertTrue(servidor.esperar(
            lambda: servidor.lineas(0) >= 6))
        time.sleep(0.05)
        mensajes = servidor.mensajes(0)
        self.assertEqual(mensajes[0], HELLO)
        self.assertEqual(mensajes.count(HELLO), 1)
        self.assertEqual([m["type"] for m in mensajes[1:]],
                         ["vad", "partial", "text", "clear", "vad"])
        self.assertEqual(len(servidor.conexiones), 1)

    def test_iniciar_idempotente_un_unico_worker(self):
        cliente = self.cliente(iniciar=False)
        antes = len(hilos_guionar())
        resultados = []
        hilos = [threading.Thread(
            target=lambda: resultados.append(cliente.iniciar()))
            for _ in range(16)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(2)
        self.assertEqual(resultados, [True] * 16)
        self.assertEqual(len(hilos_guionar()) - antes, 1)

    def test_sin_busy_loop_ni_log_spam_con_guionar_ausente(self):
        cliente = self.cliente(iniciar=False)
        intentos = []
        original = cliente._conectar

        def contar():
            intentos.append(time.monotonic())
            return original()

        cliente._conectar = contar
        salida, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(salida), \
                contextlib.redirect_stderr(error):
            cliente.iniciar()
            time.sleep(3.2)
        # Backoff 0.25 → 0.5 → 1.0 → 1.5 s: unos 5 intentos en 3 s.
        self.assertGreaterEqual(len(intentos), 3)
        self.assertLessEqual(len(intentos), 6)
        self.assertEqual(salida.getvalue() + error.getvalue(), "")

    def test_conectado_en_idle_no_sondea(self):
        servidor = self.servidor()
        cliente = self.cliente(iniciar=False)
        intentos = []
        original = cliente._conectar
        cliente._conectar = lambda: intentos.append(1) or original()
        cliente.iniciar()
        self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
        inicio_cpu = time.process_time()
        time.sleep(1.0)
        self.assertEqual(len(intentos), 1)
        self.assertLess(time.process_time() - inicio_cpu, 0.1)
        self.assertEqual(len(servidor.conexiones), 1)

    def test_close_acotado_durante_backoff(self):
        cliente = self.cliente()
        time.sleep(0.9)  # ya en una espera de backoff de 0.5–1.0 s
        inicio = time.perf_counter()
        cliente.cerrar()
        self.assertLess(time.perf_counter() - inicio, 0.3)
        self.assertEqual(hilos_guionar(), [])
        self.assertFalse(cliente.iniciar())

    def test_close_durante_reconnect_no_publica_socket(self):
        servidor = self.servidor()
        en_validacion = threading.Event()
        liberar = threading.Event()
        cliente = self.cliente(iniciar=False)
        validar = cliente._validar_peer

        def validar_lento(sock):
            en_validacion.set()
            liberar.wait(2)
            return validar(sock)

        cliente._validar_peer = validar_lento
        cliente.iniciar()
        self.assertTrue(en_validacion.wait(3))
        cierre = threading.Thread(target=cliente.cerrar)
        cierre.start()
        time.sleep(0.05)
        liberar.set()
        cierre.join(2)
        self.assertFalse(cierre.is_alive())
        self.assertIsNone(cliente._sock)
        self.assertEqual(hilos_guionar(), [])
        self.assertTrue(servidor.esperar(lambda: servidor.conexiones))
        time.sleep(0.05)
        self.assertEqual(servidor.mensajes(0), [])

    def test_broken_pipe_en_envio_desconecta_sin_excepcion(self):
        servidor = self.servidor()
        cliente = self.cliente(iniciar=False)  # sin worker: aísla el emisor
        estados = []
        cliente.agregar_observador(estados.append)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cliente._conectar())
            cliente._notificar()  # lo que haría el worker tras conectar
        self.assertTrue(servidor.esperar(lambda: servidor.conexiones))
        servidor.cortar_conexiones()
        with contextlib.redirect_stdout(io.StringIO()):
            resultados = [cliente.enviar_parcial(f"x{i}") for i in range(5)]
        self.assertIn(False, resultados)
        self.assertEqual(cliente.estado, DESCONECTADO)
        self.assertIsNone(cliente._sock)
        self.assertEqual(estados, [CONECTADO, DESCONECTADO])

    def test_callback_deduplicado_y_tolerante(self):
        estados = []
        cliente = self.cliente(iniciar=False)

        def fallar(_estado):
            raise RuntimeError("observador roto")

        cliente.agregar_observador(fallar)
        cliente.agregar_observador(estados.append)
        servidor = self.servidor()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cliente.iniciar()
            self.assertTrue(esperar(lambda: estados == [CONECTADO]))
            for indice in range(50):
                cliente.enviar_parcial(f"p{indice}")
                cliente.evento_vad(indice % 2 == 0)
            self.assertEqual(estados, [CONECTADO])
            servidor.cortar_conexiones()
            self.assertTrue(esperar(lambda: len(estados) >= 2))
            self.assertTrue(esperar(lambda: len(estados) >= 3))
            cliente.cerrar()
        self.assertEqual(estados, [CONECTADO, DESCONECTADO, CONECTADO,
                                   DESCONECTADO])

    def test_peer_uid_ajeno_rechazado_por_el_worker_sin_spam(self):
        servidor = self.servidor()
        cliente = self.cliente(iniciar=False)
        cliente._expected_uid = os.getuid() + 1
        error = io.StringIO()
        with mock.patch.object(modulo, "_BACKOFF_S", (0.02,)), \
                contextlib.redirect_stderr(error), \
                contextlib.redirect_stdout(io.StringIO()):
            cliente.iniciar()
            self.assertTrue(servidor.esperar(
                lambda: len(servidor.conexiones) >= 5))
            cliente.cerrar()
        self.assertTrue(all(c == [] for c in servidor.conexiones))
        self.assertEqual(error.getvalue().count("peer rechazado"), 1)
        self.assertEqual(cliente.estado, DESCONECTADO)

    def test_envios_concurrentes_con_reconexion(self):
        servidor = self.servidor()
        cliente = self.cliente()
        self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
        errores = []

        def emitir(funcion, cantidad):
            try:
                for indice in range(cantidad):
                    funcion(indice)
                    if indice % 50 == 0:
                        time.sleep(0.001)
            except BaseException as exc:
                errores.append(exc)

        hilos = [
            threading.Thread(target=emitir, args=(
                lambda i: cliente.enviar_parcial(f"p{i}"), 600)),
            threading.Thread(target=emitir, args=(
                lambda i: cliente.escribir_texto(f"t{i}"), 600)),
            threading.Thread(target=emitir, args=(
                lambda i: cliente.evento_vad(i % 2 == 0), 600)),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            for hilo in hilos:
                hilo.start()
            time.sleep(0.01)
            servidor.cortar_conexiones()
            for hilo in hilos:
                hilo.join(10)
            self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
        self.assertFalse(any(hilo.is_alive() for hilo in hilos))
        self.assertEqual(errores, [])
        cliente.enviar_parcial("fin")
        self.assertTrue(servidor.esperar(lambda: any(
            {"type": "partial", "data": "fin"} in c
            for c in servidor.conexiones)))
        textos = []
        for conexion in list(servidor.conexiones):
            if not conexion:
                continue
            self.assertEqual(conexion[0], HELLO)
            self.assertEqual(conexion.count(HELLO), 1)
            textos.extend(int(m["data"][1:]) for m in conexion
                          if m["type"] == "text")
        # Orden conservado, sin duplicados ni replay entre conexiones.
        self.assertEqual(textos, sorted(set(textos)))


class PruebasE2E(BaseGuionAR):
    def test_flujo_completo_ausente_aparece_cae_y_vuelve(self):
        estados = []
        cliente = self.cliente(iniciar=False)
        cliente.agregar_observador(estados.append)
        with contextlib.redirect_stdout(io.StringIO()):
            cliente.iniciar()
            # 1. Servidor ausente: ParlAR sigue sin errores.
            self.assertFalse(cliente.enviar_parcial("antes"))

            # 2. Servidor aparece → auto-connect → hello.
            primero = self.servidor()
            self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
            self.assertTrue(cliente.enviar_parcial("hola"))
            self.assertTrue(cliente.escribir_texto("hola mundo"))
            self.assertTrue(cliente.evento_vad(True))
            self.assertTrue(primero.esperar(
                lambda: primero.lineas(0) >= 4))
            self.assertEqual(primero.mensajes(0), [
                HELLO,
                {"type": "partial", "data": "hola"},
                {"type": "text", "data": "hola mundo"},
                {"type": "vad", "data": True},
            ])

            # 3. Servidor desaparece → el cliente lo detecta solo.
            self.detener(primero)
            self.assertTrue(esperar(
                lambda: cliente.estado == DESCONECTADO, 1.0))
            perdidos = [
                cliente.enviar_parcial("perdido"),
                cliente.escribir_texto("final perdido"),
                cliente.evento_vad(False),
            ]
            self.assertFalse(any(perdidos))

            # 4. Servidor reaparece → reconnect en ~1–2 s → segundo hello.
            inicio = time.monotonic()
            segundo = self.servidor()
            self.assertTrue(esperar(lambda: cliente.estado == CONECTADO))
            self.assertLess(time.monotonic() - inicio, 2.5)
            self.assertTrue(cliente.enviar_parcial("después"))
            self.assertTrue(cliente.evento_vad(True))
            self.assertTrue(segundo.esperar(
                lambda: segundo.lineas(0) >= 3))
            time.sleep(0.05)
            self.assertEqual(segundo.mensajes(0), [
                HELLO,
                {"type": "partial", "data": "después"},
                {"type": "vad", "data": True},
            ])
        self.assertEqual(estados, [CONECTADO, DESCONECTADO, CONECTADO])


class PruebasProceso(BaseGuionAR):
    def ejecutar(self, codigo):
        entorno = dict(os.environ, PYTHONPATH=str(REPO))
        return subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(codigo)],
            cwd=REPO, env=entorno, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_sigterm_cierra_acotado_y_silencioso(self):
        proceso = self.ejecutar(f"""
            import signal, sys, time
            from parlar.cliente_guionar import ClienteGuionAR
            cliente = ClienteGuionAR({str(self.ruta)!r})
            cliente.iniciar()
            def terminar(*_):
                cliente.cerrar()
                sys.exit(0)
            signal.signal(signal.SIGTERM, terminar)
            print("listo", flush=True)
            while True:
                time.sleep(0.05)
        """)
        self.assertEqual(proceso.stdout.readline().strip(), "listo")
        time.sleep(0.3)
        inicio = time.monotonic()
        proceso.send_signal(signal.SIGTERM)
        _, error = proceso.communicate(timeout=5)
        self.assertEqual(proceso.returncode, 0)
        self.assertLess(time.monotonic() - inicio, 2)
        self.assertEqual(error, "")

    def test_worker_no_retiene_el_proceso_al_salir(self):
        proceso = self.ejecutar(f"""
            from parlar.cliente_guionar import ClienteGuionAR
            ClienteGuionAR({str(self.ruta)!r}).iniciar()
        """)
        proceso.communicate(timeout=5)
        self.assertEqual(proceso.returncode, 0)


class PruebasFabricaYApp(BaseGuionAR):
    def test_cliente_nulo_y_ruta_personalizada_o_default(self):
        nulo = crear_cliente(False, str(self.ruta))
        self.assertIsInstance(nulo, ClienteNulo)
        self.assertFalse(nulo.iniciar())
        self.assertIsNone(nulo.estado)
        nulo.agregar_observador(lambda _e: None)
        self.assertEqual(hilos_guionar(), [])

        real = crear_cliente(True, str(self.ruta))
        self.clientes.append(real)
        self.assertEqual(real.ruta, str(self.ruta))
        self.assertEqual(hilos_guionar(), [])  # sin hilo hasta iniciar()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}):
            self.assertEqual(crear_cliente(True, "").ruta, str(self.ruta))

    def app(self, cliente, guionar=True):
        app = App.__new__(App)
        app.cfg = Config(guionar=guionar)
        app.estado_operativo = EstadoOperativoStore()
        app.salida = types.SimpleNamespace(guionar=cliente)
        return app

    def test_app_publica_presencia_y_ausencia_no_es_warning(self):
        cliente = self.cliente(iniciar=False)
        app = self.app(cliente)
        app._iniciar_guionar()
        store = app.estado_operativo
        self.assertEqual(store.snapshot().guionar, DESCONECTADO)
        for estado in (EstadoEntrega.SKIPPED, EstadoEntrega.FAILED):
            app._registrar_resultado_salida(ResultadoDistribucion(
                EstadoEntrega.INSERTED, estado, EstadoEntrega.SKIPPED))
        self.assertEqual(store.snapshot().warnings, ())
        self.assertEqual(
            json.loads(serializar_estado(store.snapshot()))["warnings"], [])

        with contextlib.redirect_stdout(io.StringIO()):
            self.servidor()
            self.assertTrue(esperar(
                lambda: store.snapshot().guionar == CONECTADO))
            self.assertEqual(json.loads(serializar_estado(
                store.snapshot()))["guionar"], CONECTADO)
            self.assertTrue(store.snapshot().warnings == ())

    def test_app_con_integracion_desactivada_no_inicia(self):
        cliente = self.cliente(iniciar=False)
        app = self.app(cliente, guionar=False)
        app._iniciar_guionar()
        self.assertIsNone(cliente._hilo)
        self.assertIsNone(app.estado_operativo.snapshot().guionar)

    def test_shutdown_del_coordinador_detiene_el_worker(self):
        cfg = Config(guionar=True, guionar_socket=str(self.ruta))
        salida = crear_coordinador_salida(
            cfg, inyector=mock.Mock(), sesion=mock.Mock(es_nulo=True))
        self.assertIsInstance(salida.guionar, ClienteGuionAR)
        salida.guionar.iniciar()
        self.assertEqual(len(hilos_guionar()), 1)
        inicio = time.perf_counter()
        self.assertEqual(list(salida.cerrar()), [])
        self.assertLess(time.perf_counter() - inicio, 1.0)
        self.assertEqual(hilos_guionar(), [])


class PruebasSettings(unittest.TestCase):
    def test_estado_guionar_llega_a_settings(self):
        base = {"runtime": "ready", "status": "Listo", "message": "ok"}
        for valor, esperado in (
                ("connected", ("● Conectado", True)),
                ("disconnected", ("○ No detectado", False)),
                (None, ("○ Integración desactivada", False)),
                ("raro", ("○ Integración desactivada", False))):
            with self.subTest(valor=valor):
                estado = representar_estado_parlar(dict(base, guionar=valor))
                self.assertEqual(texto_estado_guionar(estado), esperado)
        detenido = representar_estado_parlar(None)
        self.assertFalse(texto_estado_guionar(detenido)[1])

    def test_settings_marca_eleccion_solo_si_cambia(self):
        base = Config()
        snapshot = snapshot_configuracion(base)
        igual = construir_configuracion_candidata(base, snapshot)
        self.assertFalse(igual.guionar_explicit)
        apagado = construir_configuracion_candidata(
            base, dataclasses.replace(snapshot, guionar=False))
        self.assertFalse(apagado.guionar)
        self.assertTrue(apagado.guionar_explicit)


class PruebasConfigYCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "config.json"
        self.parche = mock.patch.object(config_mod, "CONFIG_FILE", self.ruta)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        self.tmp.cleanup()

    def escribir(self, datos):
        self.ruta.write_text(json.dumps(datos), encoding="utf-8")

    def test_config_nueva_activa_integracion_automatica(self):
        self.assertTrue(Config().guionar)
        self.assertTrue(Config.load().guionar)

    def test_config_vieja_false_implicito_migra_a_automatico(self):
        for datos in (
                {"schema_version": 1, "guionar": False,
                 "guionar_socket": "/run/user/1/x.sock"},
                {"guionar": False},
                {"schema_version": 1}):
            with self.subTest(datos=datos):
                self.escribir(datos)
                cfg = Config.load()
                self.assertTrue(cfg.guionar)
                self.assertFalse(cfg.guionar_explicit)
                self.assertEqual(cfg.schema_version, Config.SCHEMA_VERSION)
        self.escribir({"guionar": False, "guionar_socket": "/x.sock"})
        self.assertEqual(Config.load().guionar_socket, "/x.sock")

    def test_eleccion_explicita_se_preserva_y_hace_roundtrip(self):
        self.escribir({"schema_version": 1, "guionar": False,
                       "guionar_explicit": True})
        cfg = Config.load()
        self.assertFalse(cfg.guionar)
        cfg.save()
        self.assertFalse(Config.load().guionar)
        self.escribir({"guionar": True, "guionar_explicit": False})
        self.assertTrue(Config.load().guionar)

    def test_marca_preservada_en_extras_por_rollback_se_recupera(self):
        # Una versión anterior guarda la clave desconocida dentro de extras.
        self.escribir({"schema_version": 1, "guionar": False,
                       "extras": {"guionar_explicit": True}})
        cfg = Config.load()
        self.assertFalse(cfg.guionar)
        self.assertTrue(cfg.guionar_explicit)
        self.assertNotIn("guionar_explicit", cfg.extras)

    def test_cli_compatible_y_desactivacion_explicita(self):
        parser = _crear_parser(Config())
        self.assertTrue(parser.parse_args([]).guionar)
        self.assertTrue(parser.parse_args(["--guionar"]).guionar)
        self.assertTrue(parser.parse_args(["--guionar-enabled"]).guionar)
        self.assertFalse(parser.parse_args(["--no-guionar"]).guionar)
        self.assertFalse(parser.parse_args(["--sin-guionar"]).guionar)
        self.assertEqual(parser.parse_args(
            ["--guionar-socket", "/x.sock"]).guionar_socket, "/x.sock")
        apagado = _crear_parser(Config(guionar=False, guionar_explicit=True))
        self.assertFalse(apagado.parse_args([]).guionar)
        self.assertTrue(apagado.parse_args(["--guionar"]).guionar)
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            parser.parse_args(["--guionar", "--no-guionar"])

    def mostrar_config(self, *flags):
        salida = io.StringIO()
        with mock.patch.object(sys, "argv", ["parlar", *flags,
                                             "--mostrar-config"]), \
                contextlib.redirect_stdout(salida):
            main()
        return json.loads(salida.getvalue())

    def test_main_marca_explicito_solo_al_cambiar(self):
        datos = self.mostrar_config("--no-guionar")
        self.assertFalse(datos["guionar"])
        self.assertTrue(datos["guionar_explicit"])
        datos = self.mostrar_config("--guionar")
        self.assertTrue(datos["guionar"])
        self.assertFalse(datos["guionar_explicit"])
        datos = self.mostrar_config()
        self.assertTrue(datos["guionar"])
        self.assertFalse(datos["guionar_explicit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Regresiones de cota, deadline y lifecycle para Ollama opt-in."""

import contextlib
import io
import json
import socket
import socketserver
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import parlar.procesador_texto as procesador_mod
from parlar.app import EstadoApp
from parlar.procesador_texto import ProcesadorTexto
from tests.test_lifecycle import MicFalso, crear_app


_AUTOMATICO = object()


class PlanRespuesta:
    def __init__(self, cuerpo=b"", *, content_length=_AUTOMATICO,
                 chunked=False, intervalo=0.0, liberar_cuerpo=None,
                 desconectar=False):
        self.cuerpo = cuerpo
        self.content_length = content_length
        self.chunked = chunked
        self.intervalo = intervalo
        self.liberar_cuerpo = liberar_cuerpo
        self.desconectar = desconectar
        self.request = b""
        self.request_recibida = threading.Event()
        self.cuerpo_iniciado = threading.Event()
        self.finalizada = threading.Event()


class _HandlerOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        plan = self.server.plan
        cantidad = int(self.headers.get("Content-Length", "0"))
        plan.request = self.rfile.read(cantidad)
        plan.request_recibida.set()
        if plan.desconectar:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            plan.finalizada.set()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if plan.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif plan.content_length is not None:
            declarado = (
                len(plan.cuerpo)
                if plan.content_length is _AUTOMATICO
                else plan.content_length
            )
            self.send_header("Content-Length", str(declarado))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.flush()

        if plan.liberar_cuerpo is not None:
            plan.liberar_cuerpo.wait(1)
        plan.cuerpo_iniciado.set()
        try:
            if plan.chunked:
                for byte in plan.cuerpo:
                    self.wfile.write(b"1\r\n" + bytes((byte,)) + b"\r\n")
                    self.wfile.flush()
                    if plan.intervalo:
                        time.sleep(plan.intervalo)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            elif plan.intervalo:
                for byte in plan.cuerpo:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(plan.intervalo)
            else:
                self.wfile.write(plan.cuerpo)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            plan.finalizada.set()

    def log_message(self, *_args):
        pass


class ServidorOllama:
    def __init__(self, plan):
        self.plan = plan
        self.server = None
        self.thread = None

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HandlerOllama)
        self.server.plan = self.plan
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_args):
        if self.plan.liberar_cuerpo is not None:
            self.plan.liberar_cuerpo.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class PlanTrickleInterno:
    def __init__(self, etapa, *, repeticiones=30, intervalo=0.04):
        self.etapa = etapa
        self.repeticiones = repeticiones
        self.intervalo = intervalo
        self.request = b""
        self.request_recibida = threading.Event()
        self.finalizada = threading.Event()


class _ServidorTrickleInterno(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _HandlerTrickleInterno(socketserver.BaseRequestHandler):
    def handle(self):
        plan = self.server.plan
        recibido = b""
        while b"\r\n\r\n" not in recibido:
            trozo = self.request.recv(4096)
            if not trozo:
                return
            recibido += trozo
        cabeceras, cuerpo = recibido.split(b"\r\n\r\n", 1)
        cantidad = 0
        for linea in cabeceras.split(b"\r\n"):
            if linea.lower().startswith(b"content-length:"):
                cantidad = int(linea.split(b":", 1)[1])
        while len(cuerpo) < cantidad:
            trozo = self.request.recv(4096)
            if not trozo:
                return
            cuerpo += trozo
        plan.request = cuerpo
        plan.request_recibida.set()

        try:
            if plan.etapa == "headers":
                self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(plan.repeticiones):
                    self.request.sendall(b"a")
                    time.sleep(plan.intervalo)
                respuesta = cuerpo_json("tarde")
                self.request.sendall(
                    b"\r\nContent-Length: "
                    + str(len(respuesta)).encode("ascii")
                    + b"\r\nConnection: close\r\n\r\n"
                    + respuesta
                )
            elif plan.etapa == "framing":
                self.request.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n1;"
                )
                for _ in range(plan.repeticiones):
                    self.request.sendall(b"a")
                    time.sleep(plan.intervalo)
                self.request.sendall(b"\r\nX\r\n0\r\n\r\n")
            else:
                raise AssertionError(plan.etapa)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            plan.finalizada.set()


class ServidorTrickleInterno:
    def __init__(self, plan):
        self.plan = plan
        self.server = None
        self.thread = None

    def __enter__(self):
        self.server = _ServidorTrickleInterno(
            ("127.0.0.1", 0), _HandlerTrickleInterno)
        self.server.plan = self.plan
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def cuerpo_json(texto):
    return json.dumps({"response": texto}).encode()


def procesador(url):
    return ProcesadorTexto(
        rewrite_mode="formal",
        ollama_model="modelo-prueba",
        ollama_url=url,
        voice_commands=False,
    )


class PruebasLimitesOllama(unittest.TestCase):
    def test_respuesta_normal_pequena_conserva_resultado(self):
        plan = PlanRespuesta(cuerpo_json("Texto transformado"))
        with ServidorOllama(plan) as url:
            resultado = procesador(url).procesar_frase("texto original")
        self.assertEqual(resultado.texto, "Texto transformado")

    def test_content_length_excesivo_se_rechaza_antes_del_body(self):
        liberar = threading.Event()
        maximo = procesador_mod._OLLAMA_MAX_RESPONSE_BYTES
        plan = PlanRespuesta(
            cuerpo_json("tarde"), content_length=maximo + 1,
            liberar_cuerpo=liberar)
        with ServidorOllama(plan) as url, \
                contextlib.redirect_stderr(io.StringIO()):
            inicio = time.monotonic()
            salida = procesador(url)._reescribir_ollama("texto")
            duracion = time.monotonic() - inicio
            self.assertIsNone(salida)
            self.assertLess(duracion, 0.5)
            self.assertFalse(plan.cuerpo_iniciado.is_set())

    def test_sin_content_length_rechaza_body_mayor_al_maximo(self):
        maximo = procesador_mod._OLLAMA_MAX_RESPONSE_BYTES
        base = cuerpo_json("ok")
        plan = PlanRespuesta(
            base + b" " * (maximo + 1 - len(base)),
            content_length=None,
        )
        with ServidorOllama(plan) as url, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(
                procesador(url)._reescribir_ollama("texto"))

    def test_frontera_exacta_acepta_y_un_byte_mas_rechaza(self):
        maximo = procesador_mod._OLLAMA_MAX_RESPONSE_BYTES
        base = cuerpo_json("exacto")
        for extra, esperado in ((0, "exacto"), (1, None)):
            with self.subTest(extra=extra):
                plan = PlanRespuesta(
                    base + b" " * (maximo + extra - len(base)),
                    content_length=None,
                )
                with ServidorOllama(plan) as url, \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        procesador(url)._reescribir_ollama("texto"),
                        esperado,
                    )

    def test_chunked_trickle_respeta_deadline_total(self):
        plan = PlanRespuesta(
            cuerpo_json("ok") + b" " * 20,
            chunked=True,
            intervalo=0.03,
        )
        with mock.patch.object(
                procesador_mod, "_OLLAMA_DEADLINE_S", 0.12), \
                ServidorOllama(plan) as url, \
                contextlib.redirect_stderr(io.StringIO()):
            inicio = time.monotonic()
            salida = procesador(url)._reescribir_ollama("texto")
            duracion = time.monotonic() - inicio
        self.assertIsNone(salida)
        self.assertGreaterEqual(duracion, 0.08)
        self.assertLess(duracion, 0.5)

    def test_headers_y_framing_trickle_respetan_deadline_absoluto(self):
        for etapa in ("headers", "framing"):
            with self.subTest(etapa=etapa):
                plan = PlanTrickleInterno(etapa)
                with mock.patch.object(
                        procesador_mod, "_OLLAMA_DEADLINE_S", 0.25), \
                        ServidorTrickleInterno(plan) as url, \
                        contextlib.redirect_stderr(io.StringIO()):
                    inicio = time.monotonic()
                    salida = procesador(url)._reescribir_ollama("texto")
                    duracion = time.monotonic() - inicio
                self.assertIsNone(salida)
                self.assertGreaterEqual(duracion, 0.18)
                self.assertLess(duracion, 0.65)
                self.assertTrue(plan.finalizada.wait(0.5))

    def test_shutdown_durante_headers_y_framing_termina_por_deadline(self):
        for etapa in ("headers", "framing"):
            with self.subTest(etapa=etapa):
                plan = PlanTrickleInterno(etapa)
                with mock.patch.object(
                        procesador_mod, "_OLLAMA_DEADLINE_S", 0.25), \
                        ServidorTrickleInterno(plan) as url, \
                        contextlib.redirect_stderr(io.StringIO()):
                    app, mic, *_ = crear_app(
                        mic=MicFalso(), proc=procesador(url))
                    self.assertTrue(app.iniciar_grabacion())
                    self.assertTrue(mic.enviar(1))
                    mic.enviar(0)
                    self.assertTrue(plan.request_recibida.wait(2))
                    inicio = time.monotonic()
                    app.salir()
                    duracion = time.monotonic() - inicio
                self.assertLess(duracion, 0.65)
                self.assertEqual(app.estado, EstadoApp.CLOSED)
                self.assertFalse(app._trabajador_hilo.is_alive())
                self.assertTrue(plan.finalizada.wait(0.5))

    def test_cancel_y_start_descartan_trickle_viejo_sin_output(self):
        for etapa in ("headers", "framing"):
            with self.subTest(etapa=etapa):
                plan = PlanTrickleInterno(etapa)
                with mock.patch.object(
                        procesador_mod, "_OLLAMA_DEADLINE_S", 0.25), \
                        ServidorTrickleInterno(plan) as url, \
                        contextlib.redirect_stderr(io.StringIO()):
                    app, mic, _, _, _, _, sesion, _ = crear_app(
                        mic=MicFalso(), proc=procesador(url))
                    self.assertTrue(app.iniciar_grabacion())
                    generacion_vieja = app.generacion_activa
                    self.assertTrue(mic.enviar(1))
                    mic.enviar(0)
                    self.assertTrue(plan.request_recibida.wait(2))
                    self.assertTrue(app.cancelar_grabacion())
                    self.assertTrue(app.iniciar_grabacion())
                    self.assertNotEqual(
                        generacion_vieja, app.generacion_activa)
                    self.assertTrue(plan.finalizada.wait(0.65))
                    limite = time.monotonic() + 0.65
                    while (app._trabajador_hilo.is_alive()
                           and time.monotonic() < limite):
                        time.sleep(0.01)
                    self.assertEqual(sesion.textos, [])
                    self.assertEqual(app.estado, EstadoApp.RECORDING)
                    app.salir()

    def test_timeouts_repetidos_recolectan_procesos_y_permiten_reintento(self):
        procesos = []
        popen_real = procesador_mod.subprocess.Popen

        def capturar_proceso(*args, **kwargs):
            proceso_hijo = popen_real(*args, **kwargs)
            procesos.append(proceso_hijo)
            return proceso_hijo

        hilos_ollama_antes = {
            hilo.ident for hilo in threading.enumerate()
            if "ollama" in hilo.name.lower()
        }
        with mock.patch.object(
                procesador_mod.subprocess, "Popen",
                side_effect=capturar_proceso), \
                mock.patch.object(
                    procesador_mod, "_OLLAMA_DEADLINE_S", 0.20), \
                contextlib.redirect_stderr(io.StringIO()):
            for _ in range(3):
                plan = PlanTrickleInterno("headers")
                with ServidorTrickleInterno(plan) as url:
                    self.assertIsNone(
                        procesador(url)._reescribir_ollama("texto"))
                    self.assertTrue(plan.finalizada.wait(0.5))

            normal = PlanRespuesta(cuerpo_json("reintento sano"))
            with ServidorOllama(normal) as url:
                self.assertEqual(
                    procesador(url)._reescribir_ollama("texto"),
                    "reintento sano",
                )

        self.assertEqual(len(procesos), 4)
        self.assertTrue(all(proceso.poll() is not None for proceso in procesos))
        self.assertTrue(all(
            proceso.returncode is not None for proceso in procesos))
        self.assertEqual(
            {
                hilo.ident for hilo in threading.enumerate()
                if "ollama" in hilo.name.lower()
            },
            hilos_ollama_antes,
        )

    def test_json_truncado_invalido_y_corte_fallan_controlado(self):
        planes = (
            ("truncado", PlanRespuesta(b'{"response":')),
            ("invalido", PlanRespuesta(b"no-json")),
            ("corte", PlanRespuesta(desconectar=True)),
        )
        for nombre, plan in planes:
            with self.subTest(caso=nombre), ServidorOllama(plan) as url, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(
                    procesador(url)._reescribir_ollama("texto"))

    def test_cancel_durante_rewrite_no_publica_generacion_vieja(self):
        liberar = threading.Event()
        plan = PlanRespuesta(
            cuerpo_json("reescrito"), liberar_cuerpo=liberar)
        with ServidorOllama(plan) as url:
            proc = procesador(url)
            app, mic, _, _, _, _, sesion, _ = crear_app(
                mic=MicFalso(), proc=proc)
            self.assertTrue(app.iniciar_grabacion())
            generacion_vieja = app.generacion_activa
            emitido_viejo = threading.Event()
            emitir_original = app._emitir

            def emitir_observado(valor, generacion):
                try:
                    return emitir_original(valor, generacion)
                finally:
                    if generacion == generacion_vieja:
                        emitido_viejo.set()

            app._emitir = emitir_observado
            self.assertTrue(mic.enviar(1))
            mic.enviar(0)
            self.assertTrue(plan.request_recibida.wait(2))
            self.assertTrue(app.cancelar_grabacion())
            self.assertTrue(app.iniciar_grabacion())
            generacion_nueva = app.generacion_activa
            liberar.set()
            self.assertTrue(emitido_viejo.wait(2))
            self.assertNotEqual(generacion_vieja, generacion_nueva)
            self.assertEqual(app.estado, EstadoApp.RECORDING)
            self.assertEqual(app.generacion_activa, generacion_nueva)
            self.assertEqual(sesion.textos, [])
            app.salir()

    def test_shutdown_durante_rewrite_termina_acotado(self):
        plan = PlanRespuesta(
            cuerpo_json("ok") + b" " * 80,
            chunked=True,
            intervalo=0.03,
        )
        with mock.patch.object(
                procesador_mod, "_OLLAMA_DEADLINE_S", 0.12), \
                ServidorOllama(plan) as url:
            app, mic, *_ = crear_app(mic=MicFalso(), proc=procesador(url))
            self.assertTrue(app.iniciar_grabacion())
            self.assertTrue(mic.enviar(1))
            mic.enviar(0)
            self.assertTrue(plan.request_recibida.wait(2))
            inicio = time.monotonic()
            app.salir()
            duracion = time.monotonic() - inicio
            self.assertLess(duracion, 0.5)
            self.assertEqual(app.estado, EstadoApp.CLOSED)
            self.assertFalse(app._trabajador_hilo.is_alive())

    def test_sentinels_de_request_y_response_no_aparecen_en_logs(self):
        request_sentinel = "requestsentinel7f89"
        response_sentinel = "RESPONSE_SENTINEL_b613"
        plan = PlanRespuesta(
            ('{"response":"' + response_sentinel).encode())
        salida = io.StringIO()
        error = io.StringIO()
        with ServidorOllama(plan) as url, \
                contextlib.redirect_stdout(salida), \
                contextlib.redirect_stderr(error):
            procesador(url).procesar_frase(request_sentinel)
        logs = salida.getvalue() + error.getvalue()
        self.assertIn(request_sentinel, plan.request.decode())
        self.assertNotIn(request_sentinel, logs)
        self.assertNotIn(response_sentinel, logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Política de destino del transporte Ollama: proxies, redirecciones y URLs.

Sólo servidores HTTP y proxies simulados en loopback, con texto sintético.
Nunca se contacta un Ollama real ni un host remoto: los destinos "remotos"
se resuelven a través del proxy simulado o se rechazan antes de conectar.
"""

import contextlib
import inspect
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import parlar.procesador_texto as procesador_mod
from parlar.config import Config, ErrorConfiguracion
from parlar.procesador_texto import (
    ProcesadorTexto,
    host_es_loopback,
    host_no_especificado,
    motivo_bloqueo_ollama,
)

SENTINEL_TEXTO = "SENTINELA_DICTADO_SINTETICO_7f3a"
SENTINEL_SECRETO = "SENTINELA_CLAVE_SINTETICA_91c2"
VARIABLES_PROXY = (
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
)


def _ipv6_disponible() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as prueba:
            prueba.bind(("::1", 0))
        return True
    except OSError:
        return False


IPV6 = _ipv6_disponible()


class _ServidorIPv6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class ServidorRegistro:
    """Servidor HTTP de loopback que registra cada request recibida."""

    def __init__(self, nombre, respuesta=None, ipv6=False):
        self.nombre = nombre
        self.registro = []
        self._respuesta = respuesta or (lambda handler: (200, {}))
        registro = self.registro
        comportamiento = self._respuesta

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _atender(self):
                largo = int(self.headers.get("Content-Length") or 0)
                cuerpo = self.rfile.read(largo) if largo else b""
                registro.append({
                    "metodo": self.command,
                    "ruta": self.path,
                    "sentinela": SENTINEL_TEXTO.encode() in cuerpo,
                })
                codigo, cabeceras = comportamiento(self)
                datos = json.dumps({"response": f"desde-{nombre}"}).encode()
                self.send_response(codigo)
                for clave, valor in cabeceras.items():
                    self.send_header(clave, valor)
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def do_CONNECT(self):
                registro.append({"metodo": "CONNECT", "ruta": self.path,
                                 "sentinela": False})
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _atender

        clase = _ServidorIPv6 if ipv6 else ThreadingHTTPServer
        self.servidor = clase(("::1" if ipv6 else "127.0.0.1", 0), Handler)
        self.puerto = self.servidor.server_address[1]
        self._hilo = threading.Thread(
            target=self.servidor.serve_forever, daemon=True)
        self._hilo.start()

    def url(self, host="127.0.0.1", esquema="http"):
        return f"{esquema}://{host}:{self.puerto}"

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        self._hilo.join(timeout=5)


@contextlib.contextmanager
def entorno_proxy(**variables):
    """Reemplaza todas las variables de proxy del entorno por ``variables``."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for nombre in VARIABLES_PROXY:
            os.environ.pop(nombre, None)
        os.environ.update(variables)
        yield


def cuerpo_sintetico() -> bytes:
    return json.dumps({"model": "modelo-prueba", "prompt": SENTINEL_TEXTO,
                       "stream": False}).encode()


def transportar(url: str) -> str:
    return procesador_mod._ejecutar_ollama_aislado(
        f"{url}/api/generate", cuerpo_sintetico(), time.monotonic() + 10)


class ConServidores(unittest.TestCase):
    def servidor(self, nombre, respuesta=None, ipv6=False):
        servidor = ServidorRegistro(nombre, respuesta, ipv6)
        self.addCleanup(servidor.cerrar)
        return servidor


class PoliticaDeDestino(unittest.TestCase):
    def test_loopback_reconocido_y_no_loopback(self):
        loopback = ("127.0.0.1", "127.8.9.10", "::1", "localhost",
                    "LOCALHOST", "localhost.", "::ffff:127.0.0.1",
                    "127.1", "0x7f000001", "2130706433", "0x7f.1")
        remotos = ("10.0.0.1", "192.168.1.10", "0.0.0.1", "fe80::1",
                   "0.0.0.0", "::", "0", "example.com", "deadbeef",
                   "localhost.example.com", "127.0.0.1.example.com", "",
                   None)
        for host in loopback:
            with self.subTest(host=host):
                self.assertTrue(host_es_loopback(host))
        for host in remotos:
            with self.subTest(host=host):
                self.assertFalse(host_es_loopback(host))

    def test_direcciones_no_especificadas(self):
        for host in ("0.0.0.0", "::", "0", "0.0", "0x0",
                     "::ffff:0.0.0.0", "0.0.0.0."):
            with self.subTest(host=host):
                self.assertTrue(host_no_especificado(host))
        for host in ("127.0.0.1", "::1", "localhost", "0.0.0.1",
                     "10.0.0.1", "ollama.lan", "", None):
            with self.subTest(host=host):
                self.assertFalse(host_no_especificado(host))

    def test_matriz_de_politica(self):
        casos = (
            ("http://127.0.0.1:11434", False, None),
            ("http://localhost:11434", False, None),
            ("http://[::1]:11434", False, None),
            ("http://127.1:11434", False, None),
            ("http://0x7f000001:11434", False, None),
            ("http://0.0.0.0:11434", False, "no_especificada"),
            ("http://0.0.0.0:11434", True, "no_especificada"),
            ("https://0.0.0.0:11434", False, "no_especificada"),
            ("http://[::]:11434", False, "no_especificada"),
            ("https://[::]:11434", True, "no_especificada"),
            ("http://0:11434", True, "no_especificada"),
            ("http://[::ffff:0.0.0.0]:11434", False, "no_especificada"),
            ("https://127.0.0.1:11434", False, None),
            ("https://ollama.example", False, None),
            ("https://10.0.0.5:11434", False, None),
            ("http://192.168.1.10:11434", False, "http_remoto"),
            ("http://192.168.1.10:11434", True, None),
            ("http://ollama.lan:11434", False, "http_remoto"),
            ("http://127.0.0.1.example.com", False, "http_remoto"),
            (f"https://u:{SENTINEL_SECRETO}@ollama.example", False,
             "credenciales"),
            (f"https://u:{SENTINEL_SECRETO}@ollama.example", True,
             "credenciales"),
            ("http://usuario@127.0.0.1:11434", False, "credenciales"),
            ("http://[::1", False, None),  # malformada: falla el transporte
        )
        for url, permitir, esperado in casos:
            with self.subTest(url=url, permitir=permitir):
                self.assertEqual(motivo_bloqueo_ollama(url, permitir), esperado)


class ProcesadorAplicaPolitica(unittest.TestCase):
    def _procesador(self, url, permitir=False):
        return ProcesadorTexto(
            rewrite_mode="formal", ollama_model="modelo-prueba",
            ollama_url=url, voice_commands=False,
            ollama_permitir_http=permitir)

    def test_destinos_bloqueados_no_lanzan_proceso_y_usan_fallback(self):
        for url in (f"https://u:{SENTINEL_SECRETO}@ollama.example",
                    f"http://u:{SENTINEL_SECRETO}@0.0.0.0:11434",
                    "http://192.168.1.10:11434",
                    "http://0.0.0.0:11434", "https://[::]:11434"):
            with self.subTest(url=url), \
                    mock.patch.object(procesador_mod.subprocess,
                                      "Popen") as popen, \
                    contextlib.redirect_stderr(io.StringIO()) as errores:
                procesador = self._procesador(url)
                for _ in range(3):
                    self.assertEqual(
                        procesador.procesar_frase(f"ok {SENTINEL_TEXTO}").texto,
                        f"De acuerdo {SENTINEL_TEXTO}")
                popen.assert_not_called()
                diagnostico = errores.getvalue()
                self.assertEqual(diagnostico.count("[procesador]"), 1)
                self.assertIn("fallback local", diagnostico)
                self.assertNotIn(SENTINEL_SECRETO, diagnostico)
                self.assertNotIn(SENTINEL_TEXTO, diagnostico)
                self.assertNotIn("://", diagnostico)
                self.assertNotIn("192.168.1.10", diagnostico)

    def test_no_especificada_recomienda_loopback_aun_con_opt_in(self):
        for url in ("http://0.0.0.0:11434", "http://[::]:11434"):
            with self.subTest(url=url), \
                    mock.patch.object(procesador_mod.subprocess,
                                      "Popen") as popen, \
                    contextlib.redirect_stderr(io.StringIO()) as errores:
                procesador = self._procesador(url, permitir=True)
                self.assertEqual(procesador.procesar_frase("ok").texto,
                                 "De acuerdo")
                popen.assert_not_called()
                self.assertIn("127.0.0.1", errores.getvalue())
                self.assertIn("fallback local", errores.getvalue())

    def test_opt_in_http_remoto_lanza_el_transporte(self):
        with mock.patch.object(procesador_mod, "_ejecutar_ollama_aislado",
                               return_value="De acuerdo, reescrito") as envio:
            procesador = self._procesador("http://192.168.1.10:11434", True)
            self.assertEqual(procesador.procesar_frase("ok").texto,
                             "De acuerdo, reescrito")
        envio.assert_called_once()
        self.assertEqual(envio.call_args.args[0],
                         "http://192.168.1.10:11434/api/generate")

    def test_reescritura_apagada_no_avisa(self):
        with contextlib.redirect_stderr(io.StringIO()) as errores:
            procesador = ProcesadorTexto(
                rewrite_mode="none", ollama_model="modelo-prueba",
                ollama_url="http://192.168.1.10:11434")
            procesador.procesar_frase("hola")
        self.assertEqual(errores.getvalue(), "")


class LoopbackSinProxy(ConServidores):
    def _comprobar_directo(self, host, ipv6=False):
        ollama = self.servidor("ollama", ipv6=ipv6)
        proxy = self.servidor("proxy")
        direccion = proxy.url()
        variantes = (
            {},
            {"http_proxy": direccion, "HTTP_PROXY": direccion,
             "https_proxy": direccion, "HTTPS_PROXY": direccion,
             "all_proxy": direccion, "ALL_PROXY": direccion},
            {"http_proxy": direccion, "no_proxy": "otro.example"},
        )
        for variables in variantes:
            with self.subTest(host=host, variables=sorted(variables)):
                ollama.registro.clear()
                proxy.registro.clear()
                with entorno_proxy(**variables):
                    self.assertEqual(transportar(ollama.url(host)),
                                     "desde-ollama")
                self.assertEqual(proxy.registro, [])
                self.assertEqual(len(ollama.registro), 1)
                self.assertTrue(ollama.registro[0]["sentinela"])

    def test_127_0_0_1_no_usa_proxy(self):
        self._comprobar_directo("127.0.0.1")

    def test_ipv4_abreviada_de_loopback_no_usa_proxy(self):
        self._comprobar_directo("127.1")

    def test_localhost_no_usa_proxy(self):
        self._comprobar_directo("localhost")

    @unittest.skipUnless(IPV6, "IPv6 de loopback no disponible")
    def test_ipv6_loopback_no_usa_proxy(self):
        self._comprobar_directo("[::1]", ipv6=True)

    def test_https_loopback_no_abre_tunel_en_el_proxy(self):
        # El servidor habla HTTP: el handshake TLS falla, pero nunca vía proxy.
        destino = self.servidor("destino")
        proxy = self.servidor("proxy")
        with entorno_proxy(https_proxy=proxy.url(), HTTPS_PROXY=proxy.url()):
            with self.assertRaises(OSError):
                transportar(destino.url(esquema="https"))
        self.assertEqual(proxy.registro, [])

    def test_destino_remoto_conserva_el_proxy_del_entorno(self):
        proxy = self.servidor("proxy")
        with entorno_proxy(http_proxy=proxy.url()):
            self.assertEqual(transportar("http://ollama.remoto.test"),
                             "desde-proxy")
        self.assertEqual(len(proxy.registro), 1)
        self.assertEqual(proxy.registro[0]["ruta"],
                         "http://ollama.remoto.test/api/generate")


class SinRedirecciones(ConServidores):
    CODIGOS = (301, 302, 303, 307, 308)

    def test_ninguna_redireccion_se_sigue(self):
        destino = self.servidor("destino")
        for codigo in self.CODIGOS:
            with self.subTest(codigo=codigo):
                destino.registro.clear()
                origen = self.servidor(
                    f"redireccion-{codigo}",
                    lambda handler, codigo=codigo: (
                        codigo, {"Location": f"{destino.url()}/otro"}))
                with entorno_proxy(), self.assertRaises(OSError):
                    transportar(origen.url())
                self.assertEqual(len(origen.registro), 1)
                self.assertEqual(destino.registro, [])

    def test_redireccion_vuelve_al_fallback_sin_filtrar_texto(self):
        destino = self.servidor("destino")
        origen = self.servidor(
            "redireccion",
            lambda handler: (302, {"Location": f"{destino.url()}/otro"}))
        with entorno_proxy(), \
                contextlib.redirect_stderr(io.StringIO()) as errores:
            procesador = ProcesadorTexto(
                rewrite_mode="formal", ollama_model="modelo-prueba",
                ollama_url=origen.url(), voice_commands=False)
            resultado = procesador.procesar_frase(f"ok {SENTINEL_TEXTO}")
        self.assertEqual(resultado.texto, f"De acuerdo {SENTINEL_TEXTO}")
        self.assertEqual(destino.registro, [])
        self.assertIn("fallback local", errores.getvalue())
        self.assertNotIn(SENTINEL_TEXTO, errores.getvalue())


class TransporteRechazaNoEspecificada(ConServidores):
    def test_transporte_no_conecta_a_la_direccion_de_escucha(self):
        # El servidor escucha sólo en 127.0.0.1; en Linux 0.0.0.0 llegaría a él.
        servidor = self.servidor("ollama")
        for host in ("0.0.0.0", "0"):
            with self.subTest(host=host), entorno_proxy(), \
                    self.assertRaises(OSError):
                transportar(servidor.url(host))
        self.assertEqual(servidor.registro, [])


class TransporteRechazaCredenciales(ConServidores):
    def test_url_con_credenciales_no_conecta(self):
        servidor = self.servidor("ollama")
        url = f"http://u:{SENTINEL_SECRETO}@127.0.0.1:{servidor.puerto}"
        with entorno_proxy(), self.assertRaises(OSError) as error:
            transportar(url)
        self.assertEqual(servidor.registro, [])
        self.assertNotIn(SENTINEL_SECRETO, str(error.exception))


class ConfiguracionSinFugas(unittest.TestCase):
    def test_url_redactada(self):
        casos = (
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            (f"https://u:{SENTINEL_SECRETO}@h.example/x",
             "https://***@h.example/x"),
            (f"http://{SENTINEL_SECRETO}@[::1]:1", "http://***@[::1]:1"),
            (f"http://h.example/?token={SENTINEL_SECRETO}#{SENTINEL_SECRETO}",
             "http://h.example/?***#***"),
        )
        for valor, esperado in casos:
            with self.subTest(valor=valor):
                self.assertEqual(Config.url_redactada(valor), esperado)
        self.assertEqual(Config.url_redactada("http://[::1"), "<URL inválida>")

    def test_error_de_validacion_no_incluye_credenciales(self):
        invalidas = (
            f"http://u:{SENTINEL_SECRETO}@host invalido",
            f"http://u:{SENTINEL_SECRETO}@h.example/?k={SENTINEL_SECRETO}",
            f"ftp://u:{SENTINEL_SECRETO}@h.example",
            f"http://u:{SENTINEL_SECRETO}@h.example:99999",
        )
        for url in invalidas:
            with self.subTest(url=url):
                with self.assertRaises(ErrorConfiguracion) as error:
                    Config(ollama_url=url).validate()
                self.assertIn("ollama_url", str(error.exception))
                self.assertNotIn(SENTINEL_SECRETO, str(error.exception))

    def test_inicio_con_url_invalida_no_filtra_credenciales(self):
        import parlar.__main__ as entrada

        cfg = Config(ollama_url=f"http://u:{SENTINEL_SECRETO}@host invalido")
        previo = sys.modules.pop("parlar.app", None)
        errores = io.StringIO()
        try:
            with (mock.patch.object(entrada.Config, "load", return_value=cfg),
                  mock.patch.object(sys, "argv", ["parlar"]),
                  contextlib.redirect_stderr(errores),
                  self.assertRaisesRegex(SystemExit, "2")):
                entrada.main()
            self.assertNotIn("parlar.app", sys.modules)
        finally:
            if previo is not None:
                sys.modules["parlar.app"] = previo
        self.assertIn("ollama_url", errores.getvalue())
        self.assertNotIn(SENTINEL_SECRETO, errores.getvalue())

    def test_mostrar_config_redacta_la_url(self):
        import parlar.__main__ as entrada

        cfg = Config(ollama_url=f"https://u:{SENTINEL_SECRETO}@h.example")
        previo = sys.modules.pop("parlar.app", None)
        salida = io.StringIO()
        try:
            with (mock.patch.object(entrada.Config, "load", return_value=cfg),
                  mock.patch.object(sys, "argv",
                                    ["parlar", "--mostrar-config"]),
                  contextlib.redirect_stdout(salida)):
                entrada.main()
            self.assertNotIn("parlar.app", sys.modules)
        finally:
            if previo is not None:
                sys.modules["parlar.app"] = previo
        datos = json.loads(salida.getvalue())
        self.assertEqual(datos["ollama_url"], "https://***@h.example")
        self.assertNotIn(SENTINEL_SECRETO, salida.getvalue())


class Compatibilidad(unittest.TestCase):
    def test_default_y_tipo_del_opt_in(self):
        self.assertIs(Config().ollama_allow_insecure_http, False)
        Config(ollama_allow_insecure_http=True).validate()
        with self.assertRaisesRegex(ErrorConfiguracion,
                                    "ollama_allow_insecure_http"):
            Config(ollama_allow_insecure_http="true").validate()

    def test_urls_existentes_siguen_validando(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:11434",
                    "http://192.168.1.10:11434", "https://ollama.example",
                    f"https://u:{SENTINEL_SECRETO}@ollama.example",
                    "http://[::1]:11434"):
            with self.subTest(url=url):
                Config(ollama_url=url).validate()

    def test_app_pasa_el_opt_in_al_procesador(self):
        import parlar.app as app_mod

        fuente = inspect.getsource(app_mod)
        self.assertIn(
            "ollama_permitir_http=cfg.ollama_allow_insecure_http", fuente)

    def test_procesador_conserva_firma_posicional(self):
        procesador = ProcesadorTexto(
            True, True, "formal", "modelo", "http://127.0.0.1:11434", False)
        self.assertFalse(procesador.ollama_permitir_http)


if __name__ == "__main__":
    unittest.main()

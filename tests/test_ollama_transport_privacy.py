"""Privacidad del límite de proceso usado por el transporte Ollama."""

import subprocess
import time
import unittest
from unittest import mock

import parlar.procesador_texto as procesador_mod


class ProcesoFalso:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = 0
        self.entrada = None
        self.timeout = None

    def communicate(self, entrada=None, timeout=None):
        self.entrada = entrada
        self.timeout = timeout
        return b"respuesta", b""

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class PrivacidadTransporteOllama(unittest.TestCase):
    def test_url_y_dictado_viajan_por_stdin_y_no_por_argv(self):
        url = "https://usuario:SENTINEL_SECRET@example.com"
        cuerpo = b'{"prompt":"SENTINEL_DICTADO"}'
        procesos = []

        def crear_proceso(args, **kwargs):
            proceso = ProcesoFalso(args, **kwargs)
            procesos.append(proceso)
            return proceso

        with mock.patch.object(
            procesador_mod.subprocess,
            "Popen",
            side_effect=crear_proceso,
        ):
            resultado = procesador_mod._ejecutar_ollama_aislado(
                url,
                cuerpo,
                time.monotonic() + 2.0,
            )

        self.assertEqual(resultado, "respuesta")
        self.assertEqual(len(procesos), 1)

        proceso = procesos[0]
        argv = "\0".join(str(valor) for valor in proceso.args)

        self.assertNotIn(url, argv)
        self.assertNotIn("SENTINEL_SECRET", argv)
        self.assertNotIn("SENTINEL_DICTADO", argv)

        self.assertEqual(
            proceso.entrada,
            url.encode("utf-8") + b"\n" + cuerpo,
        )

        self.assertEqual(proceso.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(proceso.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(proceso.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(proceso.kwargs.get("shell", False))
        self.assertGreater(proceso.timeout, 0)


if __name__ == "__main__":
    unittest.main()

"""Privacidad de logs y exclusividad de transcripts locales."""

import contextlib
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App
from parlar.inyector_salida import Inyector
from parlar.procesador_texto import ProcesadorTexto
from parlar.sesion import SalidaSesion, SesionNula, crear_salida_sesion


class PrivacidadLogs(unittest.TestCase):
    SECRETO = "clave ultrasecreta 9Z-ñ"

    def test_fallback_sin_herramienta_no_imprime_dictado(self):
        salida = io.StringIO()
        with (mock.patch("parlar.inyector_salida._cual", return_value=None),
              contextlib.redirect_stdout(salida),
              contextlib.redirect_stderr(salida)):
            inyector = Inyector(backend="clipboard", notify=False)
            self.assertFalse(inyector.escribir_texto(self.SECRETO))
        registro = salida.getvalue()
        self.assertNotIn(self.SECRETO, registro)
        self.assertIn(str(len(self.SECRETO)), registro)

    def test_fallo_de_backend_no_repite_argumento_ni_stderr_externo(self):
        salida = io.StringIO()
        resultado = SimpleNamespace(returncode=1, stderr=self.SECRETO.encode())
        with (mock.patch("parlar.inyector_salida.subprocess.run",
                         return_value=resultado),
              contextlib.redirect_stdout(salida),
              contextlib.redirect_stderr(salida)):
            Inyector(backend="xdotool", notify=False).escribir_texto(self.SECRETO)
        self.assertNotIn(self.SECRETO, salida.getvalue())

    def test_transcripcion_normal_no_aparece_en_stdout_ni_stderr(self):
        app = App.__new__(App)
        app.frases = SimpleNamespace(transcribir=lambda _audio: self.SECRETO)
        app.proc = ProcesadorTexto()
        app._estado_visual_si_vigente = lambda *_args: None
        emitidos = []
        app._emitir = lambda procesado, generacion: emitidos.append(
            (procesado.texto, generacion)
        )
        salida = io.StringIO()
        with (contextlib.redirect_stdout(salida),
              contextlib.redirect_stderr(salida)):
            app._atender_frase(np.zeros(1, dtype=np.float32), 4)
        self.assertEqual(emitidos, [(self.SECRETO, 4)])
        self.assertNotIn(self.SECRETO.lower(), salida.getvalue().lower())


class ArchivosSesion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directorio = Path(self.tmp.name) / "sesiones"

    def tearDown(self):
        self.tmp.cleanup()

    def test_desactivado_no_crea_directorio_ni_archivo(self):
        salida = crear_salida_sesion(False, self.directorio)
        self.assertIsInstance(salida, SesionNula)
        self.assertFalse(salida.escribir_texto("secreto"))
        self.assertFalse(self.directorio.exists())

    def test_permisos_privados_bajo_umask_cero(self):
        anterior = os.umask(0)
        try:
            salida = SalidaSesion(self.directorio)
        finally:
            os.umask(anterior)
        try:
            self.assertEqual(self.directorio.stat().st_mode & 0o777, 0o700)
            self.assertEqual(salida.ruta.stat().st_mode & 0o777, 0o600)
        finally:
            salida.cerrar()

    def test_colision_regenera_nombre_y_no_mezcla_contenido(self):
        reloj = mock.Mock()
        reloj.now.return_value = datetime(
            2026, 9, 11, 12, 34, 56, 123456, tzinfo=timezone.utc
        )
        with (mock.patch("parlar.sesion.datetime", reloj),
              mock.patch("parlar.sesion.secrets.token_hex",
                         side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])):
            primera = SalidaSesion(self.directorio)
            segunda = SalidaSesion(self.directorio)
        try:
            self.assertNotEqual(primera.ruta, segunda.ruta)
            primera.escribir_texto("solo primera")
            segunda.escribir_texto("solo segunda")
        finally:
            primera.cerrar()
            segunda.cerrar()
        self.assertEqual(primera.ruta.read_text(), "solo primera\n")
        self.assertEqual(segunda.ruta.read_text(), "solo segunda\n")

    def test_creacion_concurrente_es_exclusiva(self):
        def crear(indice):
            salida = SalidaSesion(self.directorio)
            salida.escribir_texto(f"sesion-{indice}")
            salida.cerrar()
            return salida.ruta

        reloj = mock.Mock()
        reloj.now.return_value = datetime(
            2026, 9, 11, 12, 34, 56, 123456, tzinfo=timezone.utc
        )
        with (mock.patch("parlar.sesion.datetime", reloj),
              contextlib.redirect_stdout(io.StringIO()),
              ThreadPoolExecutor(max_workers=8) as executor):
            rutas = list(executor.map(crear, range(32)))
        self.assertEqual(len(set(rutas)), 32)
        for indice, ruta in enumerate(rutas):
            self.assertEqual(ruta.read_text(), f"sesion-{indice}\n")

    def test_creaciones_rapidas_no_reutilizan_archivo(self):
        with contextlib.redirect_stdout(io.StringIO()):
            salidas = [SalidaSesion(self.directorio) for _ in range(24)]
        try:
            rutas = [salida.ruta for salida in salidas]
            self.assertEqual(len(set(rutas)), len(rutas))
        finally:
            for salida in salidas:
                salida.cerrar()


if __name__ == "__main__":
    unittest.main()

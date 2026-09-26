"""Contrato estricto y persistencia segura de la configuración."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parlar.config as config_mod
from parlar.config import Config, ErrorConfiguracion
from parlar.procesador_texto import ProcesadorTexto


class ConfigTemporal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "config" / "config.json"
        self.parche = mock.patch.object(config_mod, "CONFIG_FILE", self.ruta)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        self.tmp.cleanup()

    def escribir(self, valor):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps(valor), encoding="utf-8")

    def test_defaults_y_objeto_valido(self):
        self.assertEqual(Config.load(), Config())
        self.escribir({
            "model_size": "base",
            "device": "cpu",
            "compute_type": "int8",
            "frame_ms": 30,
            "mode": "streaming",
            "rewrite_mode": "formal",
            "injector": "clipboard",
            "futura": 7,
        })
        cfg = Config.load()
        self.assertEqual(cfg.model_size, "base")
        self.assertEqual(cfg.frame_ms, 30)
        self.assertEqual(cfg.mode, "streaming")
        self.assertEqual(cfg.extras, {"futura": 7})

    def test_config_legacy_adquiere_schema_sin_cambiar_hotkey(self):
        hotkey_legacy = "<ctrl>+<alt>+d"
        self.escribir({
            "hotkey_toggle": hotkey_legacy,
            "futura": {"preservar": True},
        })
        cfg = Config.load()
        self.assertEqual(cfg.schema_version, Config.SCHEMA_VERSION)
        self.assertEqual(cfg.hotkey_toggle, hotkey_legacy)
        self.assertEqual(cfg.extras, {"futura": {"preservar": True}})

        cfg.save()
        guardada = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(guardada["schema_version"], Config.SCHEMA_VERSION)
        self.assertEqual(guardada["hotkey_toggle"], hotkey_legacy)
        self.assertEqual(
            guardada["extras"], {"futura": {"preservar": True}})

    def test_schema_futuro_y_version_invalida_fallan_cerrado(self):
        for valor, patron in (
            (Config.SCHEMA_VERSION + 1, "versión más nueva"),
            (-1, "no puede ser negativo"),
            ("1", "debe ser entero"),
            (True, "debe ser entero"),
        ):
            with self.subTest(valor=valor):
                self.escribir({"schema_version": valor})
                with self.assertRaisesRegex(ErrorConfiguracion, patron):
                    Config.load()

    def test_todos_los_enums_conocidos_pasan(self):
        for campo, opciones in (
            ("device", Config.DISPOSITIVOS),
            ("compute_type", Config.COMPUTE_TYPES),
            ("mode", Config.MODOS),
            ("rewrite_mode", Config.REESCRITURAS),
            ("injector", Config.INYECTORES),
        ):
            for opcion in opciones:
                with self.subTest(campo=campo, opcion=opcion):
                    self.escribir({campo: opcion})
                    self.assertEqual(getattr(Config.load(), campo), opcion)

    def test_clave_de_metodo_no_sobrescribe_metodo(self):
        self.escribir({
            "load": "controlado",
            "save": "tampoco",
            "frame_samples": 99,
        })
        cfg = Config.load()
        self.assertTrue(callable(cfg.load))
        self.assertTrue(callable(cfg.save))
        self.assertEqual(cfg.frame_samples, 320)
        self.assertEqual(cfg.extras, {
            "load": "controlado",
            "save": "tampoco",
            "frame_samples": 99,
        })

    def test_matriz_invalida(self):
        casos = (
            ([], "raíz"),
            (None, "raíz"),
            ("texto", "raíz"),
            (42, "raíz"),
            ({"guardar_sesion": "false"}, "guardar_sesion"),
            ({"guardar_sesion": "true"}, "guardar_sesion"),
            ({"guardar_sesion": 0}, "guardar_sesion"),
            ({"guardar_sesion": 1}, "guardar_sesion"),
            ({"guardar_sesion": None}, "guardar_sesion"),
            ({"guardar_sesion": []}, "guardar_sesion"),
            ({"guardar_sesion": {}}, "guardar_sesion"),
            ({"beam_size": True}, "beam_size"),
            ({"beam_size": 0}, "beam_size"),
            ({"beam_size": -1}, "beam_size"),
            ({"beam_size": "5"}, "beam_size"),
            ({"beam_size": 5.0}, "beam_size"),
            ({"max_utterance_s": 30}, "max_utterance_s"),
            ({"max_utterance_s": 0.0}, "max_utterance_s"),
            ({"max_utterance_s": -1.0}, "max_utterance_s"),
            ({"max_utterance_s": float("nan")}, "max_utterance_s"),
            ({"max_utterance_s": float("inf")}, "max_utterance_s"),
            ({"max_utterance_s": "30.0"}, "max_utterance_s"),
            ({"sample_rate": 48000}, "sample_rate"),
            ({"frame_ms": 0}, "frame_ms"),
            ({"frame_ms": -20}, "frame_ms"),
            ({"frame_ms": 40}, "frame_ms"),
            ({"vad_aggressiveness": 4}, "vad_aggressiveness"),
            ({"silence_ms": 0}, "silence_ms"),
            ({"preroll_ms": -1}, "preroll_ms"),
            ({"min_speech_ms": 0}, "min_speech_ms"),
            ({"stream_interval_s": 0.0}, "stream_interval_s"),
            ({"stream_trim_s": float("-inf")}, "stream_trim_s"),
            ({"type_delay_ms": -1}, "type_delay_ms"),
            ({"mode": "frase"}, "mode"),
            ({"rewrite_mode": "creativo"}, "rewrite_mode"),
            ({"injector": "shell"}, "injector"),
            ({"preroll_ms": 100, "min_speech_ms": 200}, "preroll_ms"),
            ({"min_speech_ms": 2000, "max_utterance_s": 1.0},
             "min_speech_ms"),
            ({"extras": []}, "extras"),
        )
        for documento, fragmento in casos:
            with self.subTest(documento=documento):
                self.escribir(documento)
                with self.assertRaisesRegex(ErrorConfiguracion, fragmento):
                    Config.load()

    def test_json_malformado_falla_cerrado(self):
        self.ruta.parent.mkdir(parents=True)
        self.ruta.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ErrorConfiguracion, "no se pudo leer"):
            Config.load()

    def test_utf8_invalido_es_error_de_configuracion_sin_filtrar_contenido(self):
        self.ruta.parent.mkdir(parents=True)
        self.ruta.write_bytes(b"prefijo-secreto-\xff-sufijo")
        with self.assertRaises(ErrorConfiguracion) as contexto:
            Config.load()
        mensaje = str(contexto.exception)
        self.assertIn(str(self.ruta), mensaje)
        self.assertIn("UTF-8", mensaje)
        self.assertIn("byte 16", mensaje)
        self.assertNotIn("secreto", mensaje)

    def test_validate_cubre_mutaciones_en_memoria(self):
        cfg = Config()
        cfg.overlay = "sí"
        with self.assertRaisesRegex(ErrorConfiguracion, "overlay"):
            cfg.validate()

    def test_preroll_no_supera_max_utterance(self):
        with self.assertRaisesRegex(
                ErrorConfiguracion, "preroll_ms.*max_utterance_s"):
            Config(preroll_ms=300, max_utterance_s=0.2,
                   min_speech_ms=200).validate()
        Config(preroll_ms=200, max_utterance_s=0.2,
               min_speech_ms=200).validate()
        Config(preroll_ms=100, max_utterance_s=0.2,
               min_speech_ms=100).validate()
        Config().validate()

    def test_max_utterance_debe_admitir_al_menos_un_frame(self):
        with self.assertRaisesRegex(
                ErrorConfiguracion, "max_utterance_s.*frame_ms"):
            Config(frame_ms=20, preroll_ms=10, min_speech_ms=10,
                   max_utterance_s=0.015).validate()

    def test_matriz_ollama_url(self):
        validas = (
            "http://127.0.0.1:11434",
            "http://localhost:11434",
            "https://example.com",
            "http://192.168.1.10:11434",
        )
        invalidas = (
            ":", "foo", "localhost:11434", "://bad",
            "file:///tmp/x", "http://", "https://",
        )
        for url in validas:
            with self.subTest(url=url):
                Config(ollama_url=url).validate()
        for url in invalidas:
            with self.subTest(url=url):
                with self.assertRaisesRegex(ErrorConfiguracion, "ollama_url"):
                    Config(ollama_url=url).validate()

    def test_fallback_ollama_cubre_preparacion_transporte_y_resultado(self):
        casos = (
            ("preparacion",
             mock.patch("json.dumps",
                        side_effect=ValueError("request inválida"))),
            ("transporte",
             mock.patch(
                 "parlar.procesador_texto._ejecutar_ollama_aislado",
                 side_effect=OSError("offline"))),
            ("resultado",
             mock.patch(
                 "parlar.procesador_texto._ejecutar_ollama_aislado",
                 return_value=None)),
        )
        for nombre, parche in casos:
            with self.subTest(nombre=nombre), parche, \
                    contextlib.redirect_stderr(io.StringIO()) as diagnostico:
                procesador = ProcesadorTexto(
                    rewrite_mode="formal", ollama_model="modelo",
                    ollama_url="http://localhost:11434")
                self.assertEqual(
                    procesador.procesar_frase("ok").texto, "De acuerdo")
                self.assertIn("fallback local", diagnostico.getvalue())

        with contextlib.redirect_stderr(io.StringIO()) as diagnostico:
            mutado = ProcesadorTexto(
                rewrite_mode="formal", ollama_model="modelo", ollama_url=":")
            self.assertEqual(mutado.procesar_frase("ok").texto, "De acuerdo")
        self.assertIn("OSError", diagnostico.getvalue())

    def test_guardado_atomico_y_permisos_bajo_umask_cero(self):
        reemplazo_real = os.replace
        anterior = os.umask(0)
        try:
            with mock.patch.object(config_mod.os, "replace",
                                   wraps=reemplazo_real) as reemplazo:
                Config(mode="streaming", extras={"futura": 9}).save()
        finally:
            os.umask(anterior)
        reemplazo.assert_called_once()
        self.assertEqual(self.ruta.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ruta.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(self.ruta.read_text())["mode"], "streaming")
        self.assertEqual(Config.load().extras, {"futura": 9})
        self.assertEqual(list(self.ruta.parent.glob(".*.tmp-*")), [])

    def test_fallo_de_replace_preserva_archivo_anterior(self):
        self.ruta.parent.mkdir(parents=True)
        self.ruta.write_text('{"mode": "utterance"}\n', encoding="utf-8")
        anterior = self.ruta.read_bytes()
        with (mock.patch.object(config_mod.os, "replace",
                                side_effect=OSError("boom")),
              self.assertRaises(OSError)):
            Config(mode="streaming").save()
        self.assertEqual(self.ruta.read_bytes(), anterior)
        self.assertEqual(list(self.ruta.parent.glob(".*.tmp-*")), [])

    def test_fallo_antes_de_replace_preserva_archivo_anterior(self):
        self.ruta.parent.mkdir(parents=True)
        self.ruta.write_text('{"mode": "utterance"}\n', encoding="utf-8")
        anterior = self.ruta.read_bytes()
        with (mock.patch.object(config_mod.os, "fdopen",
                                side_effect=OSError("fallo de escritura")),
              self.assertRaises(OSError)):
            Config(mode="streaming").save()
        self.assertEqual(self.ruta.read_bytes(), anterior)
        self.assertEqual(list(self.ruta.parent.glob(".*.tmp-*")), [])


class InicioSeguro(unittest.TestCase):
    def test_help_no_carga_config_ni_importa_app(self):
        import parlar.__main__ as entrada

        previo = sys.modules.pop("parlar.app", None)
        salida = io.StringIO()
        try:
            with (mock.patch.object(
                    entrada.Config, "load",
                    side_effect=AssertionError("no cargar config")) as carga,
                  mock.patch.object(sys, "argv", ["parlar", "--help"]),
                  contextlib.redirect_stdout(salida),
                  self.assertRaises(SystemExit) as salida_cli):
                entrada.main()
            self.assertEqual(salida_cli.exception.code, 0)
            carga.assert_not_called()
            self.assertNotIn("parlar.app", sys.modules)
            self.assertIn("usage: parlar", salida.getvalue())
        finally:
            if previo is not None:
                sys.modules["parlar.app"] = previo

    def test_config_invalida_sale_antes_de_importar_app(self):
        import parlar.__main__ as entrada

        previo = sys.modules.pop("parlar.app", None)
        try:
            with (mock.patch.object(entrada.Config, "load",
                                    side_effect=ErrorConfiguracion("rota")),
                  mock.patch.object(sys, "argv", ["parlar"]),
                  self.assertRaisesRegex(SystemExit, "2")):
                entrada.main()
            self.assertNotIn("parlar.app", sys.modules)
        finally:
            if previo is not None:
                sys.modules["parlar.app"] = previo

    def test_app_valida_antes_de_crear_recursos(self):
        from parlar.app import App

        cfg = Config(sample_rate=48000)
        with (mock.patch("parlar.app.MotorWhisper") as motor,
              self.assertRaisesRegex(ErrorConfiguracion, "sample_rate")):
            App(cfg)
        motor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

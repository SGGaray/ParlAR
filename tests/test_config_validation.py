"""Contrato estricto y persistencia segura de la configuración."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parlar.config as config_mod
from parlar.config import Config, ErrorConfiguracion


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

    def test_validate_cubre_mutaciones_en_memoria(self):
        cfg = Config()
        cfg.overlay = "sí"
        with self.assertRaisesRegex(ErrorConfiguracion, "overlay"):
            cfg.validate()

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

"""Contrato del backend de Settings, aislado de UI y recursos pesados."""

import dataclasses
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import parlar.config as config_mod
from parlar.config import Config, ErrorConfiguracion
from parlar.settings_backend import (
    ErrorDispositivosAudio,
    SettingsSnapshot,
    construir_configuracion_candidata,
    listar_dispositivos_entrada,
    normalizar_dispositivos_entrada,
    obtener_capacidades,
    persistir_configuracion,
    requiere_reinicio,
    snapshot_configuracion,
)


class PruebasSettingsBackend(unittest.TestCase):
    def test_snapshot_inmutable_expone_solo_opciones_publicas(self):
        cfg = Config(
            model_size="base",
            context_terms=["COBIT", "OWASP"],
            ollama_url="https://secreto.example",
        )

        snapshot = snapshot_configuracion(cfg)

        self.assertIsInstance(snapshot, SettingsSnapshot)
        self.assertEqual(snapshot.model_size, "base")
        self.assertEqual(snapshot.context_terms, ("COBIT", "OWASP"))
        campos = {campo.name for campo in dataclasses.fields(snapshot)}
        self.assertNotIn("ollama_url", campos)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.model_size = "small"

    def test_candidata_valida_no_muta_original_y_preserva_campos_no_publicos(self):
        original = Config(
            beam_size=7,
            context_terms=["Original"],
            extras={"futura": {"valor": 1}},
        )
        editado = dataclasses.replace(
            snapshot_configuracion(original),
            model_size="medium",
            context_terms=("Nuevo",),
        )

        candidata = construir_configuracion_candidata(original, editado)

        self.assertEqual(original.model_size, "small")
        self.assertEqual(original.context_terms, ["Original"])
        self.assertEqual(candidata.model_size, "medium")
        self.assertEqual(candidata.context_terms, ["Nuevo"])
        self.assertEqual(candidata.beam_size, 7)
        self.assertEqual(candidata.extras, {"futura": {"valor": 1}})
        candidata.extras["futura"]["valor"] = 2
        self.assertEqual(original.extras["futura"]["valor"], 1)

    def test_validacion_delegada_a_config(self):
        original = Config()
        valida = dataclasses.replace(snapshot_configuracion(original), device="cpu")
        candidata = construir_configuracion_candidata(original, valida)
        self.assertEqual(candidata.device, "cpu")

        invalida = dataclasses.replace(valida, device="tpu")
        with self.assertRaisesRegex(ErrorConfiguracion, "device"):
            construir_configuracion_candidata(original, invalida)

    def test_persistencia_aislada_usa_config_save_y_requiere_reinicio(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "parlar" / "config.json"
            original = Config(beam_size=7, extras={"futura": True})
            editado = dataclasses.replace(
                snapshot_configuracion(original),
                mode="streaming",
                guardar_sesion=True,
            )

            with mock.patch.object(config_mod, "CONFIG_FILE", ruta):
                resultado = persistir_configuracion(original, editado)
                cargada = Config.load()

            self.assertTrue(resultado.requires_restart)
            self.assertEqual(resultado.snapshot, editado)
            self.assertEqual(original.mode, "utterance")
            self.assertEqual(cargada.mode, "streaming")
            self.assertTrue(cargada.guardar_sesion)
            self.assertEqual(cargada.beam_size, 7)
            self.assertEqual(cargada.extras, {"futura": True})
            self.assertEqual(json.loads(ruta.read_text())["mode"], "streaming")

    def test_requires_restart_solo_si_hay_cambios(self):
        actual = snapshot_configuracion(Config())
        self.assertFalse(requiere_reinicio(actual, actual))
        self.assertTrue(requiere_reinicio(
            actual,
            dataclasses.replace(actual, overlay=False),
        ))

    def test_normaliza_y_filtra_dispositivos_de_entrada_sinteticos(self):
        dispositivos = [
            {"name": "Mic USB", "max_input_channels": 2},
            {"name": "Salida HDMI", "max_input_channels": 0},
            {"name": "Mic interno", "max_input_channels": 1},
        ]

        resultado = normalizar_dispositivos_entrada(dispositivos, 2)

        self.assertEqual([item.indice for item in resultado], [0, 2])
        self.assertEqual(
            [item.nombre for item in resultado],
            ["Mic USB", "Mic interno"],
        )
        self.assertEqual([item.canales_entrada for item in resultado], [2, 1])
        self.assertEqual([item.predeterminado for item in resultado], [False, True])

    def test_listado_usa_sounddevice_inyectado_sin_abrir_stream(self):
        modulo = SimpleNamespace(
            query_devices=mock.Mock(return_value=[
                {"name": "Mic", "max_input_channels": 1},
            ]),
            default=SimpleNamespace(device=(0, 4)),
            RawInputStream=mock.Mock(side_effect=AssertionError("no abrir stream")),
        )

        resultado = listar_dispositivos_entrada(modulo)

        self.assertEqual(len(resultado), 1)
        self.assertTrue(resultado[0].predeterminado)
        modulo.query_devices.assert_called_once_with()
        modulo.RawInputStream.assert_not_called()

    def test_default_de_entrada_acepta_indice_escalar(self):
        modulo = SimpleNamespace(
            query_devices=mock.Mock(return_value=[
                {"name": "Mic", "max_input_channels": 1},
            ]),
            default=SimpleNamespace(device=0),
        )

        resultado = listar_dispositivos_entrada(modulo)

        self.assertEqual(len(resultado), 1)
        self.assertTrue(resultado[0].predeterminado)

    def test_fallo_de_sounddevice_es_explicito(self):
        modulo = SimpleNamespace(
            query_devices=mock.Mock(side_effect=OSError("PortAudio caído")),
            default=SimpleNamespace(device=(0, 1)),
        )

        with self.assertRaisesRegex(
                ErrorDispositivosAudio, "enumerar dispositivos de entrada"):
            listar_dispositivos_entrada(modulo)

    def test_capabilities_no_detecta_cuda_ni_importa_runtime(self):
        capacidades = obtener_capacidades({"XDG_SESSION_TYPE": "wayland"})
        self.assertEqual(capacidades.session_type, "wayland")
        self.assertEqual(capacidades.devices, tuple(sorted(Config.DISPOSITIVOS)))

        codigo = """
import sys
import parlar.settings_backend
prohibidos = {
    'parlar.app', 'sounddevice', 'faster_whisper', 'ctranslate2', 'tkinter'
}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


if __name__ == "__main__":
    unittest.main()

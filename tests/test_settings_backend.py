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
    DispositivoEntrada,
    ErrorDispositivosAudio,
    ResolucionEntrada,
    SettingsSnapshot,
    construir_configuracion_candidata,
    crear_identidad_entrada,
    listar_dispositivos_entrada,
    normalizar_dispositivos_entrada,
    obtener_capacidades,
    persistir_configuracion,
    resolver_dispositivo_entrada,
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
        self.assertEqual(snapshot.audio_input_device, "default")
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
            audio_input_device="audio-input:ALSA:Mic%20USB",
        )

        candidata = construir_configuracion_candidata(original, editado)

        self.assertEqual(original.model_size, "small")
        self.assertEqual(original.context_terms, ["Original"])
        self.assertEqual(candidata.model_size, "medium")
        self.assertEqual(candidata.context_terms, ["Nuevo"])
        self.assertEqual(
            candidata.audio_input_device,
            "audio-input:ALSA:Mic%20USB",
        )
        self.assertEqual(original.audio_input_device, "default")
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
            {"name": "Mic USB", "max_input_channels": 2, "hostapi": 4},
            {"name": "Salida HDMI", "max_input_channels": 0, "hostapi": 4},
            {"name": "Mic interno", "max_input_channels": 1, "hostapi": 8},
        ]

        resultado = normalizar_dispositivos_entrada(
            dispositivos,
            2,
            {4: "ALSA", 8: "PipeWire"},
        )

        self.assertEqual([item.indice for item in resultado], [0, 2])
        self.assertEqual(
            [item.nombre for item in resultado],
            ["Mic USB", "Mic interno"],
        )
        self.assertEqual([item.canales_entrada for item in resultado], [2, 1])
        self.assertEqual([item.predeterminado for item in resultado], [False, True])
        self.assertEqual(
            [item.host_api for item in resultado],
            ["ALSA", "PipeWire"],
        )
        self.assertEqual(
            [item.identidad for item in resultado],
            [
                "audio-input:ALSA:Mic%20USB",
                "audio-input:PipeWire:Mic%20interno",
            ],
        )

    def test_listado_usa_sounddevice_inyectado_sin_abrir_stream(self):
        modulo = SimpleNamespace(
            query_devices=mock.Mock(return_value=[
                {"name": "Mic", "max_input_channels": 1, "hostapi": 0},
            ]),
            query_hostapis=mock.Mock(return_value=[{"name": "ALSA"}]),
            default=SimpleNamespace(device=(0, 4)),
            RawInputStream=mock.Mock(side_effect=AssertionError("no abrir stream")),
        )

        resultado = listar_dispositivos_entrada(modulo)

        self.assertEqual(len(resultado), 1)
        self.assertTrue(resultado[0].predeterminado)
        self.assertEqual(resultado[0].host_api, "ALSA")
        self.assertEqual(resultado[0].identidad, "audio-input:ALSA:Mic")
        modulo.query_devices.assert_called_once_with()
        modulo.query_hostapis.assert_called_once_with()
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

    @staticmethod
    def _inventario(*datos):
        return tuple(
            DispositivoEntrada(
                indice=indice,
                nombre=nombre,
                canales_entrada=1,
                predeterminado=predeterminado,
                host_api=host_api,
                identidad=crear_identidad_entrada(nombre, host_api),
            )
            for indice, nombre, host_api, predeterminado in datos
        )

    def test_resuelve_seleccion_default(self):
        inventario = self._inventario(
            (3, "Mic interno", "ALSA", True),
            (8, "Mic USB", "ALSA", False),
        )

        resultado = resolver_dispositivo_entrada("default", inventario)

        self.assertIsInstance(resultado, ResolucionEntrada)
        self.assertEqual(resultado.indice, 3)
        self.assertFalse(resultado.usando_fallback)
        self.assertIsNone(resultado.motivo)

    def test_resuelve_dispositivo_existente_por_identidad(self):
        inventario = self._inventario(
            (3, "Mic interno", "ALSA", True),
            (8, "Mic USB", "PipeWire", False),
        )
        seleccion = crear_identidad_entrada("Mic USB", "PipeWire")

        resultado = resolver_dispositivo_entrada(seleccion, inventario)

        self.assertEqual(resultado.indice, 8)
        self.assertEqual(resultado.identidad_resuelta, seleccion)
        self.assertFalse(resultado.usando_fallback)
        self.assertIsNone(resultado.motivo)

    def test_dispositivo_ausente_usa_default_sin_mutar_seleccion(self):
        seleccion = crear_identidad_entrada("Mic USB", "ALSA")
        inventario = self._inventario(
            (2, "Mic interno", "PipeWire", True),
        )

        resultado = resolver_dispositivo_entrada(seleccion, inventario)

        self.assertEqual(resultado.indice, 2)
        self.assertEqual(resultado.seleccion_solicitada, seleccion)
        self.assertTrue(resultado.usando_fallback)
        self.assertEqual(resultado.motivo, "seleccion_ausente")

    def test_nombres_duplicados_se_desambiguan_por_host_api(self):
        inventario = self._inventario(
            (1, "Mic USB", "ALSA", True),
            (9, "Mic USB", "PipeWire", False),
        )
        seleccion = crear_identidad_entrada("Mic USB", "PipeWire")

        resultado = resolver_dispositivo_entrada(seleccion, inventario)

        self.assertEqual(resultado.indice, 9)
        self.assertFalse(resultado.usando_fallback)

    def test_identidad_duplicada_no_se_resuelve_silenciosamente(self):
        inventario = self._inventario(
            (1, "Mic USB", "ALSA", False),
            (7, "Mic USB", "ALSA", False),
            (4, "Mic interno", "PipeWire", True),
        )
        seleccion = crear_identidad_entrada("Mic USB", "ALSA")

        resultado = resolver_dispositivo_entrada(seleccion, inventario)

        self.assertEqual(resultado.indice, 4)
        self.assertTrue(resultado.usando_fallback)
        self.assertEqual(resultado.motivo, "seleccion_ambigua")

    def test_inventario_vacio_es_explicito(self):
        default = resolver_dispositivo_entrada("default", ())
        ausente = resolver_dispositivo_entrada(
            crear_identidad_entrada("Mic", "ALSA"), ())

        self.assertIsNone(default.indice)
        self.assertEqual(default.motivo, "inventario_vacio")
        self.assertFalse(default.usando_fallback)
        self.assertIsNone(ausente.indice)
        self.assertEqual(
            ausente.motivo,
            "seleccion_ausente_inventario_vacio",
        )

    def test_seleccion_ausente_sin_default_no_inventa_dispositivo(self):
        seleccion = crear_identidad_entrada("Mic USB", "ALSA")
        inventario = self._inventario(
            (5, "Mic interno", "PipeWire", False),
        )

        resultado = resolver_dispositivo_entrada(
            seleccion,
            inventario,
        )

        self.assertIsNone(resultado.indice)
        self.assertIsNone(resultado.dispositivo)
        self.assertIsNone(resultado.identidad_resuelta)
        self.assertEqual(
            resultado.seleccion_solicitada,
            seleccion,
        )
        self.assertFalse(resultado.usando_fallback)
        self.assertEqual(
            resultado.motivo,
            "seleccion_ausente_default_no_disponible",
        )

    def test_cambio_de_indice_conserva_dispositivo_logico(self):
        seleccion = crear_identidad_entrada("Mic USB", "ALSA")
        primero = self._inventario((2, "Mic USB", "ALSA", False))
        segundo = self._inventario((11, "Mic USB", "ALSA", False))

        self.assertEqual(
            resolver_dispositivo_entrada(seleccion, primero).indice,
            2,
        )
        self.assertEqual(
            resolver_dispositivo_entrada(seleccion, segundo).indice,
            11,
        )

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

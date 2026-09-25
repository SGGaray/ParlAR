import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parlar.__main__ as entrada
import parlar.config as config_mod
from parlar.config import Config, ErrorConfiguracion


class PruebasContextoConfig(unittest.TestCase):
    def test_default_no_agrega_contexto(self):
        cfg = Config()

        self.assertEqual(cfg.context_terms, [])
        self.assertEqual(cfg.construir_contexto_stt(), "")

    def test_normaliza_y_deduplica(self):
        cfg = Config(
            context_terms=[
                "  COBIT  ",
                "cobit",
                "Acme   Corporation",
                "OWASP",
            ]
        )

        cfg.validate()

        self.assertEqual(
            cfg.construir_contexto_stt(),
            "Vocabulario relevante: "
            "COBIT, Acme Corporation, OWASP.",
        )

    def test_rechaza_elementos_invalidos(self):
        casos = (
            ("no-lista", "context_terms"),
            ([123], r"context_terms\[0\]"),
            (["   "], r"context_terms\[0\]"),
            (["A\nB"], "saltos de línea"),
            (["x" * 81], "80 caracteres"),
            (["x"] * 51, "50 términos"),
            (["x" * 50] * 41, "2000 caracteres"),
        )

        for valor, mensaje in casos:
            with self.subTest(valor=valor):
                with self.assertRaisesRegex(
                    ErrorConfiguracion,
                    mensaje,
                ):
                    Config(context_terms=valor).validate()

    def test_persistencia_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "parlar" / "config.json"

            with mock.patch.object(
                config_mod,
                "CONFIG_FILE",
                ruta,
            ):
                Config(
                    context_terms=[
                        "COBIT",
                        "Acme Corporation",
                    ]
                ).save()

                cargada = Config.load()

        self.assertEqual(
            cargada.context_terms,
            ["COBIT", "Acme Corporation"],
        )

    def test_cli_terminos_repetibles(self):
        parser = entrada._crear_parser(Config())

        args = parser.parse_args([
            "--context-term",
            "COBIT",
            "--termino-contexto",
            "Acme Corporation",
        ])

        self.assertEqual(
            args.terminos_contexto,
            ["COBIT", "Acme Corporation"],
        )
        self.assertFalse(args.sin_contexto)

    def test_cli_puede_vaciar_contexto(self):
        parser = entrada._crear_parser(
            Config(context_terms=["COBIT"])
        )

        args = parser.parse_args(["--sin-contexto"])

        self.assertTrue(args.sin_contexto)
        self.assertIsNone(args.terminos_contexto)

    def test_app_pasa_contexto_al_motor(self):
        from parlar.app import App

        cfg = Config(
            context_terms=[
                "  COBIT  ",
                "cobit",
                "OWASP",
            ],
            overlay=False,
            notify=False,
        )

        guardia = object()
        with (
            mock.patch("parlar.app.MotorWhisper") as motor,
            mock.patch("parlar.app.TranscriptorFrase"),
            mock.patch("parlar.app.TranscriptorStreaming"),
            mock.patch("parlar.app.ProcesadorTexto"),
            mock.patch("parlar.coordinador_salida.Inyector"),
            mock.patch("parlar.coordinador_salida.crear_cliente"),
            mock.patch("parlar.coordinador_salida.crear_salida_sesion"),
            mock.patch("parlar.app.CapturadorMic"),
            mock.patch("parlar.app.crear_ui"),
            mock.patch("parlar.app.ServidorControl") as control,
            mock.patch("parlar.app.DaemonAtajos"),
        ):
            App(cfg, guardia_instancia=guardia)

        motor.assert_called_once_with(
            "small",
            "auto",
            "auto",
            "es",
            5,
            initial_prompt=(
                "Vocabulario relevante: COBIT, OWASP."
            ),
        )
        control.assert_called_once_with(
            mock.ANY, guardia_instancia=guardia)


if __name__ == "__main__":
    unittest.main()

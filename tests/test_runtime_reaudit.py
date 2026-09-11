"""Regresiones de runtime: apertura, discontinuidades, health y cleanup."""

import contextlib
import io
import struct
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App, EstadoApp
from parlar.capturador_audio import CapturadorMic, Segmentador
from parlar.config import Config
from parlar.motor_transcripcion import TranscriptorStreaming
from tests.test_lifecycle import (
    FabricaSegmentador,
    FrasesFalsas,
    MicFalso,
    ProcesadorFalso,
    SalidaFalsa,
    StreamingFalso,
)


class MicDuranteStart(MicFalso):
    """Entrega una frase, pero no resuelve start hasta recibir permiso."""

    def __init__(self, *, fallar=False):
        super().__init__()
        self.fallar = fallar
        self.worker_leyo = threading.Event()
        self.resolver_start = threading.Event()

    def iniciar(self, generacion):
        self.inicios += 1
        self.generacion = generacion
        self.enviar(7)
        self.enviar(0)
        self.inicio_entrado.set()
        if not self.worker_leyo.wait(2):
            raise TimeoutError("el worker no leyó durante start")
        if not self.resolver_start.wait(2):
            raise TimeoutError("start falso no resuelto")
        if self.fallar:
            raise OSError("start falló después del callback")
        return True

    def leer_frame(self, timeout=0.05):
        item = super().leer_frame(timeout)
        if item is not None:
            self.worker_leyo.set()
        return item


class FrasesGuionadas:
    def __init__(self, resultados):
        self.resultados = list(resultados)
        self.llamadas = 0
        self.cv = threading.Condition()

    def transcribir(self, audio):
        with self.cv:
            indice = self.llamadas
            self.llamadas += 1
            self.cv.notify_all()
        resultado = self.resultados[min(indice, len(self.resultados) - 1)]
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    def esperar_llamadas(self, cantidad, timeout=2):
        with self.cv:
            return self.cv.wait_for(lambda: self.llamadas >= cantidad, timeout)


class VADGuionado:
    def __init__(self, resultados):
        self.resultados = list(resultados)
        self.llamadas = 0
        self.cv = threading.Condition()

    def is_speech(self, frame, sample_rate):
        with self.cv:
            indice = self.llamadas
            self.llamadas += 1
            self.cv.notify_all()
        resultado = self.resultados[min(indice, len(self.resultados) - 1)]
        if isinstance(resultado, BaseException):
            raise resultado
        return resultado

    def esperar_llamadas(self, cantidad, timeout=2):
        with self.cv:
            return self.cv.wait_for(lambda: self.llamadas >= cantidad, timeout)


class MotorStreamingGuionado:
    def __init__(self):
        self.llamadas = 0

    def decodificar(self, audio, word_timestamps=False, beam_size=None):
        self.llamadas += 1
        if self.llamadas == 1:
            raise OSError("fallo streaming")
        return []


class MicWorkerBloqueado(MicFalso):
    def __init__(self):
        super().__init__()
        self.worker_entro = threading.Event()
        self.liberar_worker = threading.Event()

    def leer_frame(self, timeout=0.05):
        self.worker_entro.set()
        if not self.liberar_worker.wait(3):
            raise TimeoutError("worker falso no liberado")
        return super().leer_frame(timeout=0)


class RecursoRastreado:
    _listener = None

    def __init__(self, *, fallar_inicio=False, fallar_cierre=False):
        self.fallar_inicio = fallar_inicio
        self.fallar_cierre = fallar_cierre
        self.inicio_intentado = threading.Event()
        self.cierre_intentado = threading.Event()

    def iniciar(self):
        self.inicio_intentado.set()
        if self.fallar_inicio:
            raise RuntimeError("startup deliberado")
        return True

    def detener(self):
        self.cierre_intentado.set()
        if self.fallar_cierre:
            raise RuntimeError("cleanup deliberado")


class UIRastreada:
    def __init__(self, *, fallar_entrada=False, fallar_cierre=False):
        self.fallar_entrada = fallar_entrada
        self.fallar_cierre = fallar_cierre
        self.entrada_intentada = threading.Event()
        self.cierre_intentado = threading.Event()

    def ejecutar(self):
        self.entrada_intentada.set()
        if self.fallar_entrada:
            raise RuntimeError("entrada UI deliberada")

    def fijar_estado(self, estado):
        pass

    def cerrar(self):
        self.cierre_intentado.set()
        if self.fallar_cierre:
            raise RuntimeError("cleanup UI deliberado")


class SalidaRastreada(SalidaFalsa):
    def __init__(self, *, fallar_cierre=False):
        super().__init__()
        self.fallar_cierre = fallar_cierre
        self.cierre_intentado = threading.Event()

    def cerrar(self):
        self.cierre_intentado.set()
        if self.fallar_cierre:
            raise RuntimeError("cleanup salida deliberado")
        super().cerrar()


class MicRastreado(MicFalso):
    def __init__(self, *, fallar_cierre=False):
        super().__init__()
        self.fallar_cierre = fallar_cierre
        self.cierre_intentado = threading.Event()

    def detener(self, *, vaciar=True):
        self.cierre_intentado.set()
        if self.fallar_cierre:
            raise RuntimeError("cleanup mic deliberado")
        return super().detener(vaciar=vaciar)


def crear_app_runtime(*, mic=None, frases=None, vad=None, recursos=None):
    cfg = Config(overlay=False, notify=False)
    recursos = recursos or {}
    mic = mic or MicFalso()
    frases = frases or FrasesFalsas()
    streaming = StreamingFalso()
    inyector = recursos.get("inyector", SalidaRastreada())
    guionar = recursos.get("guionar", SalidaRastreada())
    sesion = recursos.get("sesion", SalidaRastreada())
    control = recursos.get("control", RecursoRastreado())
    atajos = recursos.get("hotkeys", RecursoRastreado())
    ui = recursos.get("ui", UIRastreada())
    fabrica = FabricaSegmentador()
    app = App(
        cfg, frases=frases, streaming=streaming, proc=ProcesadorFalso(),
        inyector=inyector, guionar=guionar, sesion=sesion, mic=mic,
        ui=ui, control=control, atajos=atajos,
        vad_factory=(lambda *_: vad or object()),
        segmentador_factory=Segmentador if vad is not None else fabrica,
    )
    return app, mic, frases, sesion, {
        "control": control, "hotkeys": atajos, "ui": ui,
        "inyector": inyector, "guionar": guionar, "sesion": sesion,
    }


def esperar_app(app, predicado, timeout=2):
    with app._estado_cv:
        return app._estado_cv.wait_for(predicado, timeout)


class PruebasStarting(unittest.TestCase):
    def test_audio_durante_start_espera_y_se_procesa_una_vez(self):
        mic = MicDuranteStart()
        app, _, frases, sesion, _ = crear_app_runtime(mic=mic)
        hilo = threading.Thread(target=app.iniciar_grabacion)
        hilo.start()
        self.assertTrue(mic.inicio_entrado.wait(2))
        self.assertTrue(mic.worker_leyo.wait(2))
        self.assertEqual(app.estado, EstadoApp.STARTING)
        self.assertFalse(frases.inferencia_iniciada.is_set())
        self.assertEqual(sesion.textos, [])

        mic.resolver_start.set()
        hilo.join(2)
        self.assertFalse(hilo.is_alive())
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(frases.audios, [(7,)])
        self.assertEqual(sesion.textos, ["U:7"])
        app.salir()

    def test_start_falla_despues_de_audio_y_descarta_generacion(self):
        mic = MicDuranteStart(fallar=True)
        app, _, frases, sesion, _ = crear_app_runtime(mic=mic)
        resultados = []
        hilo = threading.Thread(
            target=lambda: resultados.append(app.iniciar_grabacion()))
        hilo.start()
        self.assertTrue(mic.inicio_entrado.wait(2))
        self.assertTrue(mic.worker_leyo.wait(2))
        mic.resolver_start.set()
        hilo.join(2)

        self.assertEqual(resultados, [False])
        self.assertEqual(app.estado, EstadoApp.ERROR)
        self.assertIsNone(app.generacion_activa)
        self.assertFalse(frases.inferencia_iniciada.is_set())
        self.assertEqual(sesion.textos, [])
        self.assertTrue(mic.q.empty())
        app.salir()


class PruebasOverflowDispositivo(unittest.TestCase):
    @staticmethod
    def activar():
        mic = CapturadorMic(16000, 2)
        with mic._callback_lock:
            mic._preparar_generacion(1)
        return mic

    def test_overflow_tiene_metrica_separada_y_frontera(self):
        mic = self.activar()
        app, _, _, _, _ = crear_app_runtime(mic=mic)
        estado_overflow = SimpleNamespace(input_overflow=True)
        mic._callback(1, struct.pack("<hh", 4, 5), 2, None, estado_overflow)
        antes = mic.estado_captura()
        self.assertIn("audio=degradado", app._respuesta_estado())
        item = mic.leer_frame(0)
        despues = mic.estado_captura()

        self.assertEqual(antes.device_overflows, 1)
        self.assertEqual(antes.frames_descartados, 0)
        self.assertTrue(antes.degradada)
        self.assertTrue(item.discontinuidad_antes)
        self.assertEqual(despues.discontinuidades, 1)
        self.assertFalse(despues.degradada)
        self.assertIn("audio=recuperado-con-perdida", app._respuesta_estado())
        self.assertIn("drops=0 device_overflows=1", app._respuesta_estado())
        app.salir()

    def test_callback_no_imprime_por_overflows_repetidos(self):
        mic = self.activar()
        errores = io.StringIO()
        with contextlib.redirect_stderr(errores):
            for valor in (1, 2):
                mic._callback(
                    1, struct.pack("<hh", valor, valor), 2, None,
                    SimpleNamespace(input_overflow=True),
                )
        self.assertEqual(errores.getvalue(), "")
        self.assertEqual(mic.estado_captura().device_overflows, 2)

    def test_overflow_invalida_resto_parcial_previo(self):
        mic = self.activar()
        mic._callback(1, struct.pack("<h", 1), 1, None, None)
        mic._callback(
            1, struct.pack("<h", 2), 1, None,
            SimpleNamespace(input_overflow=True),
        )
        self.assertIsNone(mic.leer_frame(0))
        mic._callback(1, struct.pack("<h", 3), 1, None, None)
        item = mic.leer_frame(0)
        self.assertEqual(struct.unpack("<hh", item.audio), (2, 3))
        self.assertTrue(item.discontinuidad_antes)


class PruebasInicioCaptura(unittest.TestCase):
    def test_import_fallido_revierte_notifica_y_permite_reintento(self):
        import_real = __import__
        for tipo_error in (ImportError, OSError):
            with self.subTest(tipo=tipo_error.__name__):
                mic = CapturadorMic(16000, 320)

                def importar(nombre, *args, **kwargs):
                    if nombre == "sounddevice":
                        raise tipo_error("backend no disponible")
                    return import_real(nombre, *args, **kwargs)

                with mock.patch("builtins.__import__", side_effect=importar):
                    with self.assertRaises(tipo_error):
                        mic.iniciar(1)
                self.assertEqual(mic._estado, "idle")

                detenido = threading.Event()
                hilo = threading.Thread(
                    target=lambda: (mic.detener(), detenido.set()))
                hilo.start()
                hilo.join(2)
                self.assertTrue(detenido.is_set())

                stream = mock.Mock()
                modulo = SimpleNamespace(
                    RawInputStream=mock.Mock(return_value=stream))
                with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
                    self.assertTrue(mic.iniciar(2))
                    mic.detener()
                stream.close.assert_called_once()


class PruebasOwnershipApp(unittest.TestCase):
    def _recursos(self, fallo=None):
        return {
            "control": RecursoRastreado(
                fallar_inicio=fallo == "control",
                fallar_cierre=fallo == "control_close"),
            "hotkeys": RecursoRastreado(
                fallar_inicio=fallo == "hotkeys",
                fallar_cierre=fallo == "hotkeys_close"),
            "ui": UIRastreada(
                fallar_entrada=fallo == "ui",
                fallar_cierre=fallo == "ui_close"),
            "inyector": SalidaRastreada(fallar_cierre=fallo == "inyector"),
            "guionar": SalidaRastreada(fallar_cierre=fallo == "guionar"),
            "sesion": SalidaRastreada(fallar_cierre=fallo == "sesion"),
        }

    def _assert_cleanup_completo(self, app, mic, recursos):
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertTrue(app._shutdown_completo.is_set())
        self.assertTrue(mic.cierre_intentado.is_set())
        self.assertTrue(recursos["control"].cierre_intentado.is_set())
        self.assertTrue(recursos["hotkeys"].cierre_intentado.is_set())
        for nombre in ("inyector", "guionar", "sesion", "ui"):
            self.assertTrue(recursos[nombre].cierre_intentado.is_set(), nombre)
        app.salir()  # el segundo cierre debe retornar inmediatamente

    def test_matriz_startup_adquiere_desde_el_primer_recurso(self):
        for etapa in ("worker", "control", "hotkeys", "ui"):
            with self.subTest(etapa=etapa):
                recursos = self._recursos(etapa)
                mic = MicRastreado()
                app, _, _, _, _ = crear_app_runtime(
                    mic=mic, recursos=recursos)
                parche = (mock.patch.object(
                    threading.Thread, "start",
                    side_effect=RuntimeError("worker startup deliberado"))
                    if etapa == "worker" else contextlib.nullcontext())
                with parche, self.assertRaises(RuntimeError):
                    app.ejecutar()
                self._assert_cleanup_completo(app, mic, recursos)
                self.assertFalse(
                    app._trabajador_hilo
                    and app._trabajador_hilo.is_alive())

    def test_cada_closer_fallido_deja_cleanup_terminal_y_evidencia(self):
        etapas = (
            "mic", "control_close", "hotkeys_close",
            "inyector", "guionar", "sesion", "ui_close",
        )
        for etapa in etapas:
            with self.subTest(etapa=etapa):
                recursos = self._recursos(etapa)
                mic = MicRastreado(fallar_cierre=etapa == "mic")
                app, _, _, _, _ = crear_app_runtime(
                    mic=mic, recursos=recursos)
                app.salir()
                self._assert_cleanup_completo(app, mic, recursos)
                self.assertIn("detalle=shutdown:", app._respuesta_estado())
                esperado = {
                    "control_close": "control",
                    "hotkeys_close": "hotkeys",
                    "ui_close": "ui",
                }.get(etapa, etapa)
                self.assertIn(f"{esperado}:RuntimeError", app._ultimo_error)


class PruebasHealth(unittest.TestCase):
    def test_stt_falla_y_una_unidad_futura_recupera(self):
        frases = FrasesGuionadas([RuntimeError("stt"), "segunda"])
        app, mic, _, sesion, _ = crear_app_runtime(frases=frases)
        app.iniciar_grabacion()
        mic.enviar(1)
        mic.enviar(0)
        self.assertTrue(frases.esperar_llamadas(1))
        self.assertTrue(esperar_app(app, lambda: app._stt_failures == 1))
        estado_fallo = app._respuesta_estado()
        self.assertIn("stt=degraded", estado_fallo)
        self.assertIn("stt_failures=1", estado_fallo)
        self.assertIn("last_stt_error_type=RuntimeError", estado_fallo)

        mic.enviar(2)
        mic.enviar(0)
        self.assertTrue(frases.esperar_llamadas(2))
        self.assertTrue(esperar_app(app, lambda: app._stt_health == "recovered"))
        self.assertTrue(sesion.escrito.wait(2))
        estado_ok = app._respuesta_estado()
        self.assertIn("stt=recovered", estado_ok)
        self.assertIn("stt_failures=1", estado_ok)
        self.assertEqual(sesion.textos, ["segunda"])
        self.assertTrue(app._trabajador_hilo.is_alive())
        app.salir()

    def test_stt_streaming_falla_y_decodificacion_futura_recupera(self):
        app, _, _, _, _ = crear_app_runtime()
        motor = MotorStreamingGuionado()
        app.streaming = TranscriptorStreaming(
            motor, sample_rate=16000, interval_s=0.1, trim_s=12)
        app.streaming.aceptar_audio(np.zeros(8000, dtype=np.float32))
        app._paso_streaming(1)
        self.assertIn("stt=degraded", app._respuesta_estado())
        self.assertEqual(app._stt_failures, 1)
        self.assertEqual(app._last_stt_error_type, "OSError")

        app.streaming.aceptar_audio(np.zeros(1600, dtype=np.float32))
        app._paso_streaming(1)
        self.assertIn("stt=recovered", app._respuesta_estado())
        self.assertEqual(app._stt_failures, 1)
        app.salir()

    def test_stt_repetido_es_escalar_y_no_inunda_log(self):
        frases = FrasesGuionadas([RuntimeError("stt")])
        app, mic, _, _, _ = crear_app_runtime(frases=frases)
        app.iniciar_grabacion()
        errores = io.StringIO()
        with contextlib.redirect_stderr(errores):
            for valor in range(20):
                mic.enviar(valor + 1)
                mic.enviar(0)
            self.assertTrue(frases.esperar_llamadas(20))
            self.assertTrue(esperar_app(app, lambda: app._stt_failures == 20))
        self.assertEqual(app._stt_failures, 20)
        self.assertEqual(
            errores.getvalue().count("etapa=stt degradada"), 1)
        self.assertIsInstance(app._stt_failures, int)
        app.salir()

    def test_vad_falla_abierto_y_luego_recupera_con_historial(self):
        vad = VADGuionado([
            RuntimeError("vad"), RuntimeError("vad"),
            RuntimeError("vad"), False,
        ])
        app, mic, _, _, _ = crear_app_runtime(vad=vad)
        app.iniciar_grabacion()
        errores = io.StringIO()
        with contextlib.redirect_stderr(errores):
            for valor in (1, 2, 3):
                mic.enviar(valor)
            self.assertTrue(vad.esperar_llamadas(3))
            self.assertTrue(esperar_app(app, lambda: app._vad_failures == 3))
            estado_fallo = app._respuesta_estado()
            mic.enviar(0)
            self.assertTrue(vad.esperar_llamadas(4))
            self.assertTrue(esperar_app(app, lambda: app._vad_health == "recovered"))
        self.assertIn("vad=degraded", estado_fallo)
        self.assertIn("vad_failures=3", estado_fallo)
        estado_ok = app._respuesta_estado()
        self.assertIn("vad=recovered", estado_ok)
        self.assertIn("vad_failures=3", estado_ok)
        self.assertIn("last_vad_error_type=RuntimeError", estado_ok)
        self.assertEqual(
            errores.getvalue().count("etapa=vad degradada"), 1)
        app.salir()


class PruebasStopRegistry(unittest.TestCase):
    def test_generaciones_no_adoptadas_mantienen_stop_acotado(self):
        mic = MicWorkerBloqueado()
        app, _, _, sesion, _ = crear_app_runtime(mic=mic)
        app._iniciar_trabajador()
        self.assertTrue(mic.worker_entro.wait(2))
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            for _ in range(1000):
                self.assertTrue(app.iniciar_grabacion())
                self.assertTrue(app.detener_grabacion())
        self.assertEqual(len(app._stops_listos), 1)
        self.assertEqual(len(app._errores_stop), 0)
        self.assertEqual(sesion.textos, [])

        mic.liberar_worker.set()
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(app._stops_listos, set())
        self.assertEqual(sesion.textos, [])
        app.salir()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Regresiones deterministas de lifecycle, generación, shutdown y mic."""

import queue
import struct
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App, EstadoApp
from parlar.capturador_audio import (
    CapturadorMic, EventoSegmento, FrameAudio, Segmentador,
)
from parlar.config import Config
from parlar.cliente_guionar import ClienteNulo


class MicFalso:
    def __init__(self, *, fallar_inicio=False, fallar_stop=False,
                 bloquear_inicio=False, bloquear_stop=False):
        self.q = queue.Queue()
        self.generacion = None
        self.fallar_inicio = fallar_inicio
        self.fallar_stop = fallar_stop
        self.bloquear_inicio = bloquear_inicio
        self.bloquear_stop = bloquear_stop
        self.inicio_entrado = threading.Event()
        self.liberar_inicio = threading.Event()
        self.stop_entrado = threading.Event()
        self.liberar_stop = threading.Event()
        self.inicios = 0
        self.detenciones = 0

    def iniciar(self, generacion):
        self.inicios += 1
        self.inicio_entrado.set()
        if self.bloquear_inicio:
            if not self.liberar_inicio.wait(2):
                raise TimeoutError("inicio falso no liberado")
        if self.fallar_inicio:
            raise OSError("dispositivo ocupado")
        self.generacion = generacion
        return True

    def detener(self, *, vaciar=True):
        if self.generacion is not None:
            self.detenciones += 1
        self.stop_entrado.set()
        if self.bloquear_stop:
            if not self.liberar_stop.wait(2):
                raise TimeoutError("stop falso no liberado")
        self.generacion = None
        if vaciar:
            self._filtrar(None)
        if self.fallar_stop:
            raise OSError("falló stop del dispositivo")

    def descartar_pendientes(self, generacion=None):
        self._filtrar(generacion)

    def _filtrar(self, generacion):
        conservar = []
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if generacion is not None and item.generacion != generacion:
                conservar.append(item)
        for item in conservar:
            self.q.put_nowait(item)

    def enviar(self, valor, generacion=None):
        gen = self.generacion if generacion is None else generacion
        if gen is None:
            return False
        self.q.put(FrameAudio(gen, struct.pack("<i", valor)))
        return True

    def leer_frame(self, timeout=0.05):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


class SegmentadorFalso:
    """Cada entero positivo es voz; cero cierra la frase."""

    def __init__(self, *args, fallar=False, **kwargs):
        self.en_voz = False
        self.valores = []
        self.voz_iniciada = threading.Event()
        self.fallar = fallar

    def procesar(self, frame):
        if self.fallar:
            raise RuntimeError("fallo deliberado del segmentador")
        valor = struct.unpack("<i", frame)[0]
        if valor == 0:
            if self.en_voz:
                audio = np.asarray(self.valores, dtype=np.float32)
                self.en_voz = False
                self.valores = []
                yield EventoSegmento("frase", audio)
            return
        if not self.en_voz:
            self.en_voz = True
            self.voz_iniciada.set()
            yield EventoSegmento("inicio_voz")
        self.valores.append(valor)
        yield EventoSegmento("frame_voz", np.asarray([valor], dtype=np.float32))

    def finalizar(self):
        if not self.en_voz:
            return
        audio = np.asarray(self.valores, dtype=np.float32)
        self.en_voz = False
        self.valores = []
        yield EventoSegmento("frase", audio)


class FabricaSegmentador:
    def __init__(self, *, fallar=False):
        self.instancias = []
        self.fallar = fallar
        self.creada = threading.Event()

    def __call__(self, *args, **kwargs):
        instancia = SegmentadorFalso(*args, fallar=self.fallar, **kwargs)
        self.instancias.append(instancia)
        self.creada.set()
        return instancia


class FrasesFalsas:
    def __init__(self, *, bloquear=False):
        self.bloquear = bloquear
        self.inferencia_iniciada = threading.Event()
        self.liberar = threading.Event()
        self.audios = []

    def transcribir(self, audio):
        valores = tuple(int(x) for x in audio.tolist())
        self.audios.append(valores)
        self.inferencia_iniciada.set()
        if self.bloquear and not self.liberar.wait(3):
            raise TimeoutError("STT falso no liberado")
        return "U:" + ",".join(map(str, valores))


class StreamingFalso:
    def __init__(self):
        self.buffer = []
        self.reinicios = 0
        self.finalizaciones = []

    def reiniciar(self):
        self.buffer = []
        self.reinicios += 1

    def aceptar_audio(self, audio):
        self.buffer.extend(int(x) for x in audio.tolist())

    def procesar(self):
        return ""

    def hipotesis_pendiente(self):
        return ""

    def finalizar(self):
        valores = tuple(self.buffer)
        self.finalizaciones.append(valores)
        self.buffer = []
        return "S:" + ",".join(map(str, valores)) if valores else ""


class ProcesadorFalso:
    rewrite_mode = "none"

    def procesar_frase(self, texto):
        return SimpleNamespace(comando=None, carga="", texto=texto)

    def procesar_fragmento(self, texto):
        return texto


class ProcesadorDetener(ProcesadorFalso):
    def procesar_frase(self, texto):
        return SimpleNamespace(comando="detener", carga="", texto="")


class SalidaFalsa:
    def __init__(self, *, bloquear=False, fallar=False):
        self.textos = []
        self.vad = []
        self.parciales = []
        self.bloquear = bloquear
        self.fallar = fallar
        self.escritura_iniciada = threading.Event()
        self.liberar_escritura = threading.Event()
        self.escrito = threading.Event()
        self.cerrado = threading.Event()
        self.reinicios = 0

    def escribir_texto(self, texto, registrar=True):
        self.escritura_iniciada.set()
        if self.bloquear and not self.liberar_escritura.wait(3):
            raise TimeoutError("sink falso no liberado")
        if self.fallar:
            raise RuntimeError("fallo deliberado del sink")
        self.textos.append(texto)
        self.escrito.set()
        return True

    def enviar_parcial(self, texto):
        self.parciales.append(texto)

    def evento_vad(self, hablando):
        self.vad.append(hablando)

    def reiniciar_registro(self):
        self.reinicios += 1

    def nueva_linea(self, cantidad=1):
        return True

    def borrar_ultima_oracion(self):
        return False

    def presionar_enter(self):
        return True

    def cerrar(self):
        self.cerrado.set()


class UIFalsa:
    def __init__(self):
        self.estados = []
        self.cerrada = threading.Event()

    def fijar_estado(self, estado):
        self.estados.append(estado)

    def cerrar(self):
        self.cerrada.set()


class RecursoFalso:
    _listener = None

    def iniciar(self):
        return True

    def detener(self):
        pass


def crear_app(*, modo="utterance", mic=None, frases=None, streaming=None,
              inyector=None, guionar=None, sesion=None, fabrica=None, proc=None):
    cfg = Config(mode=modo, overlay=False, notify=False)
    mic = mic or MicFalso()
    frases = frases or FrasesFalsas()
    streaming = streaming or StreamingFalso()
    inyector = inyector or SalidaFalsa()
    guionar = guionar or SalidaFalsa()
    sesion = sesion or SalidaFalsa()
    fabrica = fabrica or FabricaSegmentador()
    app = App(
        cfg, frases=frases, streaming=streaming, proc=proc or ProcesadorFalso(),
        inyector=inyector, guionar=guionar, sesion=sesion, mic=mic,
        ui=UIFalsa(), control=RecursoFalso(), atajos=RecursoFalso(),
        vad_factory=lambda *_: object(), segmentador_factory=fabrica)
    return app, mic, frases, streaming, inyector, guionar, sesion, fabrica


class PruebasApp(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            if app.estado != EstadoApp.CLOSED:
                app.salir()

    def app(self, **kwargs):
        componentes = crear_app(**kwargs)
        self.apps.append(componentes[0])
        return componentes

    def esperar_voz(self, fabrica):
        self.assertTrue(fabrica.creada.wait(2))
        self.assertTrue(fabrica.instancias[-1].voz_iniciada.wait(2))

    def test_pista_headless_no_promete_indicador(self):
        app, *_ = self.app()
        pista = app._pista_controles()
        self.assertIn("parlarctl", pista)
        self.assertIn("cancelar", pista)
        self.assertNotIn("indicador", pista)

    def test_pista_x11_documenta_ptt_continuo_esc_e_indicador(self):
        app, *_ = self.app()
        app.cfg.overlay = True
        app.atajos._listener = object()
        pista = app._pista_controles()
        self.assertIn(app.cfg.hotkey_toggle, pista)
        self.assertIn("doble toque", pista)
        self.assertIn("Esc cancela", pista)
        self.assertIn("click en el indicador", pista)

    def test_stop_finaliza_frase_abierta(self):
        app, mic, _, _, _, _, sesion, fabrica = self.app()
        self.assertTrue(app.iniciar_grabacion())
        mic.enviar(1)
        mic.enviar(2)
        self.esperar_voz(fabrica)
        self.assertTrue(app.detener_grabacion())
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:1,2"])

    def test_stop_sin_frase_y_stop_inmediato(self):
        app, mic, *_ = self.app()
        self.assertTrue(app.iniciar_grabacion())
        self.assertTrue(app.detener_grabacion())
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(mic.detenciones, 1)

    def test_stop_dos_veces_es_idempotente(self):
        app, mic, *_ = self.app()
        app.iniciar_grabacion()
        app.detener_grabacion()
        app.detener_grabacion()
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(mic.detenciones, 1)

    def test_comando_de_voz_detiene_desde_worker_sin_deadlock(self):
        app, mic, _, _, _, _, _, _ = self.app(proc=ProcesadorDetener())
        app.iniciar_grabacion()
        mic.enviar(3)
        mic.enviar(0)
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertFalse(app.grabando.is_set())

    def test_dos_starts_concurrentes_abren_una_vez(self):
        mic = MicFalso(bloquear_inicio=True)
        app, *_ = self.app(mic=mic)
        barrera = threading.Barrier(3)
        resultados = []

        def iniciar():
            barrera.wait()
            resultados.append(app.iniciar_grabacion())

        hilos = [threading.Thread(target=iniciar) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        barrera.wait()
        self.assertTrue(mic.inicio_entrado.wait(2))
        mic.liberar_inicio.set()
        for hilo in hilos:
            hilo.join(2)
        self.assertEqual(resultados, [True, True])
        self.assertEqual(mic.inicios, 1)

    def test_stop_durante_stt_entrega_resultado(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, _, _, _, _, sesion, _ = self.app(frases=frases)
        app.iniciar_grabacion()
        mic.enviar(7)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()
        self.assertEqual(app.estado, EstadoApp.STOPPING)
        frases.liberar.set()
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:7"])

    def test_restart_invalida_stt_anterior(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, _, _, _, _, sesion, _ = self.app(frases=frases)
        app.iniciar_grabacion()
        gen_a = app.generacion_activa
        mic.enviar(1)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()
        self.assertTrue(app.iniciar_grabacion())
        gen_b = app.generacion_activa
        self.assertNotEqual(gen_a, gen_b)
        mic.enviar(101)
        mic.enviar(0)
        frases.liberar.set()
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(sesion.textos, ["U:101"])
        self.assertEqual(frases.audios, [(1,), (101,)])

    def test_frames_de_generaciones_no_se_mezclan(self):
        app, mic, frases, _, _, _, sesion, fabrica = self.app()
        app.iniciar_grabacion()
        mic.enviar(1)
        mic.enviar(2)
        mic.enviar(3)
        self.esperar_voz(fabrica)
        app.detener_grabacion()
        app.iniciar_grabacion()
        mic.enviar(101)
        mic.enviar(102)
        mic.enviar(103)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(sesion.textos, ["U:101,102,103"])
        for audio in frases.audios:
            self.assertFalse(set(audio) & {1, 2, 3} and set(audio) & {101, 102, 103})

    def test_cambio_streaming_a_frase_respeta_frontera(self):
        app, mic, _, streaming, _, _, sesion, fabrica = self.app(modo="streaming")
        app.iniciar_grabacion()
        mic.enviar(1)
        self.esperar_voz(fabrica)
        app.cambiar_modo("utterance")
        mic.enviar(2)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        mic.enviar(101)
        mic.enviar(0)
        self.assertTrue(_esperar_cantidad(sesion, 2))
        self.assertEqual(sesion.textos, ["S:1,2", "U:101"])
        self.assertEqual(streaming.finalizaciones, [(1, 2)])

    def test_cambio_frase_a_streaming_respeta_frontera(self):
        app, mic, _, streaming, _, _, sesion, fabrica = self.app()
        app.iniciar_grabacion()
        mic.enviar(1)
        self.esperar_voz(fabrica)
        app.cambiar_modo("streaming")
        mic.enviar(2)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        sesion.escrito.clear()
        mic.enviar(101)
        mic.enviar(102)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(sesion.textos, ["U:1,2", "S:101,102"])
        self.assertEqual(streaming.finalizaciones, [(101, 102)])

    def test_cambios_rapidos_son_escalares_y_acotados(self):
        app, mic, _, streaming, _, _, sesion, fabrica = self.app()
        app.iniciar_grabacion()
        mic.enviar(5)
        self.esperar_voz(fabrica)
        for i in range(2000):
            app.cambiar_modo("streaming" if i % 2 == 0 else "utterance")
        app.cambiar_modo("streaming")
        self.assertLessEqual(len(app._modo_por_sesion), 1)
        self.assertEqual(streaming.buffer, [])
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        mic.enviar(105)
        mic.enviar(0)
        self.assertTrue(_esperar_cantidad(sesion, 2))
        self.assertEqual(sesion.textos, ["U:5", "S:105"])

    def test_shutdown_durante_stt_invalida_y_cierra_despues(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, _, _, inyector, _, sesion, _ = self.app(frases=frases)
        app.iniciar_grabacion()
        mic.enviar(8)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        hilo = threading.Thread(target=app.salir)
        hilo.start()
        self.assertTrue(app.esperar_estado(EstadoApp.SHUTTING_DOWN))
        self.assertFalse(inyector.cerrado.is_set())
        frases.liberar.set()
        hilo.join(2)
        self.assertFalse(hilo.is_alive())
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertEqual(sesion.textos, [])
        self.assertTrue(inyector.cerrado.is_set())

    def test_shutdown_espera_escritura_en_curso(self):
        inyector = SalidaFalsa(bloquear=True)
        app, mic, _, _, _, _, _, _ = self.app(inyector=inyector)
        app.iniciar_grabacion()
        mic.enviar(9)
        mic.enviar(0)
        self.assertTrue(inyector.escritura_iniciada.wait(2))
        hilo = threading.Thread(target=app.salir)
        hilo.start()
        self.assertTrue(app.esperar_estado(EstadoApp.SHUTTING_DOWN))
        self.assertFalse(inyector.cerrado.is_set())
        inyector.liberar_escritura.set()
        hilo.join(2)
        self.assertFalse(hilo.is_alive())
        self.assertTrue(inyector.cerrado.is_set())

    def test_shutdown_espera_envio_guionar_en_curso(self):
        guionar = SalidaFalsa(bloquear=True)
        app, mic, _, _, _, _, _, _ = self.app(guionar=guionar)
        app.iniciar_grabacion()
        mic.enviar(10)
        mic.enviar(0)
        self.assertTrue(guionar.escritura_iniciada.wait(2))
        hilo = threading.Thread(target=app.salir)
        hilo.start()
        self.assertTrue(app.esperar_estado(EstadoApp.SHUTTING_DOWN))
        self.assertFalse(guionar.cerrado.is_set())
        guionar.liberar_escritura.set()
        hilo.join(2)
        self.assertFalse(hilo.is_alive())
        self.assertTrue(guionar.cerrado.is_set())

    def test_shutdown_doble_start_posterior_y_cero_output(self):
        app, mic, _, _, _, _, sesion, _ = self.app()
        app.iniciar_grabacion()
        generacion = app.generacion_activa
        barrera = threading.Barrier(3)

        def cerrar():
            barrera.wait()
            app.salir()

        hilos = [threading.Thread(target=cerrar) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        barrera.wait()
        for hilo in hilos:
            hilo.join(2)
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertFalse(app.iniciar_grabacion())
        self.assertEqual(mic.inicios, 1)
        self.assertFalse(app._escribir_en_todas("tarde", generacion))
        self.assertEqual(sesion.textos, [])

    def test_inicio_fallido_es_error_control_y_recuperable(self):
        mic = MicFalso(fallar_inicio=True)
        app, *_ = self.app(mic=mic)
        respuesta = app._atender_comando("iniciar")
        self.assertTrue(respuesta.startswith("ERR micrófono:"), respuesta)
        self.assertEqual(app.estado, EstadoApp.ERROR)
        self.assertFalse(app.grabando.is_set())
        mic.fallar_inicio = False
        self.assertTrue(app.iniciar_grabacion())
        self.assertEqual(app.estado, EstadoApp.RECORDING)

    def test_fallo_al_cerrar_mic_queda_visible_en_health(self):
        mic = MicFalso(fallar_stop=True)
        app, *_ = self.app(mic=mic)
        app.iniciar_grabacion()
        self.assertFalse(app.detener_grabacion())
        self.assertTrue(app.esperar_estado(EstadoApp.ERROR))
        self.assertIn("error detalle=micrófono:", app._atender_comando("estado"))

    def test_fallo_inesperado_worker_cambia_health(self):
        fabrica = FabricaSegmentador(fallar=True)
        app, mic, *_ = self.app(fabrica=fabrica)
        app.iniciar_grabacion()
        mic.enviar(12)
        self.assertTrue(app.esperar_estado(EstadoApp.ERROR))
        self.assertFalse(app.grabando.is_set())
        self.assertIn("worker:", app._atender_comando("estado"))
        self.assertFalse(app.iniciar_grabacion())



    def test_cancel_descarta_frase_abierta_sin_emitirla(self):
        app, mic, _, _, _, _, sesion, fabrica = self.app()

        self.assertTrue(
            app.iniciar_grabacion()
        )
        mic.enviar(1)
        mic.enviar(2)
        self.esperar_voz(fabrica)

        self.assertTrue(
            app.cancelar_grabacion()
        )
        self.assertEqual(
            app.estado,
            EstadoApp.IDLE,
        )
        self.assertFalse(
            app.grabando.is_set()
        )

        # Una sesión posterior sirve como barrera observable del worker.
        self.assertTrue(
            app.iniciar_grabacion()
        )
        mic.enviar(101)
        mic.enviar(0)

        self.assertTrue(
            sesion.escrito.wait(2)
        )
        self.assertEqual(
            sesion.textos,
            ["U:101"],
        )

    def test_cancel_durante_stt_invalida_resultado_tardio(self):
        frases = FrasesFalsas(
            bloquear=True
        )
        app, mic, _, _, _, _, sesion, _ = self.app(
            frases=frases
        )

        self.assertTrue(
            app.iniciar_grabacion()
        )
        mic.enviar(7)
        mic.enviar(0)

        self.assertTrue(
            frases.inferencia_iniciada.wait(2)
        )

        self.assertTrue(
            app.cancelar_grabacion()
        )
        self.assertEqual(
            app.estado,
            EstadoApp.IDLE,
        )

        frases.liberar.set()

        self.assertTrue(
            app.iniciar_grabacion()
        )
        mic.enviar(101)
        mic.enviar(0)

        self.assertTrue(
            sesion.escrito.wait(2)
        )
        self.assertEqual(
            sesion.textos,
            ["U:101"],
        )

    def test_cancel_durante_starting_no_resucita_recording(self):
        mic = MicFalso(
            bloquear_inicio=True
        )
        app, *_ = self.app(
            mic=mic
        )

        resultado_start = []
        resultado_cancel = []

        hilo_start = threading.Thread(
            target=lambda:
                resultado_start.append(
                    app.iniciar_grabacion()
                )
        )
        hilo_start.start()

        self.assertTrue(
            mic.inicio_entrado.wait(2)
        )

        hilo_cancel = threading.Thread(
            target=lambda:
                resultado_cancel.append(
                    app.cancelar_grabacion()
                )
        )
        hilo_cancel.start()

        self.assertTrue(
            app.esperar_estado(
                EstadoApp.IDLE
            )
        )

        mic.liberar_inicio.set()

        hilo_start.join(2)
        hilo_cancel.join(2)

        self.assertFalse(
            hilo_start.is_alive()
        )
        self.assertFalse(
            hilo_cancel.is_alive()
        )
        self.assertEqual(
            resultado_start,
            [False],
        )
        self.assertEqual(
            resultado_cancel,
            [True],
        )
        self.assertEqual(
            app.estado,
            EstadoApp.IDLE,
        )
        self.assertIsNone(
            app.generacion_activa
        )
        self.assertFalse(
            app.grabando.is_set()
        )

    def test_cancel_durante_stopping_no_republica_generacion_vieja(self):
        mic = MicFalso(bloquear_stop=True)
        app, _, _, _, _, _, sesion, _ = self.app(mic=mic)
        self.assertTrue(app.iniciar_grabacion())
        generacion = app.generacion_activa

        resultado_stop = []
        resultado_cancel = []
        hilo_stop = threading.Thread(
            target=lambda: resultado_stop.append(app.detener_grabacion()))
        hilo_stop.start()
        self.assertTrue(mic.stop_entrado.wait(2))
        self.assertEqual(app.estado, EstadoApp.STOPPING)

        hilo_cancel = threading.Thread(
            target=lambda: resultado_cancel.append(app.cancelar_grabacion()))
        hilo_cancel.start()
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))

        mic.liberar_stop.set()
        hilo_stop.join(2)
        hilo_cancel.join(2)
        self.assertFalse(hilo_stop.is_alive())
        self.assertFalse(hilo_cancel.is_alive())
        self.assertEqual(resultado_stop, [True])
        self.assertEqual(resultado_cancel, [True])
        self.assertEqual(app.estado, EstadoApp.IDLE)
        self.assertIsNone(app.generacion_activa)
        self.assertNotIn(generacion, app._stops_listos)
        self.assertEqual(sesion.textos, [])

    def test_cancel_viejo_no_cierra_captura_de_restart(self):
        for ruta_start in ("ipc", "gesto"):
            with self.subTest(ruta_start=ruta_start):
                mic = MicFalso(bloquear_stop=True)
                app, _, _, _, _, _, sesion, fabrica = self.app(mic=mic)
                self.assertTrue(app.iniciar_grabacion())
                generacion_vieja = app.generacion_activa
                mic.enviar(1)
                self.esperar_voz(fabrica)

                reinicio_entrado = threading.Event()
                liberar_reinicio = threading.Event()
                reiniciar_original = app.gesto_dictado.reiniciar

                def reiniciar_bloqueado():
                    reiniciar_original()
                    reinicio_entrado.set()
                    if not liberar_reinicio.wait(2):
                        raise TimeoutError("CANCEL no liberado")

                app.gesto_dictado.reiniciar = reiniciar_bloqueado

                resultado_stop = []
                hilo_stop = threading.Thread(
                    target=lambda: resultado_stop.append(
                        app.detener_grabacion()))
                hilo_stop.start()
                self.assertTrue(mic.stop_entrado.wait(2))
                self.assertEqual(app.estado, EstadoApp.STOPPING)

                resultado_cancel = []
                hilo_cancel = threading.Thread(
                    target=lambda: resultado_cancel.append(
                        app.cancelar_grabacion()))
                hilo_cancel.start()
                self.assertTrue(reinicio_entrado.wait(2))
                self.assertEqual(app.estado, EstadoApp.IDLE)

                mic.liberar_stop.set()
                hilo_stop.join(2)
                self.assertFalse(hilo_stop.is_alive())

                if ruta_start == "ipc":
                    self.assertEqual(
                        app._atender_comando("iniciar"), "OK grabando")
                else:
                    app.gesto_dictado.presionar()

                generacion_nueva = app.generacion_activa
                self.assertNotEqual(generacion_vieja, generacion_nueva)
                self.assertEqual(mic.generacion, generacion_nueva)

                liberar_reinicio.set()
                hilo_cancel.join(2)
                self.assertFalse(hilo_cancel.is_alive())
                self.assertEqual(resultado_stop, [True])
                self.assertEqual(resultado_cancel, [True])
                self.assertEqual(app.estado, EstadoApp.RECORDING)
                self.assertEqual(app.generacion_activa, generacion_nueva)
                self.assertEqual(mic.generacion, generacion_nueva)
                self.assertTrue(app.grabando.is_set())
                self.assertEqual(app.ui.estados[-1], "recording")

                self.assertTrue(mic.enviar(101))
                mic.enviar(0)
                self.assertTrue(sesion.escrito.wait(2))
                self.assertEqual(sesion.textos, ["U:101"])

    def test_cancel_viejo_no_publica_estado_terminal_sobre_restart(self):
        for fallar_stop, estado_terminal, resultado_esperado in (
                (False, "idle", True),
                (True, "error", False)):
            with self.subTest(estado_terminal=estado_terminal):
                mic = MicFalso(fallar_stop=fallar_stop)
                app, _, _, _, _, _, sesion, fabrica = self.app(mic=mic)
                self.assertTrue(app.iniciar_grabacion())
                generacion_vieja = app.generacion_activa

                publicar_original = (
                    app._estado_visual_terminal_si_vigente
                )
                publicador_viejo_entrado = threading.Event()
                liberar_publicador_viejo = threading.Event()

                def publicar_bloqueado(estado, estado_app, generacion):
                    if estado == estado_terminal:
                        publicador_viejo_entrado.set()
                        if not liberar_publicador_viejo.wait(2):
                            raise TimeoutError("publicador viejo no liberado")
                    return publicar_original(estado, estado_app, generacion)

                app._estado_visual_terminal_si_vigente = publicar_bloqueado
                resultado_cancel = []
                hilo_cancel = threading.Thread(
                    target=lambda: resultado_cancel.append(
                        app.cancelar_grabacion()))
                hilo_cancel.start()

                self.assertTrue(publicador_viejo_entrado.wait(2))
                self.assertEqual(app.estado, (
                    EstadoApp.ERROR if fallar_stop else EstadoApp.IDLE))

                self.assertEqual(
                    app._atender_comando("iniciar"), "OK grabando")
                generacion_nueva = app.generacion_activa
                self.assertNotEqual(generacion_vieja, generacion_nueva)
                self.assertEqual(app.ui.estados[-1], "recording")

                liberar_publicador_viejo.set()
                hilo_cancel.join(2)
                self.assertFalse(hilo_cancel.is_alive())
                self.assertEqual(resultado_cancel, [resultado_esperado])
                self.assertEqual(app.estado, EstadoApp.RECORDING)
                self.assertEqual(app.generacion_activa, generacion_nueva)
                self.assertEqual(mic.generacion, generacion_nueva)
                self.assertTrue(app.grabando.is_set())
                self.assertEqual(app.ui.estados[-1], "recording")

                self.assertTrue(mic.enviar(101))
                mic.enviar(0)
                self.assertTrue(fabrica.creada.wait(2))
                self.assertTrue(sesion.escrito.wait(2))
                self.assertEqual(sesion.textos, ["U:101"])

    def test_cancel_dos_veces_es_idempotente(self):
        app, mic, *_ = self.app()

        self.assertTrue(
            app.iniciar_grabacion()
        )
        self.assertTrue(
            app.cancelar_grabacion()
        )
        self.assertTrue(
            app.cancelar_grabacion()
        )

        self.assertEqual(
            app.estado,
            EstadoApp.IDLE,
        )
        self.assertEqual(
            mic.detenciones,
            1,
        )

    def test_cancel_limpia_parcial_guionar_sin_borrar_final_previo(self):
        app, mic, _, _, _, guionar, sesion, _ = self.app(modo="streaming")
        self.assertTrue(app.iniciar_grabacion())
        mic.enviar(1)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(guionar.textos, ["S:1"])
        self.assertEqual(sesion.textos, ["S:1"])

        guionar.enviar_parcial("hipótesis pendiente")
        self.assertTrue(app.cancelar_grabacion())

        self.assertEqual(guionar.parciales[-2:], [
            "hipótesis pendiente", "",
        ])
        self.assertEqual(guionar.textos, ["S:1"])
        self.assertEqual(sesion.textos, ["S:1"])

    def test_pipeline_funciona_con_guionar_desactivado(self):
        app, mic, _, _, _, guionar, sesion, _ = self.app(
            guionar=ClienteNulo())
        self.assertTrue(guionar.es_nulo)
        self.assertTrue(app.iniciar_grabacion())
        mic.enviar(4)
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(sesion.textos, ["U:4"])
        self.assertTrue(app.cancelar_grabacion())



    def test_ipc_cancel_alias_invalida_sesion(self):
        app, mic, _, _, _, _, sesion, _ = self.app()

        self.assertTrue(
            app.iniciar_grabacion()
        )

        mic.enviar(9)

        self.assertEqual(
            app._atender_comando("cancel"),
            "OK detenido",
        )
        self.assertEqual(
            app.estado,
            EstadoApp.IDLE,
        )
        self.assertIsNone(
            app.generacion_activa
        )
        self.assertEqual(
            sesion.textos,
            [],
        )


def _esperar_cantidad(salida, cantidad, timeout=2.0):
    limite = threading.Event()
    # Las escrituras disparan ``escrito``. Limpiarlo y esperar evita sleeps;
    # el loop solo se repite cuando hubo progreso observable.
    while len(salida.textos) < cantidad:
        salida.escrito.clear()
        if len(salida.textos) >= cantidad:
            break
        if not salida.escrito.wait(timeout):
            limite.set()
            break
    return not limite.is_set() and len(salida.textos) >= cantidad


class PruebasCapturadorMic(unittest.TestCase):
    def test_callback_etiqueta_generacion_y_descarta_callback_viejo(self):
        mic = CapturadorMic(16000, 2)
        mic._generacion_aceptada = 2
        mic._callback(1, b"\x01\x00\x02\x00", 2, None, None)
        self.assertIsNone(mic.leer_frame(timeout=0))
        mic._callback(2, b"\x01\x00\x02\x00", 2, None, None)
        item = mic.leer_frame(timeout=0)
        self.assertEqual(item.generacion, 2)
        self.assertEqual(item.audio, b"\x01\x00\x02\x00")

    def test_segmentador_finaliza_voz_abierta(self):
        class VAD:
            def is_speech(self, frame, sr):
                return True

        frame = (np.ones(320, dtype=np.int16) * 1000).tobytes()
        seg = Segmentador(16000, 20, VAD(), 600, 0, 30, 20)
        eventos = list(seg.procesar(frame))
        self.assertIn("inicio_voz", [evento.tipo for evento in eventos])
        finales = list(seg.finalizar())
        self.assertEqual([evento.tipo for evento in finales], ["frase"])
        self.assertGreater(finales[0].audio.size, 0)
        self.assertEqual(list(seg.finalizar()), [])

    def test_constructor_falla_sin_dejar_handle(self):
        mic = CapturadorMic(16000, 320)
        modulo = SimpleNamespace(RawInputStream=mock.Mock(side_effect=OSError("boom")))
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            with self.assertRaises(OSError):
                mic.iniciar(1)
        self.assertIsNone(mic._stream)
        self.assertEqual(mic._estado, "idle")

    def test_stream_start_falla_y_cierra(self):
        stream = mock.Mock()
        stream.start.side_effect = OSError("start")
        modulo = SimpleNamespace(RawInputStream=mock.Mock(return_value=stream))
        mic = CapturadorMic(16000, 320)
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            with self.assertRaises(OSError):
                mic.iniciar(1)
        stream.close.assert_called_once()
        self.assertIsNone(mic._stream)

    def test_stop_falla_pero_close_y_cleanup_ocurren(self):
        stream = mock.Mock()
        stream.stop.side_effect = OSError("stop")
        modulo = SimpleNamespace(RawInputStream=mock.Mock(return_value=stream))
        mic = CapturadorMic(16000, 320)
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            mic.iniciar(1)
            with self.assertRaises(OSError):
                mic.detener()
        stream.close.assert_called_once()
        self.assertIsNone(mic._stream)
        self.assertEqual(mic._estado, "idle")

    def test_close_falla_pero_stop_repetido_es_seguro(self):
        stream = mock.Mock()
        stream.close.side_effect = OSError("close")
        modulo = SimpleNamespace(RawInputStream=mock.Mock(return_value=stream))
        mic = CapturadorMic(16000, 320)
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            mic.iniciar(1)
            with self.assertRaises(OSError):
                mic.detener()
            mic.detener()
        self.assertIsNone(mic._stream)
        self.assertEqual(mic._estado, "idle")

    def test_dos_inicios_concurrentes_crean_un_stream(self):
        entrada = threading.Event()
        liberar = threading.Event()
        stream = mock.Mock()

        def start():
            entrada.set()
            self.assertTrue(liberar.wait(2))

        stream.start.side_effect = start
        constructor = mock.Mock(return_value=stream)
        modulo = SimpleNamespace(RawInputStream=constructor)
        mic = CapturadorMic(16000, 320)
        resultados = []
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            hilo = threading.Thread(target=lambda: resultados.append(mic.iniciar(1)))
            hilo.start()
            self.assertTrue(entrada.wait(2))
            resultados.append(mic.iniciar(2))
            liberar.set()
            hilo.join(2)
            mic.detener()
        self.assertEqual(sorted(resultados), [False, True])
        self.assertEqual(constructor.call_count, 1)

    def test_dos_stops_concurrentes_comparten_un_unico_cierre(self):
        stop_entrado = threading.Event()
        liberar_stop = threading.Event()
        stream = mock.Mock()

        def stop():
            stop_entrado.set()
            self.assertTrue(liberar_stop.wait(2))

        stream.stop.side_effect = stop
        modulo = SimpleNamespace(RawInputStream=mock.Mock(return_value=stream))
        mic = CapturadorMic(16000, 320)
        resultados = []
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            mic.iniciar(1)
            primero = threading.Thread(
                target=lambda: (mic.detener(), resultados.append("primero")))
            segundo = threading.Thread(
                target=lambda: (mic.detener(), resultados.append("segundo")))
            primero.start()
            self.assertTrue(stop_entrado.wait(2))
            segundo.start()
            liberar_stop.set()
            primero.join(2)
            segundo.join(2)
        self.assertCountEqual(resultados, ["primero", "segundo"])
        stream.stop.assert_called_once()
        stream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

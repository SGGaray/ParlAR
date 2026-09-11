"""Regresiones deterministas de overflow, discontinuidad y recuperación."""

import queue
import struct
import threading
import unittest
from types import SimpleNamespace

import numpy as np

from parlar.app import App, EstadoApp
from parlar.capturador_audio import (
    CapturadorMic, EventoSegmento, FrameAudio, Segmentador,
)
from parlar.config import Config


def _valor(frame):
    return struct.unpack("<i", frame)[0]


class MicSinHardware(CapturadorMic):
    """Capturador real sin abrir PortAudio, para pruebas del worker."""

    def __init__(self, capacidad=5):
        super().__init__(16000, 2, max_cola=capacidad)

    def iniciar(self, generacion):
        with self._callback_lock:
            self._preparar_generacion(generacion)
        self._estado = "recording"
        return True

    def detener(self, *, vaciar=True):
        with self._callback_lock:
            self._generacion_aceptada = None
            self._resto = b""
            if vaciar:
                self._vaciar_cola()
        self._estado = "idle"

    def producir(self, valor):
        generacion = self._generacion_aceptada
        if generacion is None:
            return False
        self._callback(
            generacion, struct.pack("<i", valor), 2, None, None)
        return True


class MicGuionado:
    def __init__(self):
        self.q = queue.Queue()
        self.generacion = None

    def iniciar(self, generacion):
        self.generacion = generacion
        return True

    def detener(self, *, vaciar=True):
        self.generacion = None
        if vaciar:
            self.descartar_pendientes()

    def descartar_pendientes(self, generacion=None):
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

    def enviar(self, valor, secuencia, *, discontinuidad=False):
        self.q.put(FrameAudio(
            self.generacion, struct.pack("<i", valor),
            secuencia=secuencia, discontinuidad_antes=discontinuidad,
        ))

    def leer_frame(self, timeout=0.05):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


class SegmentadorNumeros:
    """Cada entero positivo es voz y cero cierra la unidad."""

    def __init__(self, *args, **kwargs):
        self.en_voz = False
        self.valores = []
        self.voz_iniciada = threading.Event()
        self.discontinuidades = 0

    def procesar(self, frame):
        valor = _valor(frame)
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
        yield EventoSegmento(
            "frame_voz", np.asarray([valor], dtype=np.float32))

    def discontinuidad(self):
        self.discontinuidades += 1
        if not self.en_voz:
            self.valores = []
            return
        audio = np.asarray(self.valores, dtype=np.float32)
        self.en_voz = False
        self.valores = []
        yield EventoSegmento("frase", audio)

    def finalizar(self):
        if not self.en_voz:
            return
        audio = np.asarray(self.valores, dtype=np.float32)
        self.en_voz = False
        self.valores = []
        yield EventoSegmento("frase", audio)


class FabricaSegmentador:
    def __init__(self):
        self.instancias = []
        self.creada = threading.Event()

    def __call__(self, *args, **kwargs):
        instancia = SegmentadorNumeros(*args, **kwargs)
        self.instancias.append(instancia)
        self.creada.set()
        return instancia


class FrasesControladas:
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


class StreamingControlado:
    def __init__(self, *, bloquear=False):
        self.buffer = []
        self.bloquear = bloquear
        self.proceso_iniciado = threading.Event()
        self.liberar = threading.Event()
        self.finalizaciones = []
        self.reinicios = 0

    def reiniciar(self):
        self.buffer = []
        self.reinicios += 1

    def aceptar_audio(self, audio):
        self.buffer.extend(int(x) for x in audio.tolist())

    def procesar(self):
        self.proceso_iniciado.set()
        if self.bloquear and not self.liberar.wait(3):
            raise TimeoutError("streaming falso no liberado")
        return ""

    def hipotesis_pendiente(self):
        return ""

    def finalizar(self):
        valores = tuple(self.buffer)
        self.finalizaciones.append(valores)
        self.buffer = []
        return "S:" + ",".join(map(str, valores)) if valores else ""


class ProcesadorDirecto:
    rewrite_mode = "none"

    def procesar_frase(self, texto):
        return SimpleNamespace(comando=None, carga="", texto=texto)

    def procesar_fragmento(self, texto):
        return texto


class SalidaControlada:
    def __init__(self, *, bloquear=False):
        self.bloquear = bloquear
        self.textos = []
        self.parciales = []
        self.vad = []
        self.escritura_iniciada = threading.Event()
        self.liberar = threading.Event()
        self.escrito = threading.Event()

    def escribir_texto(self, texto, registrar=True):
        self.escritura_iniciada.set()
        if self.bloquear and not self.liberar.wait(3):
            raise TimeoutError("sink falso no liberado")
        self.textos.append(texto)
        self.escrito.set()
        return True

    def enviar_parcial(self, texto):
        self.parciales.append(texto)

    def evento_vad(self, hablando):
        self.vad.append(hablando)

    def reiniciar_registro(self):
        pass

    def nueva_linea(self, cantidad=1):
        return True

    def borrar_ultima_oracion(self):
        return False

    def presionar_enter(self):
        return True

    def cerrar(self):
        pass


class UIFalsa:
    def __init__(self):
        self.estados = []

    def fijar_estado(self, estado):
        self.estados.append(estado)

    def cerrar(self):
        pass


class RecursoFalso:
    _listener = None

    def iniciar(self):
        return True

    def detener(self):
        pass


def crear_app(*, mic, modo="utterance", frases=None, streaming=None,
              inyector=None):
    cfg = Config(mode=modo, overlay=False, notify=False)
    fabrica = FabricaSegmentador()
    frases = frases or FrasesControladas()
    streaming = streaming or StreamingControlado()
    inyector = inyector or SalidaControlada()
    guionar = SalidaControlada()
    sesion = SalidaControlada()
    app = App(
        cfg, frases=frases, streaming=streaming, proc=ProcesadorDirecto(),
        inyector=inyector, guionar=guionar, sesion=sesion, mic=mic,
        ui=UIFalsa(), control=RecursoFalso(), atajos=RecursoFalso(),
        vad_factory=lambda *_: object(), segmentador_factory=fabrica,
    )
    return app, frases, streaming, inyector, sesion, fabrica


def esperar(predicado, evento=None, timeout=2):
    if evento is not None:
        evento.wait(timeout)
    return predicado()


class PruebasCapturaAcotada(unittest.TestCase):
    def activar(self, capacidad=5, generacion=1):
        mic = CapturadorMic(100, 2, max_cola=capacidad)
        with mic._callback_lock:
            mic._preparar_generacion(generacion)
        return mic

    @staticmethod
    def producir(mic, cantidad, inicio=1):
        for valor in range(inicio, inicio + cantidad):
            mic._callback(
                mic._generacion_aceptada, struct.pack("<i", valor),
                2, None, None)

    def test_overflow_drop_oldest_sequence_y_discontinuidad(self):
        mic = self.activar(capacidad=5)
        self.producir(mic, 7)
        antes = mic.estado_captura()
        items = [mic.leer_frame(0) for _ in range(5)]
        despues = mic.estado_captura()

        self.assertEqual([item.secuencia for item in items], [3, 4, 5, 6, 7])
        self.assertEqual([_valor(item.audio) for item in items], [3, 4, 5, 6, 7])
        self.assertTrue(items[0].discontinuidad_antes)
        self.assertFalse(any(item.discontinuidad_antes for item in items[1:]))
        self.assertEqual(antes.frames_capturados, 7)
        self.assertEqual(antes.frames_descartados, 2)
        self.assertEqual(antes.queue_depth, 5)
        self.assertEqual(antes.max_queue_depth, 5)
        self.assertEqual(antes.backlog_ms, 100.0)
        self.assertTrue(antes.degradada)
        self.assertEqual(despues.discontinuidades, 1)
        self.assertFalse(despues.degradada)
        self.assertEqual(
            despues.frames_capturados,
            despues.frames_entregados + despues.queue_depth
            + despues.frames_descartados,
        )

    def test_sin_overflow_no_hay_drop_ni_discontinuidad(self):
        mic = self.activar(capacidad=5)
        self.producir(mic, 4)
        antes = mic.estado_captura()
        items = [mic.leer_frame(0) for _ in range(4)]
        estado = mic.estado_captura()
        self.assertEqual(antes.queue_depth, 4)
        self.assertEqual(antes.backlog_ms, 80.0)
        self.assertFalse(antes.degradada)
        self.assertEqual([item.secuencia for item in items], [1, 2, 3, 4])
        self.assertFalse(any(item.discontinuidad_antes for item in items))
        self.assertEqual(estado.frames_descartados, 0)
        self.assertEqual(estado.discontinuidades, 0)

    def test_overflow_repetido_mantiene_cota_y_cuenta_gaps(self):
        mic = self.activar(capacidad=5)
        self.producir(mic, 7)
        primero = mic.leer_frame(0)
        self.producir(mic, 7, inicio=8)
        segundo = mic.leer_frame(0)
        estado = mic.estado_captura()
        self.assertTrue(primero.discontinuidad_antes)
        self.assertTrue(segundo.discontinuidad_antes)
        self.assertEqual(segundo.secuencia, 10)
        self.assertEqual(estado.frames_descartados, 8)
        self.assertEqual(estado.discontinuidades, 2)
        self.assertLessEqual(estado.queue_depth, 5)
        self.assertEqual(estado.max_queue_depth, 5)

    def test_nueva_generacion_reinicia_sequence_y_salud(self):
        mic = self.activar(capacidad=3, generacion=10)
        self.producir(mic, 5)
        primero = mic.leer_frame(0)
        estado_a = mic.estado_captura()
        with mic._callback_lock:
            mic._preparar_generacion(11)
        self.producir(mic, 1, inicio=101)
        segundo = mic.leer_frame(0)
        estado_b = mic.estado_captura()
        self.assertTrue(primero.discontinuidad_antes)
        self.assertEqual(estado_a.generacion, 10)
        self.assertEqual(estado_a.frames_descartados, 2)
        self.assertEqual(segundo.secuencia, 1)
        self.assertFalse(segundo.discontinuidad_antes)
        self.assertEqual(estado_b.generacion, 11)
        self.assertEqual(estado_b.frames_capturados, 1)
        self.assertEqual(estado_b.frames_descartados, 0)
        self.assertEqual(estado_b.discontinuidades, 0)

    def test_soak_100000_frames_memoria_acotada_y_recuperacion(self):
        total = 0

        normal = self.activar(capacidad=32)
        for i in range(20000):
            self.producir(normal, 1, inicio=i)
            self.assertIsNotNone(normal.leer_frame(0))
        total += normal.estado_captura().frames_capturados
        estado_normal = normal.estado_captura()
        self.assertEqual(estado_normal.frames_entregados, 20000)
        self.assertEqual(estado_normal.frames_descartados, 0)
        self.assertEqual(estado_normal.max_queue_depth, 1)

        rafagas = self.activar(capacidad=32)
        ultimo_capturado = ultimo_drop = 0
        for base in range(0, 40000, 100):
            self.producir(rafagas, 100, inicio=base)
            for _ in range(16):
                rafagas.leer_frame(0)
            estado = rafagas.estado_captura()
            self.assertGreaterEqual(estado.frames_capturados, ultimo_capturado)
            self.assertGreaterEqual(estado.frames_descartados, ultimo_drop)
            self.assertLessEqual(estado.queue_depth, 32)
            ultimo_capturado = estado.frames_capturados
            ultimo_drop = estado.frames_descartados
        total += rafagas.estado_captura().frames_capturados
        estado_rafagas = rafagas.estado_captura()
        self.assertEqual(estado_rafagas.frames_entregados, 6400)
        self.assertEqual(estado_rafagas.queue_depth, 16)
        self.assertEqual(estado_rafagas.frames_descartados, 33584)
        self.assertEqual(estado_rafagas.discontinuidades, 400)

        bloqueado = self.activar(capacidad=16)
        for base in range(0, 40000, 200):
            self.producir(bloqueado, 200, inicio=base)
            while bloqueado.leer_frame(0) is not None:
                pass
            self.assertLessEqual(bloqueado.estado_captura().queue_depth, 16)
        total += bloqueado.estado_captura().frames_capturados
        estado_bloqueado = bloqueado.estado_captura()
        self.assertEqual(total, 100000)
        self.assertEqual(estado_rafagas.max_queue_depth, 32)
        self.assertEqual(estado_bloqueado.frames_entregados, 3200)
        self.assertEqual(estado_bloqueado.frames_descartados, 36800)
        self.assertEqual(estado_bloqueado.discontinuidades, 200)
        self.assertEqual(estado_bloqueado.max_queue_depth, 16)


class PruebasFronteraDiscontinua(unittest.TestCase):
    @staticmethod
    def frame(valor):
        return (np.ones(320, dtype=np.int16) * valor).tobytes()

    def test_gap_dentro_de_voz_cierra_y_separa_ambos_lados(self):
        class VAD:
            def is_speech(self, frame, sr):
                return np.frombuffer(frame, dtype=np.int16)[0] > 0

        seg = Segmentador(16000, 20, VAD(), 20, 0, 30, 20)
        list(seg.procesar(self.frame(100)))
        pre = list(seg.discontinuidad())
        post = list(seg.procesar(self.frame(200)))
        post.extend(seg.procesar(self.frame(0)))
        frases = [e.audio for e in pre + post if e.tipo == "frase"]
        self.assertEqual(len(frases), 2)
        self.assertTrue(np.all(frases[0] == 100 / 32768.0))
        self.assertFalse(np.any(frases[0] == 200 / 32768.0))
        self.assertTrue(np.any(frases[1] == 200 / 32768.0))
        self.assertFalse(np.any(frases[1] == 100 / 32768.0))

    def test_gap_fuera_de_voz_limpia_preroll(self):
        class VAD:
            def is_speech(self, frame, sr):
                return np.frombuffer(frame, dtype=np.int16)[0] > 0

        seg = Segmentador(16000, 20, VAD(), 20, 40, 30, 20)
        list(seg.procesar(self.frame(-100)))
        self.assertEqual(list(seg.discontinuidad()), [])
        eventos = list(seg.procesar(self.frame(200)))
        eventos.extend(seg.procesar(self.frame(0)))
        frase = next(e.audio for e in eventos if e.tipo == "frase")
        self.assertFalse(np.any(frase == -100 / 32768.0))


class PruebasWorkerConBackpressure(unittest.TestCase):
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

    def test_utterance_no_mezcla_frames_a_ambos_lados_del_gap(self):
        mic = MicGuionado()
        app, frases, _, _, sesion, fabrica = self.app(mic=mic)
        app.iniciar_grabacion()
        mic.enviar(1, 1)
        self.assertTrue(fabrica.creada.wait(2))
        self.assertTrue(fabrica.instancias[-1].voz_iniciada.wait(2))
        mic.enviar(5, 5, discontinuidad=True)
        mic.enviar(0, 6)
        self.assertTrue(sesion.escrito.wait(2))
        while len(frases.audios) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(frases.audios, [(1,), (5,)])

    def test_streaming_reinicia_buffer_en_gap(self):
        mic = MicGuionado()
        streaming = StreamingControlado()
        app, _, _, _, sesion, fabrica = self.app(
            mic=mic, modo="streaming", streaming=streaming)
        app.iniciar_grabacion()
        mic.enviar(1, 1)
        self.assertTrue(fabrica.creada.wait(2))
        self.assertTrue(fabrica.instancias[-1].voz_iniciada.wait(2))
        mic.enviar(5, 5, discontinuidad=True)
        mic.enviar(0, 6)
        while len(streaming.finalizaciones) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(streaming.finalizaciones, [(1,), (5,)])

    def test_stt_lento_overflow_detectable_y_pipeline_recupera(self):
        mic = MicSinHardware(capacidad=3)
        frases = FrasesControladas(bloquear=True)
        app, _, _, _, sesion, _ = self.app(mic=mic, frases=frases)
        app.iniciar_grabacion()
        mic.producir(1)
        mic.producir(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        for valor in (2, 3, 4, 5, 0):
            mic.producir(valor)
        self.assertEqual(mic.estado_captura().frames_descartados, 2)
        self.assertIn("audio=degradado", app._respuesta_estado())
        frases.liberar.set()
        while len(frases.audios) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        estado = mic.estado_captura()
        self.assertEqual(frases.audios, [(1,), (4, 5)])
        self.assertEqual(estado.discontinuidades, 1)
        self.assertEqual(estado.queue_depth, 0)
        self.assertEqual(app.estado, EstadoApp.RECORDING)
        self.assertIn("audio=recuperado-con-perdida", app._respuesta_estado())

    def test_streaming_lento_no_absorbe_audio_posterior_al_gap(self):
        mic = MicSinHardware(capacidad=3)
        streaming = StreamingControlado(bloquear=True)
        app, _, _, _, sesion, _ = self.app(
            mic=mic, modo="streaming", streaming=streaming)
        app.iniciar_grabacion()
        mic.producir(1)
        self.assertTrue(streaming.proceso_iniciado.wait(2))
        for valor in (2, 3, 4, 5, 0):
            mic.producir(valor)
        streaming.liberar.set()
        while len(streaming.finalizaciones) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(streaming.finalizaciones, [(1,), (4, 5)])
        self.assertEqual(mic.estado_captura().frames_descartados, 2)
        self.assertEqual(mic.estado_captura().discontinuidades, 1)

    def test_sink_lento_produce_misma_senal_y_recupera(self):
        mic = MicSinHardware(capacidad=3)
        inyector = SalidaControlada(bloquear=True)
        app, frases, _, _, sesion, _ = self.app(
            mic=mic, inyector=inyector)
        app.iniciar_grabacion()
        mic.producir(1)
        mic.producir(0)
        self.assertTrue(inyector.escritura_iniciada.wait(2))
        for valor in (2, 3, 4, 5, 0):
            mic.producir(valor)
        self.assertEqual(mic.estado_captura().frames_descartados, 2)
        inyector.liberar.set()
        while len(frases.audios) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(frases.audios, [(1,), (4, 5)])
        self.assertEqual(mic.estado_captura().discontinuidades, 1)
        self.assertEqual(app.estado, EstadoApp.RECORDING)

    def test_stop_drena_gap_de_la_generacion_sin_mezclar(self):
        mic = MicSinHardware(capacidad=3)
        frases = FrasesControladas(bloquear=True)
        app, _, _, _, sesion, _ = self.app(mic=mic, frases=frases)
        app.iniciar_grabacion()
        generacion = app.generacion_activa
        mic.producir(1)
        mic.producir(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        for valor in (2, 3, 4, 5, 0):
            mic.producir(valor)
        self.assertTrue(app.detener_grabacion())
        self.assertEqual(app.estado, EstadoApp.STOPPING)
        frases.liberar.set()
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(frases.audios, [(1,), (4, 5)])
        estado = mic.estado_captura()
        self.assertEqual(estado.generacion, generacion)
        self.assertEqual(estado.frames_descartados, 2)
        self.assertEqual(estado.discontinuidades, 1)

    def test_overflow_de_generacion_anterior_no_contamina_restart(self):
        mic = MicSinHardware(capacidad=3)
        frases = FrasesControladas(bloquear=True)
        app, _, _, _, sesion, _ = self.app(mic=mic, frases=frases)
        app.iniciar_grabacion()
        generacion_a = app.generacion_activa
        mic.producir(1)
        mic.producir(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        for valor in (2, 3, 4, 5, 0):
            mic.producir(valor)
        app.detener_grabacion()
        self.assertTrue(app.iniciar_grabacion())
        generacion_b = app.generacion_activa
        self.assertNotEqual(generacion_a, generacion_b)
        mic.producir(101)
        mic.producir(0)
        frases.liberar.set()
        while len(frases.audios) < 2:
            sesion.escrito.clear()
            self.assertTrue(sesion.escrito.wait(2))
        self.assertEqual(frases.audios, [(1,), (101,)])
        self.assertEqual(sesion.textos, ["U:101"])
        estado = mic.estado_captura()
        self.assertEqual(estado.generacion, generacion_b)
        self.assertEqual(estado.frames_descartados, 0)
        self.assertEqual(estado.discontinuidades, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

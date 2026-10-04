"""Parciales STT en vivo para GuionAR (Phase 8B), sólo con STT falso.

No carga Whisper, CUDA, PortAudio, micrófono ni hotkeys: el transcriptor,
el micrófono y el segmentador son los fakes deterministas de lifecycle.
"""

import contextlib
import io
import threading
import time
import unittest

import numpy as np

from parlar.app import EstadoApp
from parlar.cliente_guionar import CONECTADO, DESCONECTADO
from parlar.parcial_stt import ProgramadorParciales, TurnoModelo
from tests.test_lifecycle import FrasesFalsas, SalidaFalsa, crear_app


def esperar(condicion, timeout=3.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.005)
    return condicion()


def hilos_parciales():
    return [h for h in threading.enumerate()
            if h.name == "parlar-parciales" and h.is_alive()]


class TranscriptorParcial:
    """Devuelve 'P:<último valor>' del audio; puede bloquear o fallar."""

    def __init__(self, *, bloquear=False, fallar=False, texto=None):
        self.bloquear = bloquear
        self.fallar = fallar
        self.texto = texto
        self.llamadas = []
        self.entro = threading.Event()
        self.liberar = threading.Event()
        self.concurrentes = 0
        self.max_concurrentes = 0
        self._lock = threading.Lock()

    def __call__(self, audio):
        with self._lock:
            self.concurrentes += 1
            self.max_concurrentes = max(self.max_concurrentes,
                                        self.concurrentes)
        try:
            self.llamadas.append(tuple(int(x) for x in audio.tolist()))
            self.entro.set()
            if self.bloquear and not self.liberar.wait(3):
                raise TimeoutError("parcial falso no liberado")
            if self.fallar:
                raise RuntimeError("fallo deliberado del parcial")
            if self.texto is not None:
                return self.texto(audio) if callable(self.texto) else self.texto
            return f"P:{int(audio[-1])}"
        finally:
            with self._lock:
                self.concurrentes -= 1


class Programador:
    """ProgramadorParciales con sample_rate=10: mínimo 8 muestras,
    intervalo 4, ventana 60. Un valor por muestra identifica el audio."""

    def __init__(self, transcriptor=None, habilitado=True):
        self.transcriptor = transcriptor or TranscriptorParcial()
        self.emitidos = []
        self.habilitado = habilitado
        self.turno = TurnoModelo()
        self.p = ProgramadorParciales(
            self.transcriptor, self.emitir, lambda: self.habilitado,
            self.turno, sample_rate=10)

    def emitir(self, token, texto):
        # Sin filtro propio: descartar resultados viejos es tarea del
        # programador (lo verifican los tests de mutación).
        self.emitidos.append((token, texto))
        return True

    def hablar(self, desde, hasta):
        for valor in range(desde, hasta):
            self.p.agregar_audio(np.asarray([valor], dtype=np.float32))


class PruebasProgramador(unittest.TestCase):
    def setUp(self):
        self.programadores = []

    def tearDown(self):
        for prog in self.programadores:
            prog.transcriptor.liberar.set()
            prog.p.cerrar()

    def nuevo(self, **kwargs):
        prog = Programador(**kwargs)
        self.programadores.append(prog)
        return prog

    def test_01_desconectado_cero_trabajo_y_sin_hilo(self):
        prog = self.nuevo(habilitado=False)
        prog.p.abrir_unidad()
        prog.hablar(1, 200)
        self.assertEqual(prog.transcriptor.llamadas, [])
        self.assertEqual(prog.p.estadisticas["agendados"], 0)
        self.assertIsNone(prog.p._hilo)
        self.assertEqual(prog.p._muestras, 0)   # ni siquiera acumula

    def test_02_conectado_habilitado(self):
        prog = self.nuevo()
        prog.p.abrir_unidad()
        prog.hablar(1, 13)
        self.assertTrue(esperar(lambda: prog.emitidos))

    def test_03_sin_voz_cero_trabajo(self):
        prog = self.nuevo()
        prog.hablar(1, 200)   # sin abrir_unidad: no hay voz confirmada
        self.assertEqual(prog.p.estadisticas["agendados"], 0)
        self.assertIsNone(prog.p._hilo)

    def test_04_minimo_de_audio(self):
        prog = self.nuevo()
        prog.p.abrir_unidad()
        prog.hablar(1, 8)     # 7 muestras < mínimo 8
        self.assertEqual(prog.p.estadisticas["agendados"], 0)
        prog.hablar(8, 9)
        self.assertEqual(prog.p.estadisticas["agendados"], 1)

    def test_05_throttle_por_audio_nuevo(self):
        prog = self.nuevo()
        prog.p.abrir_unidad()
        prog.hablar(1, 101)   # 100 muestras, intervalo 4
        self.assertTrue(esperar(lambda: prog.p.esperar_inactivo(0.01)))
        # Primer snapshot al llegar al mínimo (8), luego cada 4: 8..100.
        self.assertEqual(prog.p.estadisticas["agendados"], 24)

    def test_06_08_un_activo_un_pendiente_latest_wins(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(bloquear=True))
        prog.p.abrir_unidad()
        prog.hablar(1, 9)
        self.assertTrue(prog.transcriptor.entro.wait(2))
        prog.hablar(9, 40)    # varios snapshots mientras P1 infiere
        self.assertTrue(prog.p.activo and prog.p.pendiente)
        self.assertGreaterEqual(prog.p.estadisticas["reemplazados"], 5)
        prog.transcriptor.liberar.set()
        self.assertTrue(prog.p.esperar_inactivo(2))
        self.assertEqual(len(prog.transcriptor.llamadas), 2)
        self.assertEqual(prog.transcriptor.llamadas[-1][-1], 36)  # el último
        self.assertEqual(prog.transcriptor.max_concurrentes, 1)
        self.assertEqual(prog.p.estadisticas["max_pendientes"], 1)
        self.assertEqual(prog.p.estadisticas["max_activos"], 1)

    def test_09_final_elimina_pendiente_y_descarta_activo(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(bloquear=True))
        prog.p.abrir_unidad()
        prog.hablar(1, 9)
        self.assertTrue(prog.transcriptor.entro.wait(2))
        prog.hablar(9, 20)
        self.assertTrue(prog.p.pendiente)
        prog.p.cerrar_unidad()
        self.assertFalse(prog.p.pendiente)
        prog.transcriptor.liberar.set()
        self.assertTrue(prog.p.esperar_inactivo(2))
        self.assertEqual(len(prog.transcriptor.llamadas), 1)
        self.assertEqual(prog.emitidos, [])

    def test_10_22_final_espera_solo_al_parcial_activo(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(bloquear=True))
        prog.p.abrir_unidad()
        prog.hablar(1, 20)
        self.assertTrue(prog.transcriptor.entro.wait(2))
        orden = []

        def final():
            with prog.turno.final():
                orden.append("final")

        hilo = threading.Thread(target=final)
        hilo.start()
        time.sleep(0.05)
        self.assertEqual(orden, [])          # parcial lento en curso
        self.assertFalse(prog.turno.tomar_parcial())  # final esperando
        prog.transcriptor.liberar.set()
        hilo.join(2)
        self.assertEqual(orden, ["final"])

        # Mientras el final tiene el modelo ningún parcial lo toma.
        prog2 = self.nuevo()
        with prog2.turno.final():
            prog2.p.abrir_unidad()
            prog2.hablar(1, 13)
            self.assertTrue(esperar(
                lambda: prog2.p.estadisticas["sin_turno"] >= 1))
            self.assertEqual(prog2.transcriptor.llamadas, [])

    def test_13_14_dedup_y_correcciones(self):
        textos = iter(["todo ese", "todo ese", "todo ese",
                       "todo ese andamiaje tiene un",
                       "todo ese andamiaje tiene un objetivo",
                       "todo ese andamiaje tiene objetivo"])
        prog = self.nuevo(transcriptor=TranscriptorParcial(
            texto=lambda _a: next(textos)))
        prog.p.abrir_unidad()
        for valor in range(1, 30, 4):
            prog.hablar(valor, valor + 4)
            prog.p.esperar_inactivo(2)
        self.assertEqual([t for _, t in prog.emitidos], [
            "todo ese", "todo ese andamiaje tiene un",
            "todo ese andamiaje tiene un objetivo",
            "todo ese andamiaje tiene objetivo"])
        self.assertEqual(prog.p.estadisticas["deduplicados"], 2)

    def test_15_16_generacion_vieja_descartada(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(bloquear=True))
        prog.p.abrir_unidad()
        prog.hablar(1, 9)
        self.assertTrue(prog.transcriptor.entro.wait(2))
        prog.p.abrir_unidad()          # empezó la utterance siguiente
        self.assertEqual(prog.p._muestras, 0)
        prog.transcriptor.liberar.set()
        self.assertTrue(prog.p.esperar_inactivo(2))
        self.assertEqual(prog.emitidos, [])

    def test_21_excepcion_aislada_y_log_deduplicado(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(fallar=True))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            prog.p.abrir_unidad()
            for valor in range(1, 40, 4):
                prog.hablar(valor, valor + 4)
                prog.p.esperar_inactivo(2)
        self.assertGreaterEqual(prog.p.estadisticas["fallos"], 3)
        self.assertEqual(error.getvalue().count("[parcial]"), 1)
        prog.transcriptor.fallar = False
        prog.hablar(40, 44)
        self.assertTrue(esperar(lambda: prog.emitidos))

    def test_29_30_un_hilo_sin_fuga_ni_crecimiento(self):
        prog = self.nuevo(transcriptor=TranscriptorParcial(bloquear=True))
        for _ in range(5):
            prog.p.abrir_unidad()
            prog.hablar(1, 400)
            self.assertLessEqual(len(hilos_parciales()), 1)
            self.assertLessEqual(prog.p._muestras, 60 + 1)  # ventana
            prog.p.cerrar_unidad()
        self.assertEqual(prog.p.estadisticas["max_pendientes"], 1)
        prog.transcriptor.liberar.set()
        prog.p.cerrar()
        self.assertEqual(hilos_parciales(), [])


class GuionARFalso(SalidaFalsa):
    def __init__(self, conectado=True):
        super().__init__()
        self.estado = CONECTADO if conectado else DESCONECTADO


class FrasesConParcial(FrasesFalsas):
    def __init__(self, transcriptor=None, **kwargs):
        super().__init__(**kwargs)
        self.parcial = transcriptor or TranscriptorParcial()

    def transcribir_parcial(self, audio):
        return self.parcial(audio)


class PruebasApp(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            if app.parciales is not None:
                app.parciales._transcribir.__self__.parcial.liberar.set() \
                    if hasattr(app.parciales._transcribir, "__self__") else None
            if app.estado != EstadoApp.CLOSED:
                app.salir()

    def app(self, *, conectado=True, transcriptor=None, frases=None,
            modo="utterance"):
        frases = frases or FrasesConParcial(transcriptor)
        guionar = GuionARFalso(conectado)
        inyector, sesion = SalidaFalsa(), SalidaFalsa()
        app, mic, *_ = crear_app(modo=modo, frases=frases, guionar=guionar,
                                 inyector=inyector, sesion=sesion)
        self.apps.append(app)
        # Estos tests verifican parciales con ambos destinos activos; la
        # salida exclusiva (Phase 8D) tiene su propia suite.
        app.cfg.guionar_exclusive_output = False
        self.assertIsInstance(app.parciales, ProgramadorParciales)
        app.parciales = ProgramadorParciales(
            frases.transcribir_parcial, app._emitir_parcial,
            app._parciales_habilitados, app._turno_modelo, sample_rate=10)
        return app, mic, frases, guionar, inyector, sesion

    @staticmethod
    def hablar(mic, desde, hasta, pausa=0.002):
        """Frames con ritmo: si llegan todos juntos, latest-wins (correcto)
        los colapsa en un único parcial."""
        for valor in range(desde, hasta):
            mic.enviar(valor)
            time.sleep(pausa)

    @staticmethod
    def p_textos(salida):
        return [t for t in salida.parciales if t.startswith("P:")]

    def test_11_12_23_parcial_solo_guionar_y_final_intacto(self):
        app, mic, frases, guionar, inyector, sesion = self.app()
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 15)
        self.assertTrue(esperar(lambda: len(self.p_textos(guionar)) >= 1))
        self.hablar(mic, 15, 30)
        self.assertTrue(esperar(lambda: len(self.p_textos(guionar)) >= 2))
        mic.enviar(0)
        self.assertTrue(sesion.escrito.wait(2))
        final = "U:" + ",".join(map(str, range(1, 30)))
        self.assertEqual(inyector.textos, [final])
        self.assertEqual(sesion.textos, [final])
        self.assertEqual(guionar.textos, [final])
        self.assertEqual(self.p_textos(inyector), [])
        self.assertEqual(self.p_textos(sesion), [])
        self.assertEqual(inyector.parciales, [])   # nada provisional
        self.assertNotIn(final, guionar.parciales)

    def test_01_app_desconectada_cero_inferencias(self):
        app, mic, frases, guionar, *_ = self.app(conectado=False)
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 60)
        mic.enviar(0)
        self.assertTrue(esperar(lambda: guionar.textos))
        self.assertEqual(frases.parcial.llamadas, [])
        self.assertIsNone(app.parciales._hilo)

    def test_17_18_disconnect_y_reconnect_sin_replay(self):
        transcriptor = TranscriptorParcial(bloquear=True)
        app, mic, frases, guionar, *_ = self.app(transcriptor=transcriptor)
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 20)
        self.assertTrue(transcriptor.entro.wait(2))
        guionar.estado = DESCONECTADO        # GuionAR se cierra
        self.hablar(mic, 20, 40)
        transcriptor.liberar.set()
        self.assertTrue(app.parciales.esperar_inactivo(2))
        self.assertEqual(self.p_textos(guionar), [])   # activo descartado
        llamadas = len(transcriptor.llamadas)
        self.hablar(mic, 40, 60)
        time.sleep(0.05)
        self.assertEqual(len(transcriptor.llamadas), llamadas)

        guionar.estado = CONECTADO           # vuelve: sólo audio nuevo
        self.hablar(mic, 100, 113)
        self.assertTrue(esperar(lambda: self.p_textos(guionar)))
        self.assertTrue(all(min(c) >= 100 for c in transcriptor.llamadas[llamadas:]))
        mic.enviar(0)
        self.assertTrue(esperar(lambda: guionar.textos))

    def test_19_cancel_esc_invalida_sin_parcial_tardio(self):
        transcriptor = TranscriptorParcial(bloquear=True)
        app, mic, frases, guionar, inyector, sesion = self.app(
            transcriptor=transcriptor)
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 20)
        self.assertTrue(transcriptor.entro.wait(2))
        self.assertTrue(app.cancelar_grabacion())
        self.assertFalse(app.parciales.pendiente)
        transcriptor.liberar.set()
        self.assertTrue(app.parciales.esperar_inactivo(2))
        time.sleep(0.05)
        self.assertEqual(self.p_textos(guionar), [])
        self.assertEqual(inyector.textos, [])
        self.assertEqual(app.estado, EstadoApp.IDLE)

    def test_20_shutdown_con_parcial_activo(self):
        transcriptor = TranscriptorParcial(bloquear=True)
        app, mic, *_ = self.app(transcriptor=transcriptor)
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 20)
        self.assertTrue(transcriptor.entro.wait(2))
        threading.Timer(0.1, transcriptor.liberar.set).start()
        inicio = time.monotonic()
        app.salir()
        self.assertLess(time.monotonic() - inicio, 3)
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertTrue(esperar(lambda: hilos_parciales() == []))

    def test_10_final_prioritario_no_espera_parciales_en_cola(self):
        transcriptor = TranscriptorParcial(bloquear=True)
        app, mic, frases, guionar, inyector, _ = self.app(
            transcriptor=transcriptor)
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 40)
        self.assertTrue(transcriptor.entro.wait(2))
        mic.enviar(0)                         # VAD cierra la unidad
        time.sleep(0.05)
        self.assertEqual(inyector.textos, [])  # espera al parcial activo
        self.assertFalse(app.parciales.pendiente)
        transcriptor.liberar.set()
        self.assertTrue(esperar(lambda: inyector.textos))
        self.assertEqual(len(transcriptor.llamadas), 1)  # pendiente no corrió
        self.assertEqual(self.p_textos(guionar), [])

    def test_25_26_continuo_cada_unidad_con_su_lifecycle(self):
        app, mic, frases, guionar, inyector, _ = self.app()
        eventos = []
        enviar_parcial = guionar.enviar_parcial
        escribir = guionar.escribir_texto
        guionar.enviar_parcial = lambda t: (eventos.append(("p", t)),
                                            enviar_parcial(t))[1]
        guionar.escribir_texto = lambda t, **k: (eventos.append(("t", t)),
                                                 escribir(t, **k))[1]
        self.assertTrue(app.iniciar_grabacion())
        for base in (100, 200, 300):
            self.hablar(mic, base, base + 20)
            self.assertTrue(esperar(lambda b=base: any(
                t.startswith("P:") and b <= int(t[2:]) < b + 20
                for t in self.p_textos(guionar))))
            mic.enviar(0)
            self.assertTrue(esperar(lambda b=base: any(
                t.lstrip().startswith(f"U:{b}") for t in inyector.textos)))
        self.assertEqual(len(inyector.textos), 3)
        # Ningún parcial de una unidad sale después de su final, y cada
        # unidad sólo muestra parciales de su propio audio.
        for base in (100, 200, 300):
            final = next(i for i, (k, t) in enumerate(eventos)
                         if k == "t" and t.startswith(f"U:{base}"))
            tardios = [t for k, t in eventos[final + 1:] if k == "p"
                       and t.startswith("P:") and base <= int(t[2:]) < base + 20]
            self.assertEqual(tardios, [])
        self.assertEqual(app.parciales.estadisticas["max_pendientes"], 1)

    def test_24_27_ptt_y_doble_toque_continuo(self):
        for gesto in ("ptt", "doble"):
            with self.subTest(gesto=gesto):
                app, mic, frases, guionar, inyector, _ = self.app()
                control = app.gesto_dictado
                control.presionar()
                if gesto == "doble":
                    control.soltar()
                    control.presionar()
                    control.soltar()
                    self.assertTrue(esperar(lambda: control.continuo_activo, 1))
                self.assertTrue(esperar(lambda: mic.generacion is not None))
                self.hablar(mic, 1, 20)
                self.assertTrue(esperar(lambda: self.p_textos(guionar)))
                mic.enviar(0)
                self.assertTrue(esperar(lambda: inyector.textos))
                self.assertEqual(self.p_textos(inyector), [])
                if gesto == "ptt":
                    control.soltar()
                app.salir()

    def test_21_23_parciales_rotos_no_afectan_final(self):
        transcriptor = TranscriptorParcial(fallar=True)
        app, mic, frases, guionar, inyector, sesion = self.app(
            transcriptor=transcriptor)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertTrue(app.iniciar_grabacion())
            for unidad in range(3):
                self.hablar(mic, 1, 30)
                mic.enviar(0)
                self.assertTrue(esperar(
                    lambda u=unidad: len(inyector.textos) == u + 1))
        self.assertEqual(len(sesion.textos), 3)
        self.assertEqual(self.p_textos(guionar), [])
        self.assertLessEqual(error.getvalue().count("[parcial]"), 1)
        self.assertEqual(app.estado, EstadoApp.RECORDING)

    def test_28_concurrencia_final_y_parciales(self):
        transcriptor = TranscriptorParcial()
        frases = FrasesConParcial(transcriptor)
        enMod = []
        original = frases.transcribir

        def final_observado(audio):
            enMod.append(transcriptor.concurrentes)
            return original(audio)

        frases.transcribir = final_observado
        app, mic, _, guionar, inyector, _ = self.app(frases=frases)
        self.assertTrue(app.iniciar_grabacion())
        for _ in range(10):
            self.hablar(mic, 1, 25)
            mic.enviar(0)
        self.assertTrue(esperar(lambda: len(inyector.textos) == 10, 5))
        # El final nunca corrió a la vez que un parcial.
        self.assertEqual(set(enMod), {0})
        self.assertEqual(transcriptor.max_concurrentes, 1)
        self.assertLessEqual(len(hilos_parciales()), 1)

    def test_streaming_no_duplica_parciales_propios(self):
        app, mic, frases, guionar, *_ = self.app(modo="streaming")
        self.assertTrue(app.iniciar_grabacion())
        self.hablar(mic, 1, 40)
        mic.enviar(0)
        self.assertTrue(esperar(lambda: guionar.textos))
        self.assertEqual(frases.parcial.llamadas, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

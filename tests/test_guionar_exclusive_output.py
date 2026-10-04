"""Salida exclusiva a GuionAR (Phase 8D): routing por unidad, sin fugas.

Sólo fakes: sin micrófono, Whisper, CUDA ni hotkeys físicas.
"""

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import parlar.config as config_mod
from parlar.app import EstadoApp
from parlar.cliente_guionar import CONECTADO, DESCONECTADO
from parlar.config import Config
from parlar.coordinador_salida import CoordinadorSalida
from parlar.entrega import EstadoEntrega
from parlar.settings_backend import (
    construir_configuracion_candidata,
    requiere_reinicio,
    snapshot_configuracion,
)
from parlar.settings_window import (
    ARQUITECTURA_SETTINGS,
    snapshot_desde_valores,
    valores_desde_snapshot,
)
from tests.test_lifecycle import (
    FrasesFalsas, ProcesadorFalso, SalidaFalsa, crear_app,
)
from tests.test_live_partials import FrasesConParcial, GuionARFalso


def esperar(condicion, timeout=3.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.005)
    return condicion()


class GuionARQueFalla(GuionARFalso):
    def escribir_texto(self, texto, registrar=True):
        self.intentos = getattr(self, "intentos", 0) + 1
        return False


class InyectorConTeclas(SalidaFalsa):
    def __init__(self):
        super().__init__()
        self.teclas = []

    def nueva_linea(self, cantidad=1):
        self.teclas.append(("nueva_linea", cantidad))
        return True

    def presionar_enter(self):
        self.teclas.append(("enter",))
        return True

    def borrar_ultima_oracion(self):
        self.teclas.append(("borrar",))
        return True


class ProcesadorComandos(ProcesadorFalso):
    """'U:9' dispara Enter; cualquier otra frase es texto."""

    def procesar_frase(self, texto):
        if texto == "U:9":
            return SimpleNamespace(comando="enviar", carga="", texto="")
        return SimpleNamespace(comando=None, carga="", texto=texto)


class Base(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            if app.estado != EstadoApp.CLOSED:
                app.salir()

    def app(self, *, conectado=True, exclusiva=True, integracion=True,
            guionar=None, frases=None, proc=None, inyector=None):
        guionar = guionar or GuionARFalso(conectado)
        inyector = inyector or InyectorConTeclas()
        sesion = SalidaFalsa()
        app, mic, frases, _s, _i, _g, _se, fabrica = crear_app(
            frases=frases or FrasesFalsas(), guionar=guionar,
            inyector=inyector, sesion=sesion, proc=proc)
        app.cfg.guionar = integracion
        app.cfg.guionar_exclusive_output = exclusiva
        self.apps.append(app)
        return SimpleNamespace(app=app, mic=mic, frases=frases, guionar=guionar,
                               inyector=inyector, sesion=sesion,
                               fabrica=fabrica)

    def empezar_unidad(self, c, *valores):
        """Abre la unidad y espera a que el worker fije su ruta."""
        for valor in valores:
            c.mic.enviar(valor)
        self.assertTrue(esperar(lambda: c.fabrica.instancias
                                and c.fabrica.instancias[-1].en_voz))
        self.assertTrue(esperar(
            lambda: len(c.fabrica.instancias[-1].valores) >= len(valores)))

    def cerrar_unidad(self, c, esperados):
        c.mic.enviar(0)
        self.assertTrue(esperar(lambda: len(c.sesion.textos) >= esperados))


class PruebasRouting(Base):
    def test_01_02_15_16_exclusiva_conectada(self):
        c = self.app()
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 4, 5)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, [])          # 1 no inyecta
        self.assertEqual(c.guionar.textos, ["U:4,5"])    # 2 GuionAR recibe
        self.assertEqual(c.sesion.textos, ["U:4,5"])     # 15 historial
        self.assertEqual(c.frases.audios, [(4, 5)])      # 16 STT/proceso
        self.assertEqual(c.app.ultima_entrega.inyector, EstadoEntrega.SKIPPED)

    def test_04_exclusiva_off_inyecta_como_antes(self):
        c = self.app(exclusiva=False)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 7)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, ["U:7"])
        self.assertEqual(c.guionar.textos, ["U:7"])

    def test_05_desconectado_al_inicio_salida_normal(self):
        c = self.app(conectado=False)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 7)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, ["U:7"])

    def test_20_integracion_off_salida_normal(self):
        c = self.app(integracion=False)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 7)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, ["U:7"])

    def test_06_disconnect_a_mitad_no_fuga_al_inyector(self):
        c = self.app()
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 3)
        c.guionar.estado = DESCONECTADO
        c.mic.enviar(4)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, [])
        self.assertEqual(c.sesion.textos, ["U:3,4"])

    def test_07_connect_a_mitad_conserva_ruta_normal(self):
        c = self.app(conectado=False)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 3)
        c.guionar.estado = CONECTADO
        c.mic.enviar(4)
        self.cerrar_unidad(c, 1)
        self.assertEqual(c.inyector.textos, ["U:3,4"])

    def test_08_09_10_continuo_reevalua_por_unidad(self):
        c = self.app()
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 1)
        self.cerrar_unidad(c, 1)                 # u1 exclusiva
        c.guionar.estado = DESCONECTADO
        self.empezar_unidad(c, 2)
        self.cerrar_unidad(c, 2)                 # u2 normal
        c.guionar.estado = CONECTADO
        self.empezar_unidad(c, 3)
        self.cerrar_unidad(c, 3)                 # u3 exclusiva otra vez
        self.assertEqual([t.strip() for t in c.inyector.textos], ["U:2"])
        self.assertEqual(c.guionar.textos, ["U:1", "U:2", "U:3"])

    def test_11_12_ptt_y_doble_toque(self):
        for gesto in ("ptt", "doble"):
            with self.subTest(gesto=gesto):
                c = self.app()
                control = c.app.gesto_dictado
                control.presionar()
                if gesto == "doble":
                    control.soltar()
                    control.presionar()
                    control.soltar()
                    self.assertTrue(esperar(lambda: control.continuo_activo, 1))
                self.assertTrue(esperar(lambda: c.mic.generacion is not None))
                self.empezar_unidad(c, 6)
                self.cerrar_unidad(c, 1)
                self.assertEqual(c.inyector.textos, [])
                self.assertEqual(c.guionar.textos, ["U:6"])
                if gesto == "ptt":
                    control.soltar()
                c.app.salir()

    def test_13_cancel_no_inyecta_y_reevalua(self):
        frases = FrasesFalsas(bloquear=True)
        c = self.app(frases=frases)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 3)
        c.mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        self.assertTrue(c.app.cancelar_grabacion())
        frases.liberar.set()
        time.sleep(0.1)
        self.assertEqual(c.inyector.textos, [])
        self.assertFalse(c.app.salida.ruta_exclusiva)
        # La sesión siguiente, ya sin GuionAR, vuelve a la salida normal.
        frases.bloquear = False
        c.guionar.estado = DESCONECTADO
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 8)
        c.mic.enviar(0)
        self.assertTrue(esperar(lambda: c.inyector.textos))
        self.assertEqual(c.inyector.textos, ["U:8"])

    def test_14_fallo_de_guionar_sin_fallback(self):
        guionar = GuionARQueFalla()
        c = self.app(guionar=guionar)
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 5)
        self.cerrar_unidad(c, 1)
        self.assertEqual(guionar.intentos, 1)
        self.assertEqual(c.inyector.textos, [])
        resultado = c.app.ultima_entrega
        self.assertEqual(resultado.inyector, EstadoEntrega.SKIPPED)
        self.assertEqual(resultado.guionar, EstadoEntrega.FAILED)

    def test_comandos_de_teclado_omitidos_en_exclusiva(self):
        c = self.app(proc=ProcesadorComandos())
        c.app.cfg.comando_enviar = True
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 9)
        c.mic.enviar(0)
        self.assertTrue(esperar(lambda: c.app.ui.trabajo_finalizado.is_set()))
        time.sleep(0.05)
        self.assertEqual(c.inyector.teclas, [])

    def test_03_23_parciales_solo_guionar(self):
        c = self.app(frases=FrasesConParcial())
        from parlar.parcial_stt import ProgramadorParciales
        c.app.parciales = ProgramadorParciales(
            c.frases.transcribir_parcial, c.app._emitir_parcial,
            c.app._parciales_habilitados, c.app._turno_modelo, sample_rate=10)
        self.assertTrue(c.app.iniciar_grabacion())
        for valor in range(1, 20):
            c.mic.enviar(valor)
            time.sleep(0.002)
        self.assertTrue(esperar(lambda: any(
            t.startswith("P:") for t in c.guionar.parciales)))
        c.mic.enviar(0)
        self.assertTrue(esperar(lambda: c.guionar.textos))
        self.assertEqual(c.inyector.textos, [])
        self.assertEqual(c.inyector.parciales, [])

    def test_25_sin_hilos_ni_timers_nuevos(self):
        c = self.app()
        self.assertTrue(c.app.iniciar_grabacion())
        self.empezar_unidad(c, 1)
        antes = {h.name for h in threading.enumerate()}
        self.cerrar_unidad(c, 1)
        self.empezar_unidad(c, 2)
        self.cerrar_unidad(c, 2)
        despues = {h.name for h in threading.enumerate()}
        self.assertLessEqual(despues - antes, set())


class PruebasCoordinador(unittest.TestCase):
    def test_ruta_fijada_al_iniciar_y_liberada_al_cerrar(self):
        inyector, guionar, sesion = SalidaFalsa(), SalidaFalsa(), SalidaFalsa()
        sesion.es_nulo = True
        c = CoordinadorSalida(inyector, guionar, sesion)
        c.iniciar_unidad(1, exclusiva=True)
        c.entregar_texto("hola")
        c.finalizar_unidad()
        c.iniciar_unidad(2)
        c.entregar_texto("chau")
        self.assertEqual(inyector.textos, ["chau"])
        self.assertEqual(guionar.textos, ["hola", "chau"])
        c.iniciar_unidad(3, exclusiva=True)
        c.cancelar_unidad()
        self.assertFalse(c.ruta_exclusiva)


class PruebasConfigYSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "config.json"
        self.parche = mock.patch.object(config_mod, "CONFIG_FILE", self.ruta)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        self.tmp.cleanup()

    def escribir(self, datos):
        self.ruta.write_text(json.dumps(datos), encoding="utf-8")

    def test_17_default_true_y_config_vieja(self):
        self.assertTrue(Config().guionar_exclusive_output)
        self.escribir({"schema_version": 1, "guionar": True,
                       "guionar_explicit": True, "guionar_socket": "/x.sock"})
        cfg = Config.load()
        self.assertTrue(cfg.guionar_exclusive_output)
        self.assertEqual(cfg.guionar_socket, "/x.sock")     # 22
        self.assertTrue(cfg.guionar_explicit)                # 21

    def test_18_false_persistido_se_respeta_y_rollback(self):
        self.escribir({"schema_version": 1, "guionar_exclusive_output": False})
        self.assertFalse(Config.load().guionar_exclusive_output)
        self.escribir({"schema_version": 1,
                       "extras": {"guionar_exclusive_output": False}})
        cfg = Config.load()
        self.assertFalse(cfg.guionar_exclusive_output)
        self.assertNotIn("guionar_exclusive_output", cfg.extras)

    def test_19_21_22_settings_guarda_sin_tocar_lo_demas(self):
        base = Config(guionar_explicit=True, guionar=False,
                      guionar_socket="/run/user/1/g.sock")
        snapshot = snapshot_configuracion(base)
        valores = dataclasses.replace(valores_desde_snapshot(snapshot),
                                      guionar_exclusive_output=False)
        candidato = snapshot_desde_valores(snapshot, valores)
        cfg = construir_configuracion_candidata(base, candidato)
        cfg.save()
        guardado = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertIs(guardado["guionar_exclusive_output"], False)
        self.assertTrue(guardado["guionar_explicit"])
        self.assertFalse(guardado["guionar"])
        self.assertEqual(guardado["guionar_socket"], "/run/user/1/g.sock")
        self.assertFalse(requiere_reinicio(snapshot, candidato))
        otro = dataclasses.replace(candidato, mode="streaming")
        self.assertTrue(requiere_reinicio(snapshot, otro))
        from parlar.settings_window import ControlSettings
        control = ControlSettings(base, snapshot,
                                  persistir=lambda *_: None)
        self.assertTrue(control.esta_sucio(valores))   # se puede guardar
        guionar = next(p for p in ARQUITECTURA_SETTINGS.pestañas
                       if p.nombre == "GuionAR")
        self.assertIn("guionar_exclusive_output", guionar.campos)

    def test_cambio_en_vivo_afecta_la_siguiente_unidad(self):
        self.escribir({"schema_version": 1})
        base = Base()
        base.setUp()
        try:
            c = base.app()
            c.app._config_en_vivo = True
            self.assertTrue(c.app.iniciar_grabacion())
            base.empezar_unidad(c, 1)
            # Settings guarda a mitad de la unidad: no cambia su ruta.
            self.escribir({"schema_version": 1,
                           "guionar_exclusive_output": False})
            base.cerrar_unidad(c, 1)
            base.empezar_unidad(c, 2)
            base.cerrar_unidad(c, 2)
            self.assertEqual([t.strip() for t in c.inyector.textos], ["U:2"])
            self.assertFalse(c.app.cfg.guionar_exclusive_output)
        finally:
            base.tearDown()

    def test_tests_nunca_leen_el_config_del_usuario(self):
        base = Base()
        base.setUp()
        try:
            c = base.app()
            self.assertFalse(c.app._config_en_vivo)
            with mock.patch("parlar.app.os.stat") as stat:
                c.app._preferencia_exclusiva()
            stat.assert_not_called()
        finally:
            base.tearDown()


if __name__ == "__main__":
    unittest.main(verbosity=2)

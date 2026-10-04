import dataclasses
import subprocess
import sys
import unittest

from parlar.config import Config, ErrorConfiguracion
from parlar.hotkey import (
    ATAJO_PREDETERMINADO,
    CapturaAtajo,
    ErrorAtajo,
    SesionCapturaAtajo,
    etiqueta_atajo,
    parsear_atajo,
    token_desde_evento_tk,
    validar_atajo_principal,
)
from parlar.settings_backend import (
    ErrorSuspensionAtajo,
    SuspensionHotkeyProductivo,
    snapshot_configuracion,
)
from parlar.settings_window import (
    ControlSettings,
    valores_desde_snapshot,
)


class PruebasModeloAtajo(unittest.TestCase):
    def test_default_interno_tiene_label_humano(self):
        self.assertEqual(
            etiqueta_atajo(ATAJO_PREDETERMINADO),
            "Ctrl derecho + Meta derecha",
        )

    def test_config_existente_se_parsea(self):
        atajo = parsear_atajo("<ctrl>+<alt>+d")
        self.assertEqual(atajo.persistido, "<ctrl>+<alt>+d")
        self.assertEqual(atajo.etiqueta, "Ctrl + Alt + D")

    def test_normaliza_orden_sin_usar_label(self):
        atajo = parsear_atajo("x+<shift_r>+<ctrl_l>")
        self.assertEqual(
            atajo.persistido,
            "<ctrl_l>+<shift_r>+x",
        )

    def test_preserva_lados(self):
        izquierda = parsear_atajo("<ctrl_l>+<shift_l>")
        derecha = parsear_atajo("<ctrl_r>+<shift_r>")
        self.assertNotEqual(izquierda.persistido, derecha.persistido)
        self.assertIn("izquierdo", izquierda.etiqueta)
        self.assertIn("derecho", derecha.etiqueta)

    def test_modifier_only_es_valido(self):
        atajo = validar_atajo_principal("<cmd_r>+<ctrl_r>")
        self.assertEqual(atajo.persistido, ATAJO_PREDETERMINADO)

    def test_default_anterior_sigue_siendo_valido_para_configs_existentes(self):
        atajo = validar_atajo_principal("<ctrl_r>+<shift_r>")
        self.assertEqual(atajo.persistido, "<ctrl_r>+<shift_r>")

    def test_vacio_rechazado(self):
        with self.assertRaisesRegex(ErrorAtajo, "vacío"):
            validar_atajo_principal("")

    def test_sintaxis_invalida_rechazada(self):
        with self.assertRaisesRegex(ErrorAtajo, "Sintaxis"):
            validar_atajo_principal("ctrl+shift")

    def test_escape_rechazado_en_cualquier_chord(self):
        with self.assertRaisesRegex(ErrorAtajo, "Escape"):
            validar_atajo_principal("<ctrl>+<esc>")

    def test_conflicto_con_salida_ignora_lado_de_modificadores(self):
        with self.assertRaisesRegex(ErrorAtajo, "salir"):
            validar_atajo_principal("<ctrl_r>+<alt_r>+q")

    def test_conflicto_interno_configurable_rechazado(self):
        with self.assertRaisesRegex(ErrorAtajo, "reservada"):
            validar_atajo_principal(
                "<ctrl>+x", reservados=("<ctrl_l>+x",))

    def test_tecla_ordinaria_sola_rechazada(self):
        with self.assertRaisesRegex(ErrorAtajo, "modificador"):
            validar_atajo_principal("a")

    def test_dos_teclas_ordinarias_rechazadas(self):
        with self.assertRaisesRegex(ErrorAtajo, "modificador"):
            validar_atajo_principal("a+b")

    def test_duplicados_logicos_rechazados(self):
        with self.assertRaisesRegex(ErrorAtajo, "duplicadas"):
            validar_atajo_principal("<ctrl>+<ctrl_l>")

    def test_config_valida_hotkey_antes_del_runtime(self):
        with self.assertRaisesRegex(ErrorConfiguracion, "hotkey_toggle"):
            Config(hotkey_toggle="escape").validate()

    def test_label_no_es_sintaxis_persistible(self):
        with self.assertRaises(ErrorAtajo):
            parsear_atajo("Ctrl derecho + Mayús derecha")

    def test_import_es_liviano(self):
        codigo = """
import sys
import parlar.hotkey
assert {'tkinter', 'pynput', 'sounddevice'}.isdisjoint(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


class PruebasCapturaAtajo(unittest.TestCase):
    def test_orden_de_captura_produce_forma_canonica(self):
        captura = CapturaAtajo()
        captura.presionar("<cmd_r>")
        captura.presionar("<ctrl_r>")
        self.assertEqual(captura.candidata.persistido, ATAJO_PREDETERMINADO)

    def test_autorepeat_no_duplica(self):
        captura = CapturaAtajo()
        self.assertEqual(captura.presionar("<ctrl_r>"), "capturada")
        self.assertEqual(captura.presionar("<ctrl_r>"), "repeticion")
        self.assertEqual(len(captura.candidata.teclas), 1)

    def test_nuevo_chord_reemplaza_el_anterior_tras_soltar(self):
        captura = CapturaAtajo()
        captura.presionar("<ctrl_r>")
        captura.presionar("<shift_r>")
        captura.soltar("<shift_r>")
        captura.soltar("<ctrl_r>")
        captura.presionar("<ctrl_l>")
        captura.presionar("k")
        self.assertEqual(captura.candidata.persistido, "<ctrl_l>+k")

    def test_escape_cancela_y_no_es_candidata(self):
        captura = CapturaAtajo()
        captura.presionar("<ctrl_r>")
        self.assertEqual(captura.presionar("<esc>"), "cancelar")
        self.assertEqual(captura.candidata.persistido, "<ctrl_r>")

    def test_tab_se_ignora(self):
        captura = CapturaAtajo()
        self.assertEqual(captura.presionar("<tab>"), "ignorar")
        self.assertIsNone(captura.candidata)

    def test_eventos_tk_preservan_lateralidad(self):
        self.assertEqual(token_desde_evento_tk("Control_L"), "<ctrl_l>")
        self.assertEqual(token_desde_evento_tk("Control_R"), "<ctrl_r>")
        self.assertEqual(token_desde_evento_tk("Shift_R"), "<shift_r>")


class SuspensionFalsa:
    def __init__(self):
        self.adquisiciones = 0
        self.liberaciones = 0
        self.renovaciones = 0

    def adquirir(self, *, runtime_activo):
        self.adquisiciones += int(runtime_activo)

    def renovar(self):
        self.renovaciones += 1
        return True

    def liberar(self):
        self.liberaciones += 1
        return True


class PruebasSesionCapturaAtajo(unittest.TestCase):
    def crear(self, *, validar=validar_atajo_principal):
        suspension = SuspensionFalsa()
        sesion = SesionCapturaAtajo(
            "<ctrl_l>+k", validar=validar, suspension=suspension)
        sesion.iniciar(runtime_activo=True)
        return sesion, suspension

    def test_cancelar_preserva_anterior_y_restaura_listener(self):
        sesion, suspension = self.crear()
        sesion.presionar("<ctrl_r>")
        self.assertEqual(sesion.cancelar(), "<ctrl_l>+k")
        self.assertEqual(suspension.liberaciones, 1)

    def test_usar_devuelve_candidato_y_restaura_listener(self):
        sesion, suspension = self.crear()
        sesion.presionar("<cmd_r>")
        sesion.presionar("<ctrl_r>")
        self.assertEqual(sesion.usar().persistido, ATAJO_PREDETERMINADO)
        self.assertEqual(suspension.liberaciones, 1)

    def test_excepcion_inesperada_restaura_listener(self):
        def fallar(_texto):
            raise RuntimeError("fallo inyectado")

        sesion, suspension = self.crear(validar=fallar)
        sesion.presionar("<ctrl_r>")
        sesion.presionar("<shift_r>")
        with self.assertRaises(RuntimeError):
            sesion.usar()
        self.assertEqual(suspension.liberaciones, 1)

    def test_perdida_de_foco_cancela_y_restaura(self):
        sesion, suspension = self.crear()
        self.assertEqual(sesion.perder_foco(), "<ctrl_l>+k")
        self.assertEqual(suspension.liberaciones, 1)

    def test_cierre_es_idempotente_y_no_deja_lease(self):
        sesion, suspension = self.crear()
        sesion.cerrar()
        sesion.cerrar()
        self.assertEqual(suspension.liberaciones, 1)

    def test_escape_cierra_la_sesion_del_recorder(self):
        sesion, suspension = self.crear()
        self.assertEqual(sesion.presionar("<esc>"), "cancelar")
        self.assertFalse(sesion.activa)
        self.assertEqual(suspension.liberaciones, 1)


class PruebasFlujoSettingsAtajo(unittest.TestCase):
    def setUp(self):
        self.base = Config(hotkey_toggle="<ctrl_l>+k")
        self.snapshot = snapshot_configuracion(self.base)
        self.persistidas = []
        self.control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=lambda _base, candidata:
                self.persistidas.append(candidata),
        )

    def test_usar_combinacion_cambia_solo_candidato(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            hotkey_toggle=ATAJO_PREDETERMINADO,
        )
        candidata = self.control.snapshot_candidato(valores)
        self.assertEqual(candidata.hotkey_toggle, ATAJO_PREDETERMINADO)
        self.assertEqual(self.base.hotkey_toggle, "<ctrl_l>+k")
        self.assertEqual(self.persistidas, [])

    def test_restaurar_default_marca_dirty_sin_persistir(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            hotkey_toggle=ATAJO_PREDETERMINADO,
        )
        self.assertTrue(self.control.esta_sucio(valores))
        self.assertEqual(self.persistidas, [])

    def test_cancelar_settings_despues_de_restore_no_persiste(self):
        cierres = []
        self.control.cancelar(lambda: cierres.append(True))
        self.assertEqual(cierres, [True])
        self.assertEqual(self.persistidas, [])

    def test_cancelar_recorder_conserva_valor_viejo(self):
        anterior = self.snapshot.hotkey_toggle
        captura = CapturaAtajo()
        captura.presionar("<ctrl_r>")
        captura.presionar("<shift_r>")
        self.assertEqual(anterior, self.snapshot.hotkey_toggle)


class PruebasSuspensionHotkey(unittest.TestCase):
    def test_runtime_detenido_no_envia_ipc(self):
        comandos = []
        suspension = SuspensionHotkeyProductivo(
            enviar=lambda comando: comandos.append(comando), token="a" * 24)
        self.assertFalse(suspension.adquirir(runtime_activo=False))
        self.assertEqual(comandos, [])

    def test_adquirir_renovar_y_restaurar_versionado(self):
        comandos = []

        def enviar(comando):
            comandos.append(comando)
            if comando.startswith("hotkey-restaurar"):
                return "OK hotkey restaurado v1"
            return "OK hotkey suspendido v1"

        suspension = SuspensionHotkeyProductivo(
            enviar=enviar, token="b" * 24)
        self.assertTrue(suspension.adquirir(runtime_activo=True))
        self.assertTrue(suspension.renovar())
        self.assertTrue(suspension.liberar())
        self.assertEqual(
            [comando.split()[0] for comando in comandos],
            ["hotkey-suspender", "hotkey-renovar", "hotkey-restaurar"],
        )
        self.assertTrue(all(" v1 " in comando for comando in comandos))

    def test_fallo_de_suspension_impide_captura_competitiva(self):
        suspension = SuspensionHotkeyProductivo(
            enviar=lambda _comando: "ERR comando desconocido", token="c" * 24)
        with self.assertRaises(ErrorSuspensionAtajo):
            suspension.adquirir(runtime_activo=True)

    def test_excepcion_al_liberar_no_deja_cliente_adquirido(self):
        respuestas = iter(("OK hotkey suspendido v1", OSError("socket")))

        def enviar(_comando):
            respuesta = next(respuestas)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        suspension = SuspensionHotkeyProductivo(
            enviar=enviar, token="d" * 24)
        suspension.adquirir(runtime_activo=True)
        self.assertFalse(suspension.liberar())
        self.assertFalse(suspension.adquirida)


if __name__ == "__main__":
    unittest.main()

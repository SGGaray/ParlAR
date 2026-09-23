"""Atajos de teclado globales.

En X11, el atajo de dictado funciona como push-to-talk:
completar la combinación inicia captura y soltar cualquiera de sus
componentes la detiene.

La detección del dictado conserva lateralidad física cuando pynput la
expone, de modo que Ctrl/Shift derechos pueden distinguirse de los
izquierdos.

En Wayland, los compositores bloquean por diseño la captura global de
teclas desde clientes arbitrarios. El camino genérico soportado sigue
siendo usar comandos de ``parlarctl`` desde bindings del compositor.
"""

import sys
from typing import Callable, Hashable, Iterable

from .inyector_salida import detectar_sesion


def _identidad_tecla(tecla: object) -> Hashable:
    """Convierte una tecla pynput en una identidad física estable.

    Los KeyCode de caracteres se comparan por carácter. Para teclas
    especiales/modificadores usamos el virtual key recibido por X11,
    preservando diferencias como Ctrl izquierdo/derecho.
    """
    caracter = getattr(tecla, "char", None)
    if caracter:
        return ("char", caracter.casefold())

    vk = getattr(tecla, "vk", None)

    if vk is None:
        valor = getattr(tecla, "value", None)
        vk = getattr(valor, "vk", None)

    if vk is not None:
        return ("vk", int(vk))

    return ("repr", repr(tecla))


class _EstadoCombo:
    """Detector puro de flancos para una combinación de teclas."""

    def __init__(self, teclas: Iterable[Hashable]):
        self.teclas = frozenset(teclas)

        if not self.teclas:
            raise ValueError(
                "la combinación no puede estar vacía"
            )

        self.presionadas: set[Hashable] = set()
        self.activa = False

    def presionar(self, tecla: Hashable) -> bool:
        """True una sola vez al completarse la combinación."""
        if tecla not in self.teclas:
            return False

        self.presionadas.add(tecla)

        if (
            not self.activa
            and self.teclas <= self.presionadas
        ):
            self.activa = True
            return True

        return False

    def soltar(self, tecla: Hashable) -> bool:
        """True una sola vez al abandonar una combinación activa."""
        if tecla not in self.teclas:
            return False

        estaba_activa = self.activa
        self.presionadas.discard(tecla)

        if (
            estaba_activa
            and not self.teclas <= self.presionadas
        ):
            self.activa = False
            return True

        return False

    def reiniciar(self) -> None:
        self.presionadas.clear()
        self.activa = False


class DaemonAtajos:
    def __init__(
        self,
        combo_dictado: str,
        combo_salir: str,
        al_presionar: Callable[[], None],
        al_soltar: Callable[[], None],
        al_salir: Callable[[], None],
    ):
        self.combo_dictado = combo_dictado
        self.combo_salir = combo_salir
        self.al_presionar = al_presionar
        self.al_soltar = al_soltar
        self.al_salir = al_salir

        self._listener = None
        self._estado_dictado = None
        self._hotkey_salir = None

    def _crear_listener(self, keyboard):
        teclas_dictado = {
            _identidad_tecla(tecla)
            for tecla in keyboard.HotKey.parse(
                self.combo_dictado
            )
        }

        estado_dictado = _EstadoCombo(
            teclas_dictado
        )

        hotkey_salir = keyboard.HotKey(
            keyboard.HotKey.parse(
                self.combo_salir
            ),
            self.al_salir,
        )

        listener = None

        def al_press(tecla):
            # El hotkey de salida conserva el comportamiento estándar
            # de pynput, para el que sí conviene canonicalizar.
            hotkey_salir.press(
                listener.canonical(tecla)
            )

            identidad = _identidad_tecla(tecla)

            if estado_dictado.presionar(
                identidad
            ):
                self.al_presionar()

        def al_release(tecla):
            identidad = _identidad_tecla(tecla)

            if estado_dictado.soltar(
                identidad
            ):
                self.al_soltar()

            hotkey_salir.release(
                listener.canonical(tecla)
            )

        listener = keyboard.Listener(
            on_press=al_press,
            on_release=al_release,
        )

        self._estado_dictado = estado_dictado
        self._hotkey_salir = hotkey_salir

        return listener

    def iniciar(self) -> bool:
        if detectar_sesion() == "wayland":
            print(
                "[atajos] sesión Wayland: "
                "sin push-to-talk global. "
                "Usá bindings del compositor "
                "con `parlarctl`."
            )
            return False

        try:
            from pynput import keyboard
        except Exception as exc:
            print(
                "[atajos] pynput no disponible "
                f"({exc}); usá `parlarctl alternar`.",
                file=sys.stderr,
            )
            return False

        try:
            self._listener = self._crear_listener(
                keyboard
            )
            self._listener.start()

            print(
                "[atajos] mantener="
                f"{self.combo_dictado} "
                f"salir={self.combo_salir}"
            )
            return True
        except Exception as exc:
            print(
                "[atajos] falló el registro "
                f"({exc}); usá `parlarctl alternar`.",
                file=sys.stderr,
            )
            self._listener = None
            return False

    def detener(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass

            self._listener = None

        if self._estado_dictado is not None:
            self._estado_dictado.reiniciar()

        self._estado_dictado = None
        self._hotkey_salir = None

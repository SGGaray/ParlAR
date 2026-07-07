"""Atajos de teclado globales.

X11: pynput GlobalHotKeys funciona directo.
Wayland: los compositores bloquean por diseño la captura global de teclas
desde clientes arbitrarios. El camino soportado ahí es asignar
`parlarctl alternar` a un atajo en la configuración de tu DE
(GNOME/KDE/Hyprland/sway soportan atajos con comandos).
"""

import sys
from typing import Callable

from .inyector_salida import detectar_sesion


class DaemonAtajos:
    def __init__(self, combo_alternar: str, combo_salir: str,
                 al_alternar: Callable[[], None], al_salir: Callable[[], None]):
        self.combo_alternar = combo_alternar
        self.combo_salir = combo_salir
        self.al_alternar = al_alternar
        self.al_salir = al_salir
        self._listener = None

    def iniciar(self) -> bool:
        if detectar_sesion() == "wayland":
            print("[atajos] sesión Wayland: sin captura global. "
                  "Asigná `parlarctl alternar` a un atajo de teclado en tu DE.")
            return False
        try:
            from pynput import keyboard
        except Exception as e:
            print(f"[atajos] pynput no disponible ({e}); usá `parlarctl alternar`.",
                  file=sys.stderr)
            return False
        try:
            self._listener = keyboard.GlobalHotKeys({
                self.combo_alternar: self.al_alternar,
                self.combo_salir: self.al_salir,
            })
            self._listener.start()
            print(f"[atajos] alternar={self.combo_alternar} salir={self.combo_salir}")
            return True
        except Exception as e:
            print(f"[atajos] falló el registro ({e}); usá `parlarctl alternar`.",
                  file=sys.stderr)
            return False

    def detener(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

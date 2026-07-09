"""Inyección de texto a nivel sistema en la ventana con foco.

Backends, auto-seleccionados según tipo de sesión y disponibilidad:
  X11:     xdotool
  Wayland: wtype (protocolo virtual-keyboard), luego ydotool (daemon uinput)
  Respaldo: portapapeles (wl-copy / xclip) + notificación de escritorio

Registra las últimas oraciones inyectadas para poder honrar
"borra la última oración" con retrocesos sintéticos.
"""

import os
import shutil
import subprocess
import sys
from typing import List, Optional


def _cual(nombre: str) -> Optional[str]:
    return shutil.which(nombre)


def detectar_sesion() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    return "x11"


class Inyector:
    def __init__(self, backend: str = "auto", type_delay_ms: int = 1, notify: bool = True):
        self.notify = notify
        self.type_delay_ms = max(0, type_delay_ms)
        self.backend = self._resolver(backend)
        self._registro_oraciones: List[str] = []  # para borrar_ultima
        print(f"[inyector] backend: {self.backend}")

    # ---------------------------------------------------------------- setup

    def _resolver(self, backend: str) -> str:
        if backend != "auto":
            return backend
        sesion = detectar_sesion()
        if sesion == "wayland":
            for cand in ("wtype", "ydotool"):
                if _cual(cand):
                    return cand
        else:
            if _cual("xdotool"):
                return "xdotool"
        # respaldos cruzados
        for cand in ("xdotool", "wtype", "ydotool"):
            if _cual(cand):
                return cand
        return "clipboard"

    # ---------------------------------------------------------------- tipeo

    def escribir_texto(self, texto: str, registrar: bool = True) -> bool:
        if not texto:
            return True
        ok = self._tipear(texto)
        if not ok:
            ok = self._portapapeles(texto)
        if ok and registrar:
            self._registrar(texto)
        return ok

    def _tipear(self, texto: str) -> bool:
        try:
            if self.backend == "xdotool":
                return self._correr(["xdotool", "type", "--clearmodifiers",
                                     "--delay", str(self.type_delay_ms), "--", texto])
            if self.backend == "wtype":
                # wtype maneja saltos de línea bien partiendo con -k Return
                partes = texto.split("\n")
                for i, parte in enumerate(partes):
                    if parte and not self._correr(["wtype", "--", parte]):
                        return False
                    if i < len(partes) - 1 and not self._correr(["wtype", "-k", "Return"]):
                        return False
                return True
            if self.backend == "ydotool":
                return self._correr(["ydotool", "type", "--key-delay",
                                     str(self.type_delay_ms), "--", texto])
            if self.backend == "clipboard":
                return False
        except Exception as e:
            print(f"[inyector] {self.backend} falló: {e}", file=sys.stderr)
        return False

    def retroceso(self, cantidad: int) -> bool:
        if cantidad <= 0:
            return True
        try:
            if self.backend == "xdotool":
                return self._correr(["xdotool", "key", "--clearmodifiers", "--repeat",
                                     str(cantidad), "--repeat-delay", "2", "BackSpace"])
            if self.backend == "wtype":
                args = ["wtype"]
                for _ in range(cantidad):
                    args += ["-k", "BackSpace"]
                return self._correr(args)
            if self.backend == "ydotool":
                # 14 es KEY_BACKSPACE; presión(1)/liberación(0)
                seq = []
                for _ in range(cantidad):
                    seq += ["14:1", "14:0"]
                return self._correr(["ydotool", "key"] + seq)
        except Exception as e:
            print(f"[inyector] retroceso falló: {e}", file=sys.stderr)
        return False

    def presionar_enter(self) -> bool:
        if self.backend == "xdotool":
            return self._correr(["xdotool", "key", "--clearmodifiers", "Return"])
        if self.backend == "wtype":
            return self._correr(["wtype", "-k", "Return"])
        if self.backend == "ydotool":
            return self._correr(["ydotool", "key", "28:1", "28:0"])
        return False

    def nueva_linea(self, cantidad: int = 1) -> bool:
        """Envía `cantidad` pulsaciones reales de Enter.

        Deliberadamente NO se tipea el carácter '\\n' como texto: xdotool
        type lo descarta en silencio en vez de generar un salto de línea, así
        que el comando "nuevo párrafo" no hacía nada visible antes de este
        fix. Enviar la tecla Return explícitamente funciona en los tres
        backends.
        """
        if cantidad <= 0:
            return True
        try:
            if self.backend == "xdotool":
                return self._correr(["xdotool", "key", "--clearmodifiers", "--repeat",
                                     str(cantidad), "--repeat-delay", "2", "Return"])
            if self.backend == "wtype":
                args = ["wtype"]
                for _ in range(cantidad):
                    args += ["-k", "Return"]
                return self._correr(args)
            if self.backend == "ydotool":
                seq = []
                for _ in range(cantidad):
                    seq += ["28:1", "28:0"]
                return self._correr(["ydotool", "key"] + seq)
        except Exception as e:
            print(f"[inyector] nueva_linea falló: {e}", file=sys.stderr)
        return self._portapapeles("\n" * cantidad)

    # ---------------------------------------------------------------- comandos

    def borrar_ultima_oracion(self) -> bool:
        if not self._registro_oraciones:
            return False
        ultima = self._registro_oraciones.pop()
        return self.retroceso(len(ultima))

    def _registrar(self, texto: str):
        self._registro_oraciones.append(texto)
        if len(self._registro_oraciones) > 20:
            self._registro_oraciones.pop(0)

    def reiniciar_registro(self):
        self._registro_oraciones.clear()

    # ------------------------------------------------------- interfaz de salida

    def evento_vad(self, hablando: bool):
        """No-op: el inyector no reacciona a VAD, solo lo hacen las salidas
        que dibujan estado (GuionAR)."""
        pass

    def cerrar(self):
        """No-op: el inyector no mantiene recursos que cerrar."""
        pass

    # ---------------------------------------------------------------- ayudantes

    def _portapapeles(self, texto: str) -> bool:
        herramienta = None
        if detectar_sesion() == "wayland" and _cual("wl-copy"):
            herramienta = ["wl-copy"]
        elif _cual("xclip"):
            herramienta = ["xclip", "-selection", "clipboard"]
        if herramienta is None:
            print(f"[inyector] SIN herramienta de inyección. El texto era:\n{texto}",
                  file=sys.stderr)
            return False
        try:
            subprocess.run(herramienta, input=texto.encode(), check=True, timeout=5)
            self._notificar("ParlAR",
                            "Herramienta de tipeo no disponible. Texto copiado al "
                            "portapapeles, presioná Ctrl+V.")
            return True
        except Exception as e:
            print(f"[inyector] portapapeles falló: {e}", file=sys.stderr)
            return False

    def _notificar(self, titulo: str, cuerpo: str):
        if self.notify and _cual("notify-send"):
            subprocess.run(["notify-send", "-a", "ParlAR", titulo, cuerpo],
                           check=False, timeout=5)

    @staticmethod
    def _correr(cmd: List[str]) -> bool:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0:
            print(f"[inyector] {' '.join(cmd[:2])} rc={r.returncode}: "
                  f"{r.stderr.decode(errors='replace')[:200]}", file=sys.stderr)
        return r.returncode == 0

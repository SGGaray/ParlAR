"""Inyección de texto a nivel sistema en la ventana con foco.

Backends, auto-seleccionados según tipo de sesión y disponibilidad:
  X11:     xdotool
  Wayland: wtype (protocolo virtual-keyboard), luego ydotool (daemon uinput)
  Respaldo: portapapeles (wl-copy / xclip) + notificación de escritorio

Registra las últimas unidades insertadas para poder honrar
"borra la última oración" con retrocesos sintéticos.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

from .entrega import EstadoEntrega, ResultadoSink


_X11_PASTE_SETTLE_S = 0.05


@dataclass(frozen=True)
class EstadoRecuperacionUnidad:
    """Snapshot acotado de la unidad streaming todavía activa."""

    generacion: Optional[int]
    texto_logico: str
    prefijo_insertado: str
    texto_recuperacion: str
    degradada: bool
    recuperacion_disponible: bool


def _cual(nombre: str) -> Optional[str]:
    return shutil.which(nombre)


def detectar_sesion() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return "wayland"
    return "x11"


class Inyector:
    def __init__(self, backend: str = "auto", type_delay_ms: int = 1,
                 notify: bool = True, permitir_return: bool = False):
        self.notify = notify
        self.permitir_return = permitir_return
        self.type_delay_ms = max(0, type_delay_ms)
        self.backend = self._resolver(backend)
        self._registro_oraciones: List[str] = []  # para borrar_ultima
        self._clipboard_unidad = ""
        self._clipboard_unidad_activa = False
        self._clipboard_generacion = None
        self._texto_unidad = ""
        self._prefijo_insertado = ""
        self._unidad_degradada = False
        self._recuperacion_disponible = False
        self._clipboard_forzado_por_salto = False
        self._undo_unidad_diferido = False
        self._efecto_fisico_incierto = False
        self._x11_copia_preparada = False
        self._x11_copia_fallida = False
        self._hubo_intento_fisico = False
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
            if _cual("xdotool") and _cual("xclip"):
                return "xdotool"
        # respaldos cruzados
        for cand in ("xdotool", "wtype", "ydotool"):
            if (_cual(cand)
                    and (cand != "xdotool" or _cual("xclip"))):
                return cand
        return "clipboard"

    # ---------------------------------------------------------------- tipeo

    def escribir_texto(self, texto: str, registrar: bool = True) -> ResultadoSink:
        if not texto:
            return ResultadoSink(EstadoEntrega.SKIPPED)
        if self._clipboard_unidad_activa:
            self._texto_unidad += texto
            if not registrar:
                self._undo_unidad_diferido = True
            if self._unidad_degradada:
                self._clipboard_unidad += texto
                return self._copiar_recuperacion()
        salto_bloqueado = self._contiene_salto(texto) and not self.permitir_return
        if salto_bloqueado:
            if self._clipboard_unidad_activa:
                self._degradar_unidad(texto, forzada_por_salto=True)
                return self._copiar_recuperacion()
            if self._portapapeles(texto):
                return ResultadoSink(EstadoEntrega.COPIED)
            return ResultadoSink(EstadoEntrega.FAILED)
        self._x11_copia_preparada = False
        self._x11_copia_fallida = False
        self._hubo_intento_fisico = False
        ok = self._tipear(texto)
        if ok:
            if self._clipboard_unidad_activa:
                self._prefijo_insertado += texto
            if registrar and not self._efecto_fisico_incierto:
                self._registrar(texto)
            return ResultadoSink(EstadoEntrega.INSERTED)
        if self._x11_copia_fallida and not self._hubo_intento_fisico:
            if self._clipboard_unidad_activa:
                self._degradar_unidad(texto)
            return ResultadoSink(EstadoEntrega.FAILED)
        if self.backend != "clipboard":
            self._marcar_efecto_fisico_incierto()
        contenido = texto
        if self._clipboard_unidad_activa:
            self._degradar_unidad(texto)
            return self._copiar_recuperacion()
        if (self._x11_copia_preparada
                and not self._contiene_salto(texto)):
            self._notificar(
                "ParlAR",
                "El pegado X11 no pudo confirmarse. El texto quedó en el "
                "portapapeles para pegarlo manualmente.",
            )
            return ResultadoSink(EstadoEntrega.COPIED)
        if self._portapapeles(contenido):
            return ResultadoSink(EstadoEntrega.COPIED)
        return ResultadoSink(EstadoEntrega.FAILED)

    def _degradar_unidad(self, texto: str, *, forzada_por_salto=False):
        self._unidad_degradada = True
        self._clipboard_forzado_por_salto = forzada_por_salto
        self._clipboard_unidad = texto
        self._recuperacion_disponible = False

    def _marcar_efecto_fisico_incierto(self):
        # Un subproceso puede fallar después de haber tipeado un prefijo. Como
        # el editor no ofrece transacciones, ningún historial previo es seguro.
        self._registro_oraciones.clear()
        if self._clipboard_unidad_activa:
            self._efecto_fisico_incierto = True

    def _copiar_recuperacion(self) -> ResultadoSink:
        ok = self._portapapeles(self._clipboard_unidad)
        self._recuperacion_disponible = ok
        return ResultadoSink(
            EstadoEntrega.COPIED if ok else EstadoEntrega.FAILED)

    def estado_recuperacion_unidad(self) -> EstadoRecuperacionUnidad:
        return EstadoRecuperacionUnidad(
            generacion=self._clipboard_generacion,
            texto_logico=self._texto_unidad,
            prefijo_insertado=self._prefijo_insertado,
            texto_recuperacion=self._clipboard_unidad,
            degradada=self._unidad_degradada,
            recuperacion_disponible=self._recuperacion_disponible,
        )

    def iniciar_unidad(self, generacion=None):
        # Si el llamador abre otra unidad sin resolver la anterior, la frontera
        # es una cancelación, no una finalización implícita.
        self.cancelar_unidad()
        self._clipboard_unidad_activa = True
        self._clipboard_generacion = generacion

    def finalizar_unidad(self):
        if self._clipboard_unidad_activa and self._undo_unidad_diferido:
            completamente_insertada = (
                bool(self._texto_unidad)
                and not self._unidad_degradada
                and not self._efecto_fisico_incierto
                and self._prefijo_insertado == self._texto_unidad
            )
            if completamente_insertada:
                self._registrar(self._prefijo_insertado)
            elif self._prefijo_insertado or self._efecto_fisico_incierto:
                self._registro_oraciones.clear()
        self._limpiar_unidad()

    def cancelar_unidad(self):
        if (self._clipboard_unidad_activa and self._undo_unidad_diferido
                and (self._prefijo_insertado
                     or self._efecto_fisico_incierto)):
            # Hubo (o pudo haber) texto físico que no constituye una unidad
            # lógica finalizada. Ningún undo futuro puede atravesar esa marca.
            self._registro_oraciones.clear()
        self._limpiar_unidad()

    def _limpiar_unidad(self):
        self._clipboard_unidad = ""
        self._texto_unidad = ""
        self._prefijo_insertado = ""
        self._unidad_degradada = False
        self._recuperacion_disponible = False
        self._clipboard_forzado_por_salto = False
        self._undo_unidad_diferido = False
        self._efecto_fisico_incierto = False
        self._clipboard_unidad_activa = False
        self._clipboard_generacion = None

    def _tipear(self, texto: str) -> bool:
        try:
            if self.backend == "clipboard":
                return False
            partes = re.split(r"\r\n|\r|\n", texto)
            if len(partes) > 1 and not self.permitir_return:
                return False
            for indice, parte in enumerate(partes):
                if parte and not self._tipear_segmento(parte):
                    return False
                if indice < len(partes) - 1 and not self._return_fisico():
                    return False
            return True
        except Exception as e:
            print(f"[inyector] {self.backend} falló: {type(e).__name__}",
                  file=sys.stderr)
        return False

    @staticmethod
    def _contiene_salto(texto: str) -> bool:
        return "\n" in texto or "\r" in texto

    def _tipear_segmento(self, texto: str) -> bool:
        if self.backend == "xdotool":
            if not self._copiar_x11(texto):
                self._x11_copia_fallida = True
                return False
            self._x11_copia_preparada = True
            time.sleep(_X11_PASTE_SETTLE_S)
            self._hubo_intento_fisico = True
            try:
                return self._correr(
                    ["xdotool", "key", "--clearmodifiers", "ctrl+v"])
            finally:
                # El destino puede pedir CLIPBOARD después de recibir Ctrl+V.
                # No lo sobrescribimos con el fragmento siguiente de inmediato.
                time.sleep(_X11_PASTE_SETTLE_S)
        if self.backend == "wtype":
            self._hubo_intento_fisico = True
            return self._correr(["wtype", "--", texto])
        if self.backend == "ydotool":
            self._hubo_intento_fisico = True
            return self._correr(["ydotool", "type", "--key-delay",
                                 str(self.type_delay_ms), "--", texto])
        return False

    def _return_fisico(self) -> bool:
        """Única frontera que puede emitir una pulsación física Return."""
        if not self.permitir_return:
            return False
        self._hubo_intento_fisico = True
        if self.backend == "xdotool":
            return self._correr(
                ["xdotool", "key", "--clearmodifiers", "Return"])
        if self.backend == "wtype":
            return self._correr(["wtype", "-k", "Return"])
        if self.backend == "ydotool":
            return self._correr(["ydotool", "key", "28:1", "28:0"])
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
        try:
            return self._return_fisico()
        except Exception as exc:
            print(f"[inyector] Enter falló: {exc}", file=sys.stderr)
        return False

    def nueva_linea(self, cantidad: int = 1) -> bool:
        """Envía `cantidad` pulsaciones por la frontera física autorizada."""
        if cantidad <= 0:
            return True
        try:
            if all(self._return_fisico() for _ in range(cantidad)):
                return True
        except Exception as e:
            print(f"[inyector] nueva_linea falló: {e}", file=sys.stderr)
        if not self.permitir_return:
            return False
        return self._portapapeles("\n" * cantidad)

    # ---------------------------------------------------------------- comandos

    def borrar_ultima_oracion(self) -> bool:
        if not self._registro_oraciones:
            return False
        ultima = self._registro_oraciones[-1]
        if not self.retroceso(len(ultima)):
            return False
        self._registro_oraciones.pop()
        return True

    def _registrar(self, texto: str):
        self._registro_oraciones.append(texto)
        if len(self._registro_oraciones) > 20:
            self._registro_oraciones.pop(0)

    def reiniciar_registro(self):
        self._registro_oraciones.clear()
        self.cancelar_unidad()

    # ------------------------------------------------------- interfaz de salida

    def evento_vad(self, hablando: bool):
        """No-op: el inyector no reacciona a VAD, solo lo hacen las salidas
        que dibujan estado (GuionAR)."""
        pass

    def cerrar(self):
        """No-op: el inyector no mantiene recursos que cerrar."""
        self.cancelar_unidad()

    # ---------------------------------------------------------------- ayudantes

    def _copiar_x11(self, texto: str) -> bool:
        if not _cual("xclip"):
            print("[inyector] xclip no disponible para inyección X11",
                  file=sys.stderr)
            return False
        return self._copiar_con(
            ["xclip", "-selection", "clipboard"], texto)

    @staticmethod
    def _copiar_con(herramienta: List[str], texto: str) -> bool:
        try:
            subprocess.run(
                herramienta, input=texto.encode(), check=True, timeout=5)
        except Exception as e:
            print(f"[inyector] portapapeles falló: {type(e).__name__}",
                  file=sys.stderr)
            return False
        return True

    def _portapapeles(self, texto: str) -> bool:
        herramienta = None
        if detectar_sesion() == "wayland" and _cual("wl-copy"):
            herramienta = ["wl-copy"]
        elif _cual("xclip"):
            herramienta = ["xclip", "-selection", "clipboard"]
        if herramienta is None:
            print(f"[inyector] SIN herramienta de inyección; texto no insertado "
                  f"({len(texto)} caracteres)",
                  file=sys.stderr)
            return False
        if not self._copiar_con(herramienta, texto):
            return False
        self._notificar("ParlAR",
                        "Herramienta de tipeo no disponible. Texto copiado al "
                        "portapapeles, presioná Ctrl+V.")
        return True

    def _notificar(self, titulo: str, cuerpo: str):
        if self.notify and _cual("notify-send"):
            try:
                resultado = subprocess.run(
                    ["notify-send", "-a", "ParlAR", titulo, cuerpo],
                    check=False, timeout=5)
                if resultado.returncode != 0:
                    print("[inyector] notificación falló: "
                          f"rc={resultado.returncode}", file=sys.stderr)
            except Exception as e:
                # El feedback visual es best-effort: nunca cambia el resultado
                # de una copia ya confirmada ni registra el texto sensible.
                print(f"[inyector] notificación falló: {type(e).__name__}",
                      file=sys.stderr)

    @staticmethod
    def _correr(cmd: List[str]) -> bool:
        r = subprocess.run(cmd, capture_output=True, check=False, timeout=30)
        if r.returncode != 0:
            # stderr pertenece a una herramienta externa y podría repetir el
            # argumento dictado. Solo registramos metadatos no sensibles.
            print(f"[inyector] {' '.join(cmd[:2])} rc={r.returncode}",
                  file=sys.stderr)
        return r.returncode == 0

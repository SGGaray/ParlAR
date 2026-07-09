"""Transcript de sesión: cada texto confirmado se agrega a un archivo local.

Diseño:
- Apagado por defecto. Los dictados pueden ser sensibles; esto es texto en
  reposo sin cifrar. Se activa con `guardar_sesion: true` en la config o el
  flag --guardar-sesion. Si está desactivado, crear_salida_sesion() devuelve
  una SesionNula (no-op).
- Un archivo por corrida del daemon, nombre fijado al arrancar:
  $XDG_DATA_HOME/parlar/sesiones/AAAA-MM-DD_HHMM.txt (fallback ~/.local/share).
- Registra SIEMPRE el texto confirmado, sin importar si la inyección al
  sistema tuvo éxito: es el respaldo, no depende de xdotool/wtype/ydotool.
- Para borrar: los archivos son texto plano en esa carpeta, se borran a mano
  (`rm ~/.local/share/parlar/sesiones/*.txt` o el que corresponda). ParlAR no
  los borra solo.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional


def directorio_sesiones_por_defecto() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "parlar" / "sesiones"
    return Path.home() / ".local" / "share" / "parlar" / "sesiones"


class SalidaSesion:
    """Escribe cada texto confirmado a un archivo de transcript, una línea
    por texto. Nunca lanza excepciones hacia el pipeline de dictado."""

    def __init__(self, directorio: Optional[Path] = None):
        self.directorio = directorio or directorio_sesiones_por_defecto()
        nombre = datetime.now().strftime("%Y-%m-%d_%H%M") + ".txt"
        self.ruta = self.directorio / nombre
        self._archivo = None
        try:
            self.directorio.mkdir(parents=True, exist_ok=True)
            self._archivo = open(self.ruta, "a", encoding="utf-8")
            print(f"[sesion] guardando transcript en: {self.ruta}")
        except OSError as e:
            print(f"[sesion] no se pudo abrir {self.ruta}: {e}")

    # ---------------------------------------------------------- API pública

    def escribir_texto(self, texto: str) -> bool:
        if not texto or self._archivo is None:
            return False
        try:
            self._archivo.write(texto + "\n")
            self._archivo.flush()
            return True
        except OSError as e:
            print(f"[sesion] no se pudo escribir: {e}")
            return False

    def evento_vad(self, hablando: bool):
        pass

    def cerrar(self):
        if self._archivo is not None:
            try:
                self._archivo.close()
            except OSError:
                pass
            self._archivo = None


class SesionNula:
    """No-op cuando --guardar-sesion está desactivado. Mismo contrato."""

    def escribir_texto(self, texto: str) -> bool: return False
    def evento_vad(self, hablando: bool): pass
    def cerrar(self): pass


def crear_salida_sesion(activado: bool, directorio: Optional[Path] = None):
    return SalidaSesion(directorio) if activado else SesionNula()

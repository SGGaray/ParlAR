"""Transcript de sesión: cada texto confirmado se agrega a un archivo local.

Diseño:
- Apagado por defecto. Los dictados pueden ser sensibles; esto es texto en
  reposo sin cifrar. Se activa con `guardar_sesion: true` en la config o el
  flag --guardar-sesion. Si está desactivado, crear_salida_sesion() devuelve
  una SesionNula (no-op).
- Un archivo exclusivo por corrida del daemon, nombre fijado al arrancar:
  $XDG_DATA_HOME/parlar/sesiones/parlar-AAAAMMDD-HHMMSS-microsegundos-token.txt
  (fallback ~/.local/share). El directorio se crea 0700 y el archivo 0600.
- Registra SIEMPRE el texto confirmado, sin importar si la inyección al
  sistema tuvo éxito: es el respaldo, no depende de xdotool/wtype/ydotool.
- Para borrar: los archivos son texto plano en esa carpeta, se borran a mano
  (`rm ~/.local/share/parlar/sesiones/*.txt` o el que corresponda). ParlAR no
  los borra solo.
"""

import os
import secrets
from datetime import datetime, timezone
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
        self.ruta = None
        self._archivo = None
        fd = None
        try:
            creado = not self.directorio.exists()
            self.directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
            if creado:
                self.directorio.chmod(0o700)

            instante = datetime.now(timezone.utc).astimezone().strftime(
                "%Y%m%d-%H%M%S-%f"
            )
            for _ in range(100):
                nombre = f"parlar-{instante}-{secrets.token_hex(4)}.txt"
                candidata = self.directorio / nombre
                try:
                    fd = os.open(
                        candidata,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND,
                        0o600,
                    )
                    self.ruta = candidata
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("no se pudo reservar un transcript exclusivo")

            self._archivo = os.fdopen(fd, "a", encoding="utf-8")
            fd = None
            print(f"[sesion] guardando transcript en: {self.ruta}")
        except OSError as e:
            if fd is not None:
                os.close(fd)
            if self._archivo is None and self.ruta is not None:
                try:
                    self.ruta.unlink()
                except FileNotFoundError:
                    pass
            destino = self.ruta or self.directorio
            print(f"[sesion] no se pudo abrir {destino}: {e}")

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

    es_nulo = True

    def escribir_texto(self, texto: str) -> bool: return False
    def evento_vad(self, hablando: bool): pass
    def cerrar(self): pass


def crear_salida_sesion(activado: bool, directorio: Optional[Path] = None):
    return SalidaSesion(directorio) if activado else SesionNula()

"""Prueba temporal y aislada de dispositivos de entrada.

Esta frontera no conoce ``App`` ni conserva audio.  Abre un único stream de
``sounddevice``, publica solamente un nivel RMS y garantiza que el cierre del
recurso ocurra fuera del callback de PortAudio.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import numpy as np


class ErrorPruebaAudio(RuntimeError):
    """La prueba de micrófono no pudo iniciarse o cerrarse correctamente."""


@dataclass(frozen=True, slots=True)
class EstadoPruebaAudio:
    """Snapshot inmutable que una futura UI puede consultar con seguridad."""

    nivel: float
    activo: bool
    error: str | None


def calcular_nivel_rms(muestras: Any) -> float:
    """Calcula un RMS normalizado para muestras float en el rango ``[-1, 1]``.

    Los valores no finitos se vuelven valores seguros y la salida siempre se
    limita al intervalo ``0.0``–``1.0``.
    """
    valores = np.asarray(muestras, dtype=np.float64)
    if valores.size == 0:
        return 0.0
    valores = np.nan_to_num(
        valores, copy=True, nan=0.0, posinf=1.0, neginf=-1.0)
    np.clip(valores, -1.0, 1.0, out=valores)
    nivel = float(np.sqrt(np.mean(np.square(valores))))
    if not np.isfinite(nivel):
        return 0.0
    return min(1.0, max(0.0, nivel))


def _mensaje_error(contexto: str, exc: BaseException) -> str:
    detalle = str(exc).strip()
    return f"{contexto}: {detalle}" if detalle else contexto


class PruebaMicrofono:
    """Controla un stream temporal sin compartir recursos con ``CapturadorMic``.

    ``sounddevice`` se importa de forma diferida y puede inyectarse en tests.
    Un hilo propietario espera una orden de cierre; así el callback sólo copia
    el nivel o señala el error y nunca cierra PortAudio desde su propio hilo.
    """

    def __init__(self, sounddevice=None, *, sample_rate: int = 16000):
        self._sounddevice = sounddevice
        self._sample_rate = sample_rate
        self._lifecycle_lock = threading.Lock()
        self._estado_lock = threading.Lock()
        self._estado = "idle"
        self._nivel = 0.0
        self._error: str | None = None
        self._error_cierre: BaseException | None = None
        self._stream = None
        self._generacion = 0
        self._solicitar_cierre: threading.Event | None = None
        self._inicio_finalizado: threading.Event | None = None
        self._inicio_exitoso: threading.Event | None = None
        self._cierre_finalizado: threading.Event | None = None
        self._hilo_cierre: threading.Thread | None = None

    def estado(self) -> EstadoPruebaAudio:
        """Devuelve el último nivel y error sin exponer estado mutable."""
        with self._estado_lock:
            return EstadoPruebaAudio(
                nivel=self._nivel,
                activo=self._estado in {"starting", "active"},
                error=self._error,
            )

    def iniciar(self, dispositivo: int | None = None) -> bool:
        """Abre una prueba; devuelve ``False`` si ya existe una en curso."""
        with self._lifecycle_lock:
            with self._estado_lock:
                if self._estado != "idle":
                    return False
                self._estado = "starting"
                self._nivel = 0.0
                self._error = None
                self._error_cierre = None
                self._generacion += 1
                generacion = self._generacion

            try:
                sounddevice = self._obtener_sounddevice()
                stream = sounddevice.InputStream(
                    samplerate=self._sample_rate,
                    device=dispositivo,
                    channels=1,
                    dtype="float32",
                    callback=lambda datos, frames, tiempo, status: self._callback(
                        generacion, datos, frames, tiempo, status),
                )
            except Exception as exc:
                mensaje = _mensaje_error(
                    "no se pudo abrir la prueba de micrófono", exc)
                with self._estado_lock:
                    self._estado = "idle"
                    self._error = mensaje
                raise ErrorPruebaAudio(mensaje) from exc

            solicitar_cierre = threading.Event()
            inicio_finalizado = threading.Event()
            inicio_exitoso = threading.Event()
            cierre_finalizado = threading.Event()
            hilo = threading.Thread(
                target=self._gestionar_cierre,
                args=(
                    generacion,
                    stream,
                    solicitar_cierre,
                    inicio_finalizado,
                    inicio_exitoso,
                    cierre_finalizado,
                ),
                name="parlar-audio-test-close",
                daemon=True,
            )
            with self._estado_lock:
                self._stream = stream
                self._solicitar_cierre = solicitar_cierre
                self._inicio_finalizado = inicio_finalizado
                self._inicio_exitoso = inicio_exitoso
                self._cierre_finalizado = cierre_finalizado
                self._hilo_cierre = hilo
            hilo.start()

            try:
                stream.start()
            except Exception as exc:
                mensaje = _mensaje_error(
                    "no se pudo iniciar la prueba de micrófono", exc)
                self._registrar_error(generacion, mensaje)
                inicio_finalizado.set()
                cierre_finalizado.wait()
                hilo.join()
                raise ErrorPruebaAudio(mensaje) from exc

            inicio_exitoso.set()
            inicio_finalizado.set()
            with self._estado_lock:
                if self._estado == "starting":
                    self._estado = "active"
                    return True
                mensaje = self._error or "falló el callback de audio"

            cierre_finalizado.wait()
            hilo.join()
            raise ErrorPruebaAudio(mensaje)

    def detener(self) -> bool:
        """Cierra sincrónicamente la prueba; repetir la operación es seguro."""
        with self._lifecycle_lock:
            with self._estado_lock:
                if self._estado == "idle":
                    return False
                self._estado = "stopping"
                solicitar_cierre = self._solicitar_cierre
                cierre_finalizado = self._cierre_finalizado
                hilo = self._hilo_cierre
            if solicitar_cierre is not None:
                solicitar_cierre.set()
            if cierre_finalizado is not None:
                cierre_finalizado.wait()
            if hilo is not None and hilo is not threading.current_thread():
                hilo.join()
            with self._estado_lock:
                error_cierre = self._error_cierre
            if error_cierre is not None:
                raise ErrorPruebaAudio(str(error_cierre)) from error_cierre
            return True

    def cancelar(self) -> bool:
        """Alias explícito para cancelar una prueba en curso."""
        return self.detener()

    def cerrar(self) -> bool:
        """Alias de cierre para propietarios de recursos."""
        return self.detener()

    def _obtener_sounddevice(self):
        if self._sounddevice is not None:
            return self._sounddevice
        import sounddevice
        return sounddevice

    def _callback(self, generacion, datos, _frames, _tiempo, status):
        try:
            if status:
                raise RuntimeError(f"sounddevice reportó: {status}")
            nivel = calcular_nivel_rms(datos)
        except Exception as exc:
            self._registrar_error(
                generacion,
                _mensaje_error("falló el callback de audio", exc),
            )
            return

        with self._estado_lock:
            if (
                    generacion == self._generacion
                    and self._estado in {"starting", "active"}):
                self._nivel = nivel

    def _registrar_error(self, generacion: int, mensaje: str) -> None:
        with self._estado_lock:
            if (
                    generacion != self._generacion
                    or self._estado not in {"starting", "active"}):
                return
            self._error = mensaje
            self._estado = "stopping"
            solicitar_cierre = self._solicitar_cierre
        if solicitar_cierre is not None:
            solicitar_cierre.set()

    def _gestionar_cierre(
            self,
            generacion: int,
            stream,
            solicitar_cierre: threading.Event,
            inicio_finalizado: threading.Event,
            inicio_exitoso: threading.Event,
            cierre_finalizado: threading.Event,
    ) -> None:
        solicitar_cierre.wait()
        inicio_finalizado.wait()
        errores = []
        if inicio_exitoso.is_set():
            try:
                stream.stop()
            except Exception as exc:
                errores.append(exc)
        try:
            stream.close()
        except Exception as exc:
            errores.append(exc)

        with self._estado_lock:
            if generacion == self._generacion and self._stream is stream:
                if errores:
                    detalle = "; ".join(
                        _mensaje_error("falló el cierre de audio", error)
                        for error in errores
                    )
                    error_cierre = ErrorPruebaAudio(detalle)
                    self._error_cierre = error_cierre
                    self._error = (
                        f"{self._error}; {detalle}" if self._error else detalle)
                self._stream = None
                self._estado = "idle"
                self._solicitar_cierre = None
                self._inicio_finalizado = None
                self._inicio_exitoso = None
                self._cierre_finalizado = None
                self._hilo_cierre = None
        cierre_finalizado.set()

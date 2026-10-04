"""Parciales STT en vivo para GuionAR, acotados y descartables.

Un parcial es una señal provisional: sólo viaja a GuionAR, nunca al
inyector, al historial ni a la sesión, y puede corregirse en el siguiente.
El texto final de la unidad sigue siendo la única autoridad.

Garantías:
- Sin GuionAR conectado no se acumula audio ni se crea el hilo: cero
  inferencias adicionales.
- Un único hilo, como máximo 1 parcial activo y 1 pendiente (latest-wins).
- El audio de cada parcial es una ventana móvil acotada: el costo no crece
  con la duración de la frase.
- Cada unidad de voz tiene un token; un resultado de una unidad ya cerrada,
  cancelada o reemplazada se descarta.
- ``TurnoModelo`` da prioridad al final: el parcial sólo toma el modelo si
  está libre y no hay un final esperando; el final espera, como mucho, al
  parcial que ya estaba inferenciando.
"""

import os
import sys
import threading
from contextlib import contextmanager

import numpy as np


SAMPLE_RATE = 16000
# Elegidos con benchmark (Whisper small, CUDA float16, beam 1): la frase
# completa crece hasta ~0.65 s por parcial en 25 s de voz; una ventana móvil
# de 4–8 s queda constante en ~0.2–0.3 s con utilidad equivalente. Cada
# 450 ms (~2 parciales/s) deja margen al GPU; 350 ms reemplaza más y 600 ms
# llega tarde. Primer parcial tras 0.8 s de voz.
INTERVALO_S = 0.45
MINIMO_S = 0.8
VENTANA_S = 6.0
_CIERRE_TIMEOUT_S = 2.0
DEBUG = os.environ.get("PARLAR_DEBUG_PARCIALES") == "1"


class TurnoModelo:
    """Serializa el uso del modelo con prioridad para el texto final."""

    def __init__(self):
        self._cv = threading.Condition()
        self._ocupado = False
        self._finales_esperando = 0

    @contextmanager
    def final(self):
        with self._cv:
            self._finales_esperando += 1
            try:
                while self._ocupado:
                    self._cv.wait()
            finally:
                self._finales_esperando -= 1
            self._ocupado = True
        try:
            yield
        finally:
            with self._cv:
                self._ocupado = False
                self._cv.notify_all()

    def tomar_parcial(self) -> bool:
        """Nunca espera: si el modelo está tomado o un final lo pide, no."""
        with self._cv:
            if self._ocupado or self._finales_esperando:
                return False
            self._ocupado = True
            return True

    def liberar_parcial(self):
        with self._cv:
            self._ocupado = False
            self._cv.notify_all()


class ProgramadorParciales:
    """Agenda parciales latest-wins para la unidad de voz abierta.

    ``agregar_audio``/``abrir_unidad``/``cerrar_unidad`` se llaman desde el
    hilo de captura; la inferencia corre en un único hilo propio.
    ``emitir(token, texto)`` decide (bajo los locks de la app) si el
    resultado todavía puede salir y devuelve True si salió.
    """

    def __init__(self, transcribir, emitir, habilitado, turno: TurnoModelo,
                 *, sample_rate=SAMPLE_RATE, intervalo_s=INTERVALO_S,
                 minimo_s=MINIMO_S, ventana_s=VENTANA_S):
        self._transcribir = transcribir
        self._emitir = emitir
        self._habilitado = habilitado
        self.turno = turno
        self._intervalo = int(intervalo_s * sample_rate)
        self._minimo = int(minimo_s * sample_rate)
        self._ventana = int(ventana_s * sample_rate)
        self._cv = threading.Condition()
        self._token = None           # unidad abierta (None: ninguna)
        self._siguiente_token = 0
        self._pendiente = None       # (token, audio) latest-wins
        self._activo = False
        self._cerrado = False
        self._hilo = None
        # Acumulador de la unidad: sólo el hilo de captura lo toca.
        self._trozos = []
        self._muestras = 0
        self._total_unidad = 0
        self._desde_ultimo = 0
        self._ultimo_texto = {}      # token -> último parcial emitido
        self._ultimo_error = None
        self.estadisticas = {
            "agendados": 0, "inferidos": 0, "enviados": 0,
            "deduplicados": 0, "reemplazados": 0, "descartados": 0,
            "sin_turno": 0, "fallos": 0, "max_pendientes": 0,
            "max_activos": 0,
        }

    # ------------------------------------------------ hilo de captura
    def abrir_unidad(self):
        with self._cv:
            self._siguiente_token += 1
            self._token = self._siguiente_token
            self._descartar_pendiente_locked()
            self._ultimo_texto.clear()
        self._reiniciar_audio()

    def cerrar_unidad(self):
        """Hilo de captura: invalida la unidad (final o reinicio) y suelta
        su audio."""
        self.invalidar()
        self._reiniciar_audio()

    def invalidar(self):
        """Cualquier hilo (Esc, shutdown): ningún resultado de la unidad
        actual puede salir y el pendiente se descarta. El acumulador de
        audio se limpia en la próxima apertura desde el hilo de captura."""
        with self._cv:
            self._token = None
            self._descartar_pendiente_locked()
            self._ultimo_texto.clear()

    def agregar_audio(self, audio, voz: bool = True):
        """``voz=False`` para frames de silencio dentro de la unidad: se
        conservan como contexto pero no disparan parciales, así el final
        (que llega tras el silencio de cierre) casi nunca espera a uno."""
        if self._token is None:
            return
        if not self._habilitado():
            # GuionAR ausente: ni audio acumulado ni trabajo pendiente.
            if self._muestras or self._pendiente is not None:
                self._reiniciar_audio()
                with self._cv:
                    self._descartar_pendiente_locked()
            return
        self._trozos.append(audio)
        self._muestras += audio.size
        self._total_unidad += audio.size
        self._desde_ultimo += audio.size
        while self._trozos and self._muestras - self._trozos[0].size >= self._ventana:
            self._muestras -= self._trozos.pop(0).size
        if (voz and self._total_unidad >= self._minimo
                and self._desde_ultimo >= self._intervalo):
            self._desde_ultimo = 0
            ventana = np.concatenate(self._trozos)[-self._ventana:]
            self._agendar(ventana)

    def _reiniciar_audio(self):
        self._trozos = []
        self._muestras = self._total_unidad = self._desde_ultimo = 0

    def _agendar(self, audio):
        with self._cv:
            if self._cerrado or self._token is None:
                return
            if self._pendiente is not None:
                self.estadisticas["reemplazados"] += 1
            self._pendiente = (self._token, audio)
            self.estadisticas["agendados"] += 1
            self.estadisticas["max_pendientes"] = max(
                self.estadisticas["max_pendientes"], 1)
            self._asegurar_hilo_locked()
            self._cv.notify_all()

    def _descartar_pendiente_locked(self):
        if self._pendiente is not None:
            self._pendiente = None
            self.estadisticas["descartados"] += 1

    def _asegurar_hilo_locked(self):
        if self._hilo is not None:
            return
        self._hilo = threading.Thread(
            target=self._ejecutar, name="parlar-parciales", daemon=True)
        self._hilo.start()

    # ------------------------------------------------ consultas
    def vigente(self, token) -> bool:
        with self._cv:
            return not self._cerrado and token is not None \
                and token == self._token

    @property
    def activo(self) -> bool:
        return self._activo

    @property
    def pendiente(self) -> bool:
        return self._pendiente is not None

    # ------------------------------------------------ worker
    def _ejecutar(self):
        while True:
            with self._cv:
                while not self._cerrado and self._pendiente is None:
                    self._cv.wait()
                if self._cerrado:
                    return
                token, audio = self._pendiente
                self._pendiente = None
                if token != self._token or not self._habilitado():
                    self.estadisticas["descartados"] += 1
                    continue
                if not self.turno.tomar_parcial():
                    # Un final está corriendo o esperando: tiene prioridad.
                    self.estadisticas["sin_turno"] += 1
                    continue
                self._activo = True
                self.estadisticas["max_activos"] = max(
                    self.estadisticas["max_activos"], 1)
            try:
                texto = self._inferir(token, audio)
            finally:
                self.turno.liberar_parcial()
                with self._cv:
                    self._activo = False
                    self._cv.notify_all()
            if texto is not None:
                self._entregar(token, texto)

    def _inferir(self, token, audio):
        if not self.vigente(token):
            self.estadisticas["descartados"] += 1
            return None
        try:
            texto = (self._transcribir(audio) or "").strip()
        except Exception as exc:
            self.estadisticas["fallos"] += 1
            tipo = type(exc).__name__
            if tipo != self._ultimo_error:
                self._ultimo_error = tipo
                print(f"[parcial] fallo de inferencia ({tipo}); el texto "
                      "final no se ve afectado", file=sys.stderr)
            return None
        self._ultimo_error = None
        self.estadisticas["inferidos"] += 1
        return texto

    def _entregar(self, token, texto):
        if not texto:
            return
        with self._cv:
            if token != self._token:
                self.estadisticas["descartados"] += 1
                return
            if self._ultimo_texto.get(token) == texto:
                self.estadisticas["deduplicados"] += 1
                return
        if self._emitir(token, texto):
            with self._cv:
                if token == self._token:
                    self._ultimo_texto[token] = texto
                self.estadisticas["enviados"] += 1
            if DEBUG:
                print(f"[parcial] u={token} #{self.estadisticas['enviados']} "
                      f"palabras={len(texto.split())}", file=sys.stderr)
        else:
            self.estadisticas["descartados"] += 1

    # ------------------------------------------------ lifecycle
    def esperar_inactivo(self, timeout=2.0) -> bool:
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._activo and self._pendiente is None, timeout)

    def cerrar(self):
        with self._cv:
            self._cerrado = True
            self._token = None
            self._pendiente = None
            hilo = self._hilo
            self._cv.notify_all()
        if hilo is not None and hilo is not threading.current_thread():
            hilo.join(_CIERRE_TIMEOUT_S)

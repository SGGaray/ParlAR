"""Cliente GuionAR: envía texto y estado VAD al teleprompter por socket Unix.

Diseño:
- Best-effort con timeout corto: si GuionAR no está corriendo, se reintenta la
  conexión en el próximo envío. Nunca propaga excepciones al pipeline.
- Opcional: se activa con `guionar: true` en la config o el flag --guionar.
  Si está desactivado, crear_cliente() devuelve un ClienteNulo (no-ops).
- Deduplicación por conexión viva: VAD solo se envía cuando cambia; los
  parciales solo cuando difieren del último enviado. Un probe no bloqueante
  detecta EOF/reset antes de deduplicar y permite reponer el snapshot.

Protocolo (JSON por líneas, ver GuionAR/INTEGRATION.md):
    {"type": "text",    "data": "hola mundo"}
    {"type": "partial", "data": "hipótesis pendiente"}
    {"type": "vad",     "data": true}
    {"type": "clear"}
"""

import json
import os
import select
import socket
import sys
import threading


def ruta_socket_por_defecto() -> str:
    """Misma convención que GuionAR: runtime dir del usuario."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "guionar.sock")
    return f"/tmp/guionar-{os.getuid()}.sock"


_MAX_TEXTO = 2000  # GuionAR trunca a esto; truncamos acá para no gastar socket


class ClienteGuionAR:
    """Emisor best-effort hacia GuionAR. Nunca lanza excepciones."""

    def __init__(self, ruta: str = ""):
        self.ruta = ruta or ruta_socket_por_defecto()
        self._sock = None
        self._vad_deseado = None
        self._parcial_deseado = None
        self._vad_enviado = None
        self._parcial_enviado = None
        self._epoca_conexion = 0
        self._cerrado = False
        self._lock = threading.RLock()

    # ---------------------------------------------------------- transporte
    def _conectar(self, *, snapshot_parcial=True) -> bool:
        if self._cerrado:
            return False
        if self._sock is not None:
            if self._conexion_viva():
                return True
            self._desconectar()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.05)
            s.connect(self.ruta)
            self._sock = s
            self._epoca_conexion += 1
            self._vad_enviado = None
            self._parcial_enviado = None
            if self._vad_deseado is not None:
                if not self._enviar_conectado(
                        {"type": "vad", "data": self._vad_deseado}):
                    return False
                self._vad_enviado = self._vad_deseado
            if snapshot_parcial and self._parcial_deseado is not None:
                if not self._enviar_conectado(
                        {"type": "partial", "data": self._parcial_deseado}):
                    return False
                self._parcial_enviado = self._parcial_deseado
            return True
        except OSError:
            try:
                s.close()
            except (OSError, UnboundLocalError):
                pass
            self._sock = None
            return False

    def _conexion_viva(self) -> bool:
        """Detecta EOF/reset sin consumir datos ni esperar al receptor."""
        try:
            legibles, _, excepcionales = select.select(
                [self._sock], [], [self._sock], 0)
            if excepcionales:
                return False
            if not legibles:
                return True
            datos = self._sock.recv(
                1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            return datos != b""
        except (BlockingIOError, socket.timeout):
            return True
        except (OSError, ValueError):
            return False

    def _desconectar(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _enviar_conectado(self, obj: dict) -> bool:
        try:
            self._sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n")
                               .encode("utf-8"))
            return True
        except (OSError, BlockingIOError):
            self._desconectar()
            return False

    def _enviar(self, obj: dict) -> bool:
        with self._lock:
            return self._conectar() and self._enviar_conectado(obj)

    # ---------------------------------------------------------- API pública
    def escribir_texto(self, texto: str):
        if not texto:
            return False
        limitado = texto[:_MAX_TEXTO]
        if len(texto) > _MAX_TEXTO:
            print("[guionar] texto final limitado a 2000 caracteres por el "
                  "contrato del receptor", file=sys.stderr)
        with self._lock:
            if self._cerrado:
                return False
            self._parcial_deseado = ""
            conectado = self._conectar(snapshot_parcial=False)
            ok = conectado and self._enviar_conectado(
                {"type": "text", "data": limitado})
            if ok:
                self._parcial_enviado = ""
            return ok

    def enviar_parcial(self, texto: str):
        texto = (texto or "")[-_MAX_TEXTO:]
        with self._lock:
            if self._cerrado:
                return False
            self._parcial_deseado = texto
            if not self._conectar():
                return False
            if texto == self._parcial_enviado:
                return True
            ok = self._enviar_conectado({"type": "partial", "data": texto})
            if ok:
                self._parcial_enviado = texto
            return ok

    def evento_vad(self, hablando: bool):
        hablando = bool(hablando)
        with self._lock:
            if self._cerrado:
                return False
            self._vad_deseado = hablando
            if not self._conectar():
                return False
            if hablando == self._vad_enviado:
                return True
            ok = self._enviar_conectado({"type": "vad", "data": hablando})
            if ok:
                self._vad_enviado = hablando
            return ok

    def enviar_limpiar(self):
        with self._lock:
            if self._cerrado:
                return False
            self._parcial_deseado = ""
            ok = self._conectar() and self._enviar_conectado({"type": "clear"})
            if ok:
                self._parcial_enviado = ""
            return ok

    def cerrar(self):
        with self._lock:
            self._cerrado = True
            self._desconectar()


class ClienteNulo:
    """No-op cuando la integración está desactivada. Mismo contrato."""

    es_nulo = True

    def escribir_texto(self, texto: str): return False
    def enviar_parcial(self, texto: str): return False
    def evento_vad(self, hablando: bool): return False
    def enviar_limpiar(self): return False
    def cerrar(self): pass


def crear_cliente(activado: bool, ruta: str = ""):
    return ClienteGuionAR(ruta) if activado else ClienteNulo()

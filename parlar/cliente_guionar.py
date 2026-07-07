"""Cliente GuionAR: envía texto y estado VAD al teleprompter por socket Unix.

Diseño:
- Fire-and-forget: si GuionAR no está corriendo, los envíos se descartan en
  silencio y se reintenta la conexión en el próximo envío. Este módulo no
  puede bloquear, demorar ni tirar excepciones hacia el pipeline de dictado.
- Opcional: se activa con `guionar: true` en la config o el flag --guionar.
  Si está desactivado, crear_cliente() devuelve un ClienteNulo (no-ops).
- Deduplicación local: VAD solo se envía cuando cambia; los parciales solo
  cuando difieren del último enviado. Evita spam por el socket.

Protocolo (JSON por líneas, ver GuionAR/INTEGRATION.md):
    {"type": "text",    "data": "hola mundo"}
    {"type": "partial", "data": "hipótesis pendiente"}
    {"type": "vad",     "data": true}
    {"type": "clear"}
"""

import json
import os
import socket


def ruta_socket_por_defecto() -> str:
    """Misma convención que GuionAR: runtime dir del usuario."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "guionar.sock")
    return f"/tmp/guionar-{os.getuid()}.sock"


_MAX_TEXTO = 2000  # GuionAR trunca a esto; truncamos acá para no gastar socket


class ClienteGuionAR:
    """Emisor no bloqueante hacia GuionAR. Nunca lanza excepciones."""

    def __init__(self, ruta: str = ""):
        self.ruta = ruta or ruta_socket_por_defecto()
        self._sock = None
        self._ultimo_vad = None
        self._ultimo_parcial = None

    # ---------------------------------------------------------- transporte
    def _conectar(self) -> bool:
        if self._sock is not None:
            return True
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.setblocking(False)
            s.connect(self.ruta)
            self._sock = s
            # tras reconectar, el estado remoto es desconocido: reenviá todo
            self._ultimo_vad = None
            self._ultimo_parcial = None
            return True
        except OSError:
            self._sock = None
            return False

    def _enviar(self, obj: dict):
        if not self._conectar():
            return
        try:
            self._sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n")
                               .encode("utf-8"))
        except (OSError, BlockingIOError):
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None  # reconexión en el próximo envío

    # ---------------------------------------------------------- API pública
    def enviar_texto(self, texto: str):
        if texto:
            self._enviar({"type": "text", "data": texto[:_MAX_TEXTO]})
            self._ultimo_parcial = None  # el final invalida el parcial

    def enviar_parcial(self, texto: str):
        texto = (texto or "")[-_MAX_TEXTO:]
        if texto == self._ultimo_parcial:
            return
        self._ultimo_parcial = texto
        self._enviar({"type": "partial", "data": texto})

    def enviar_vad(self, hablando: bool):
        hablando = bool(hablando)
        if hablando == self._ultimo_vad:
            return
        self._ultimo_vad = hablando
        self._enviar({"type": "vad", "data": hablando})

    def enviar_limpiar(self):
        self._enviar({"type": "clear"})

    def cerrar(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class ClienteNulo:
    """No-op cuando la integración está desactivada. Mismo contrato."""

    def enviar_texto(self, texto: str): pass
    def enviar_parcial(self, texto: str): pass
    def enviar_vad(self, hablando: bool): pass
    def enviar_limpiar(self): pass
    def cerrar(self): pass


def crear_cliente(activado: bool, ruta: str = ""):
    return ClienteGuionAR(ruta) if activado else ClienteNulo()

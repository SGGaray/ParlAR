"""Cliente GuionAR: envía texto y estado VAD al teleprompter por socket Unix.

Diseño:
- Integración automática y opcional: un único worker por cliente detecta a
  GuionAR, conecta, valida el peer y reconecta con backoff acotado. Su
  ausencia es normal: no genera errores visibles ni logs repetidos.
- Los envíos nunca conectan ni esperan: sin conexión vuelven enseguida y el
  evento se descarta. No hay cola ni replay de voz vieja tras reconectar.
- Cada conexión nueva envía un único ``hello`` antes de cualquier evento.
  El hello informa presencia; no autentica ni es requisito del receptor.
- Deduplicación por conexión viva: VAD solo se envía cuando cambia; los
  parciales solo cuando difieren del último enviado.
- Best-effort: nunca propaga excepciones al pipeline.
- Si la integración está desactivada, crear_cliente() devuelve ClienteNulo.

Protocolo (JSON por líneas, ver GuionAR/INTEGRATION.md):
    {"type": "hello",   "client": "parlar", "role": "voice-producer",
     "protocol": 1}
    {"type": "text",    "data": "hola mundo"}
    {"type": "partial", "data": "hipótesis pendiente"}
    {"type": "vad",     "data": true}
    {"type": "clear"}
"""

import errno
import json
import os
import select
import socket
import struct
import sys
import threading

from .entrega import EstadoEntrega, ResultadoSink


CONECTADO = "connected"
DESCONECTADO = "disconnected"

HELLO = {
    "type": "hello",
    "client": "parlar",
    "role": "voice-producer",
    "protocol": 1,
}


def ruta_socket_por_defecto() -> str:
    """Misma convención que GuionAR: runtime dir del usuario."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "guionar.sock")
    return f"/tmp/guionar-{os.getuid()}.sock"


_MAX_TEXTO = 2000  # GuionAR trunca a esto; truncamos acá para no gastar socket
_UCRED = struct.Struct("=iII")  # Linux: pid_t, uid_t, gid_t
_TIMEOUT_SOCKET_S = 0.05
# Reintentos mientras GuionAR no está: rápido tras una caída, luego ~1.5 s.
_BACKOFF_S = (0.25, 0.5, 1.0, 1.5)
_CIERRE_TIMEOUT_S = 1.0
_AUSENCIA_NORMAL = frozenset({errno.ENOENT, errno.ECONNREFUSED})


class ClienteGuionAR:
    """Emisor best-effort hacia GuionAR. Nunca lanza excepciones."""

    def __init__(self, ruta: str = ""):
        self.ruta = ruta or ruta_socket_por_defecto()
        self._expected_uid = os.getuid()
        self._sock = None
        self._vad_enviado = None
        self._parcial_enviado = None
        self._epoca_conexion = 0
        self._cerrado = False
        self._lock = threading.RLock()
        self._estado = DESCONECTADO
        self._hilo = None
        self._despertar_r = None
        self._despertar_w = None
        self._ultimo_aviso = None
        self._observadores = []
        self._notificacion_lock = threading.RLock()
        self._estado_notificado = DESCONECTADO

    # ---------------------------------------------------------- transporte
    def _avisar(self, mensaje: str):
        """Loguea una sola vez por racha; nunca incluye contenido dictado."""
        if mensaje != self._ultimo_aviso:
            self._ultimo_aviso = mensaje
            print(f"[guionar] {mensaje}", file=sys.stderr)

    def _validar_peer(self, sock) -> bool:
        """Verifica el UID del socket conectado antes del primer payload."""
        opcion = getattr(socket, "SO_PEERCRED", None)
        if opcion is None:
            self._avisar("validación del peer no disponible")
            return False
        try:
            datos = sock.getsockopt(socket.SOL_SOCKET, opcion, _UCRED.size)
            if len(datos) != _UCRED.size:
                raise ValueError("credenciales de peer malformadas")
            _, uid, _ = _UCRED.unpack(datos)
        except (OSError, TypeError, ValueError, struct.error):
            self._avisar("falló la validación del peer")
            return False
        if uid != self._expected_uid:
            self._avisar("peer rechazado: UID inesperado")
            return False
        return True

    def _conectar(self) -> bool:
        """Un intento acotado: connect, validar peer y hello, en ese orden."""
        with self._lock:
            if self._cerrado:
                return False
            if self._sock is not None:
                return True
        s = None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(_TIMEOUT_SOCKET_S)
            s.connect(self.ruta)
            if not self._validar_peer(s):
                s.close()
                return False
            with self._lock:
                if self._cerrado or self._sock is not None:
                    s.close()
                    return self._sock is not None
                self._sock = s
                s = None
                self._epoca_conexion += 1
                self._vad_enviado = None
                self._parcial_enviado = None
                # El hello sale bajo el mismo lock que publica el socket:
                # ningún evento de voz puede adelantarse en esta conexión.
                if not self._enviar_conectado(HELLO):
                    return False
                self._estado = CONECTADO
                self._ultimo_aviso = None
                return True
        except OSError as exc:
            if exc.errno not in _AUSENCIA_NORMAL \
                    and not isinstance(exc, socket.timeout):
                codigo = errno.errorcode.get(exc.errno, "error")
                self._avisar(f"no se pudo conectar ({codigo})")
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            return False

    def _drenar(self, sock) -> bool:
        """Descarta datos inesperados del peer; False ante EOF/reset."""
        try:
            return sock.recv(4096, socket.MSG_DONTWAIT) != b""
        except (BlockingIOError, InterruptedError, socket.timeout):
            return True
        except (OSError, ValueError):
            return False

    def _desconectar(self):
        sock, self._sock = self._sock, None
        self._estado = DESCONECTADO
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            self._despertar()

    def _despertar(self):
        # Llamado con ``_lock`` tomado: el worker cierra el pipe bajo el
        # mismo lock, así que nunca se escribe en un descriptor reciclado.
        if self._despertar_w is not None:
            try:
                os.write(self._despertar_w, b"\0")
            except OSError:
                pass

    def _enviar_conectado(self, obj: dict) -> bool:
        try:
            self._sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n")
                               .encode("utf-8"))
            return True
        except (OSError, AttributeError):
            self._desconectar()
            return False

    # ---------------------------------------------------------- worker
    def _esperar(self, sock, despertar_r, timeout):
        lectura = [despertar_r] if sock is None else [sock, despertar_r]
        try:
            listos, _, _ = select.select(lectura, [], [], timeout)
        except InterruptedError:
            return False
        except (OSError, ValueError):
            # Un emisor cerró el socket entre la lectura y el select: el
            # drenaje posterior confirma la caída sin loguear ni girar.
            return sock is not None
        if despertar_r in listos:
            try:
                while os.read(despertar_r, 64):
                    pass
            except OSError:
                pass
        return sock is not None and sock in listos

    def _ejecutar(self, despertar_r):
        intento = 0
        try:
            while True:
                with self._lock:
                    if self._cerrado:
                        return
                    sock = self._sock
                try:
                    if sock is None:
                        if self._conectar():
                            intento = 0
                        else:
                            espera = _BACKOFF_S[
                                min(intento, len(_BACKOFF_S) - 1)]
                            intento += 1
                            self._esperar(None, despertar_r, espera)
                    elif self._esperar(sock, despertar_r, None):
                        with self._lock:
                            if self._sock is sock and not self._drenar(sock):
                                self._desconectar()
                except Exception as exc:
                    # Un fallo inesperado nunca debe matar el worker ni
                    # convertirse en un bucle caliente.
                    self._avisar(f"worker: {type(exc).__name__}")
                    with self._lock:
                        if self._sock is sock:
                            self._desconectar()
                    try:
                        self._esperar(None, despertar_r, _BACKOFF_S[-1])
                    except Exception:
                        pass
                finally:
                    self._notificar()
        finally:
            with self._lock:
                self._desconectar()
                r, w = self._despertar_r, self._despertar_w
                self._despertar_r = self._despertar_w = None
            for fd in (r, w):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._notificar()

    # ---------------------------------------------------------- estado
    @property
    def estado(self) -> str:
        return self._estado

    def agregar_observador(self, callback):
        """Registra ``callback(estado)``; se llama sólo cuando cambia.

        Se invoca desde el worker o el hilo emisor, nunca con el lock de
        transporte tomado. Una UI debe pasar la actualización a su hilo.
        """
        with self._notificacion_lock:
            self._observadores.append(callback)

    def _notificar(self):
        if self._estado == self._estado_notificado:
            return
        with self._notificacion_lock:
            estado = self._estado
            if estado == self._estado_notificado:
                return
            self._estado_notificado = estado
            print("[guionar] conectado" if estado == CONECTADO
                  else "[guionar] desconectado")
            for callback in tuple(self._observadores):
                try:
                    callback(estado)
                except Exception as exc:
                    print(f"[guionar] observador falló: {type(exc).__name__}",
                          file=sys.stderr)

    # ---------------------------------------------------------- API pública
    def iniciar(self) -> bool:
        """Arranca el único worker de conexión. Idempotente."""
        with self._lock:
            if self._cerrado:
                return False
            if self._hilo is not None:
                return True
            try:
                r, w = os.pipe()
            except OSError:
                return False
            os.set_blocking(r, False)
            os.set_blocking(w, False)
            self._despertar_r, self._despertar_w = r, w
            hilo = threading.Thread(
                target=self._ejecutar, args=(r,),
                name="parlar-guionar", daemon=True)
            try:
                hilo.start()
            except RuntimeError:
                self._despertar_r = self._despertar_w = None
                os.close(r)
                os.close(w)
                return False
            self._hilo = hilo
            return True

    def comprobar_disponibilidad(self):
        """Indica si hay una conexión validada; no hace I/O."""
        return self._sock is not None

    def _tras_envio(self, resultado):
        if self._estado != self._estado_notificado:
            self._notificar()
        return resultado

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
            if self._sock is None:
                # GuionAR ausente es normal: no es un fallo de entrega.
                return ResultadoSink(EstadoEntrega.SKIPPED)
            ok = self._enviar_conectado({"type": "text", "data": limitado})
            if ok:
                self._parcial_enviado = ""
        return self._tras_envio(ok)

    def enviar_parcial(self, texto: str):
        texto = (texto or "")[-_MAX_TEXTO:]
        with self._lock:
            if self._cerrado or self._sock is None:
                return False
            if texto == self._parcial_enviado:
                return True
            ok = self._enviar_conectado({"type": "partial", "data": texto})
            if ok:
                self._parcial_enviado = texto
        return self._tras_envio(ok)

    def evento_vad(self, hablando: bool):
        hablando = bool(hablando)
        with self._lock:
            if self._cerrado or self._sock is None:
                return False
            if hablando == self._vad_enviado:
                return True
            ok = self._enviar_conectado({"type": "vad", "data": hablando})
            if ok:
                self._vad_enviado = hablando
        return self._tras_envio(ok)

    def enviar_limpiar(self):
        with self._lock:
            if self._cerrado or self._sock is None:
                return False
            ok = self._enviar_conectado({"type": "clear"})
            if ok:
                self._parcial_enviado = ""
        return self._tras_envio(ok)

    def cerrar(self):
        with self._lock:
            self._cerrado = True
            self._desconectar()
            self._despertar()
            hilo = self._hilo
        if hilo is not None and hilo is not threading.current_thread():
            hilo.join(_CIERRE_TIMEOUT_S)
        self._notificar()


class ClienteNulo:
    """No-op cuando la integración está desactivada. Mismo contrato."""

    es_nulo = True
    estado = None

    def iniciar(self): return False
    def agregar_observador(self, callback): pass
    def comprobar_disponibilidad(self): return True
    def escribir_texto(self, texto: str): return False
    def enviar_parcial(self, texto: str): return False
    def evento_vad(self, hablando: bool): return False
    def enviar_limpiar(self): return False
    def cerrar(self): pass


def crear_cliente(activado: bool, ruta: str = ""):
    """El cliente real no abre sockets ni hilos hasta ``iniciar()``."""
    return ClienteGuionAR(ruta) if activado else ClienteNulo()

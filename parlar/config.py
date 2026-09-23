"""Configuración de ParlAR.

Se carga desde ~/.config/parlar/config.json si existe; si no, valores por
defecto. Todo valor puede sobreescribirse con flags CLI en __main__.py.

Nota: las claves del JSON se mantienen en inglés a propósito, para no romper
configuraciones existentes (compatibilidad hacia atrás).
"""

import json
import ipaddress
import math
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import ClassVar

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "parlar"
CONFIG_FILE = CONFIG_DIR / "config.json"
_runtime = os.environ.get("XDG_RUNTIME_DIR")
RUNTIME_DIR = Path(_runtime) if _runtime and Path(_runtime).is_dir() else Path("/tmp")
SOCKET_PATH = (RUNTIME_DIR / "parlar.sock" if _runtime and Path(_runtime).is_dir()
               else RUNTIME_DIR / f"parlar-{os.getuid()}.sock")


class ErrorConfiguracion(ValueError):
    """Configuración inválida que impide iniciar ParlAR con seguridad."""


@dataclass
class Config:
    SCHEMA_VERSION: ClassVar[int] = 1
    MODOS: ClassVar[frozenset[str]] = frozenset({"utterance", "streaming"})
    DISPOSITIVOS: ClassVar[frozenset[str]] = frozenset({"auto", "cpu", "cuda"})
    COMPUTE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"auto", "int8", "float16", "int8_float16"}
    )
    MAX_CONTEXT_TERMS: ClassVar[int] = 50
    MAX_CONTEXT_TERM_CHARS: ClassVar[int] = 80
    MAX_CONTEXT_TOTAL_CHARS: ClassVar[int] = 2000

    REESCRITURAS: ClassVar[frozenset[str]] = frozenset(
        {"none", "formal", "concise", "email"}
    )
    INYECTORES: ClassVar[frozenset[str]] = frozenset(
        {"auto", "xdotool", "wtype", "ydotool", "clipboard"}
    )
    CAMPOS_DINAMICOS: ClassVar[frozenset[str]] = frozenset(
        {"mode", "rewrite_mode"}
    )

    schema_version: int = SCHEMA_VERSION

    # --- STT ---
    model_size: str = "small"          # tiny | base | small | medium | large-v3
    device: str = "auto"               # auto | cpu | cuda
    compute_type: str = "auto"         # auto | int8 | float16 | int8_float16
    language: str = "es"               # español por defecto; "" = autodetectar
    beam_size: int = 5                 # usado en pasadas finales por frase
    context_terms: list = field(default_factory=list)  # vocabulario/contexto personalizado

    # --- Modo ---
    mode: str = "utterance"            # utterance | streaming

    # --- Audio / VAD ---
    sample_rate: int = 16000
    frame_ms: int = 20                 # webrtcvad soporta 10/20/30
    vad_aggressiveness: int = 2        # 0..3
    silence_ms: int = 600              # silencio que cierra una frase
    preroll_ms: int = 300              # audio previo al inicio de voz que se conserva
    max_utterance_s: float = 30.0
    min_speech_ms: int = 200           # ignora chispazos más cortos que esto

    # --- Streaming ---
    stream_interval_s: float = 1.0     # cadencia de re-decodificación
    stream_trim_s: float = 12.0        # recorta audio confirmado pasado este tamaño

    # --- Procesamiento de texto ---
    rewrite_mode: str = "none"         # none | formal | concise | email
    remove_fillers: bool = True
    voice_commands: bool = True
    comando_enviar: bool = False       # autoriza TODA acción que genera Enter;
                                       # apagado por defecto: ver SECURITY.md
    ollama_model: str = ""             # ej. "llama3.2:3b"; vacío = solo reglas
    ollama_url: str = "http://127.0.0.1:11434"

    # --- Inyección ---
    injector: str = "auto"             # auto | xdotool | wtype | ydotool | clipboard
    type_delay_ms: int = 1

    # --- Atajos ---
    # X11: mantener para dictar. Conservamos el nombre histórico del campo.
    # Wayland: usar bindings del compositor con parlarctl.
    hotkey_toggle: str = "<ctrl_r>+<shift_r>"
    hotkey_quit: str = "<ctrl>+<alt>+q"

    # --- UI ---
    overlay: bool = True
    notify: bool = True                # notificaciones de escritorio vía notify-send

    # --- GuionAR (teleprompter, opcional) ---
    guionar: bool = False              # enviar texto/VAD al overlay GuionAR
    guionar_socket: str = ""           # vacío = $XDG_RUNTIME_DIR/guionar.sock

    # --- Sesión (transcript en disco, opcional) ---
    guardar_sesion: bool = False       # apagado por defecto: dictados son datos
                                       # sensibles en reposo, ver SECURITY.md

    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except UnicodeDecodeError as e:
                raise ErrorConfiguracion(
                    f"no se pudo leer {CONFIG_FILE}: codificación UTF-8 "
                    f"inválida en el byte {e.start}"
                ) from e
            except (json.JSONDecodeError, OSError) as e:
                raise ErrorConfiguracion(
                    f"no se pudo leer {CONFIG_FILE}: {e}"
                ) from e
            if not isinstance(data, dict):
                raise ErrorConfiguracion(
                    f"{CONFIG_FILE}: la raíz debe ser un objeto JSON"
                )

            data = cls._migrar_datos(data)

            configurables = {
                campo.name: campo.type for campo in fields(cls)
                if campo.name != "extras"
            }
            for nombre, valor in data.items():
                if nombre == "extras":
                    if type(valor) is not dict:
                        raise ErrorConfiguracion(
                            f"{CONFIG_FILE}: 'extras' debe ser un objeto JSON"
                        )
                    cfg.extras.update(valor)
                elif nombre in configurables:
                    esperado = configurables[nombre]
                    if type(valor) is not esperado:
                        raise ErrorConfiguracion(
                            f"{CONFIG_FILE}: '{nombre}' debe ser "
                            f"{cls._nombre_tipo(esperado)}, no "
                            f"{type(valor).__name__}"
                        )
                    setattr(cfg, nombre, valor)
                else:
                    cfg.extras[nombre] = valor
        cfg.validate()
        return cfg

    @classmethod
    def _migrar_datos(cls, data: dict) -> dict:
        """Actualiza formatos conocidos sin inferir preferencias del usuario."""
        version = data.get("schema_version", 0)
        if type(version) is not int:
            raise ErrorConfiguracion(
                f"{CONFIG_FILE}: 'schema_version' debe ser entero, no "
                f"{type(version).__name__}"
            )
        if version < 0:
            raise ErrorConfiguracion(
                f"{CONFIG_FILE}: 'schema_version' no puede ser negativo"
            )
        if version > cls.SCHEMA_VERSION:
            raise ErrorConfiguracion(
                f"{CONFIG_FILE}: schema_version={version} requiere una "
                "versión más nueva de ParlAR"
            )

        migrados = dict(data)
        while version < cls.SCHEMA_VERSION:
            if version == 0:
                # El formato sin versión no registraba si hotkey_toggle era
                # default o elección explícita. Se preserva exactamente.
                version = 1
            migrados["schema_version"] = version
        return migrados

    def save(self) -> None:
        self.validate()
        directorio = CONFIG_FILE.parent
        creado = not directorio.exists()
        directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
        if creado:
            directorio.chmod(0o700)

        contenido = (json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n").encode()
        temporal = None
        fd = None
        try:
            for intento in range(100):
                candidata = directorio / f".{CONFIG_FILE.name}.tmp-{os.getpid()}-{intento}"
                try:
                    fd = os.open(candidata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    temporal = candidata
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("no se pudo reservar un archivo temporal")

            archivo = os.fdopen(fd, "wb")
            fd = None  # desde aquí el objeto archivo es dueño del descriptor
            with archivo:
                archivo.write(contenido)
                archivo.flush()
            os.replace(temporal, CONFIG_FILE)
            temporal = None
            CONFIG_FILE.chmod(0o600)
        finally:
            if fd is not None:
                os.close(fd)
            if temporal is not None:
                try:
                    temporal.unlink()
                except FileNotFoundError:
                    pass

    def validate(self) -> None:
        """Verifica tipos, dominios y relaciones antes de crear recursos."""
        errores = []
        for campo in fields(self):
            esperado = campo.type
            valor = getattr(self, campo.name)
            if type(valor) is not esperado:
                errores.append(
                    f"'{campo.name}' debe ser {self._nombre_tipo(esperado)}, "
                    f"no {type(valor).__name__}"
                )
        if errores:
            raise ErrorConfiguracion("; ".join(errores))

        def enum(nombre, opciones):
            valor = getattr(self, nombre)
            if valor not in opciones:
                permitidos = ", ".join(sorted(opciones))
                errores.append(
                    f"'{nombre}'={valor!r} no es válido; se esperaba: {permitidos}"
                )

        enum("device", self.DISPOSITIVOS)
        enum("compute_type", self.COMPUTE_TYPES)
        enum("mode", self.MODOS)
        enum("rewrite_mode", self.REESCRITURAS)
        enum("injector", self.INYECTORES)

        if self.schema_version != self.SCHEMA_VERSION:
            errores.append(
                f"'schema_version'={self.schema_version}: se esperaba "
                f"{self.SCHEMA_VERSION}"
            )

        if type(self.context_terms) is list:
            if len(self.context_terms) > self.MAX_CONTEXT_TERMS:
                errores.append(
                    f"'context_terms' admite como máximo "
                    f"{self.MAX_CONTEXT_TERMS} términos"
                )

            total_contexto = 0
            for indice, termino in enumerate(self.context_terms):
                if type(termino) is not str:
                    errores.append(
                        f"'context_terms[{indice}]' debe ser texto, no "
                        f"{type(termino).__name__}"
                    )
                    continue

                limpio = " ".join(termino.split())
                if not limpio:
                    errores.append(
                        f"'context_terms[{indice}]' no puede estar vacío"
                    )
                    continue

                if "\n" in termino or "\r" in termino:
                    errores.append(
                        f"'context_terms[{indice}]' no puede contener "
                        "saltos de línea"
                    )

                if len(limpio) > self.MAX_CONTEXT_TERM_CHARS:
                    errores.append(
                        f"'context_terms[{indice}]' supera "
                        f"{self.MAX_CONTEXT_TERM_CHARS} caracteres"
                    )

                total_contexto += len(limpio)

            if total_contexto > self.MAX_CONTEXT_TOTAL_CHARS:
                errores.append(
                    f"'context_terms' supera "
                    f"{self.MAX_CONTEXT_TOTAL_CHARS} caracteres totales"
                )

        if not self.model_size.strip():
            errores.append("'model_size' no puede estar vacío")
        if self.sample_rate != 16000:
            errores.append(
                f"'sample_rate'={self.sample_rate}: solo se admite 16000 Hz "
                "(sin resampling)"
            )
        if self.frame_ms not in {10, 20, 30}:
            errores.append(
                f"'frame_ms'={self.frame_ms}: WebRTC VAD requiere 10, 20 o 30"
            )
        if not 0 <= self.vad_aggressiveness <= 3:
            errores.append(
                f"'vad_aggressiveness'={self.vad_aggressiveness}: "
                "debe estar entre 0 y 3"
            )
        if self.beam_size <= 0:
            errores.append(f"'beam_size'={self.beam_size}: debe ser mayor que 0")
        if self.silence_ms <= 0:
            errores.append(f"'silence_ms'={self.silence_ms}: debe ser mayor que 0")
        if self.preroll_ms < 0:
            errores.append(f"'preroll_ms'={self.preroll_ms}: no puede ser negativo")
        if self.preroll_ms < self.min_speech_ms:
            errores.append(
                f"'preroll_ms'={self.preroll_ms}: debe ser mayor o igual que "
                f"'min_speech_ms'={self.min_speech_ms}"
            )
        if self.min_speech_ms <= 0:
            errores.append(
                f"'min_speech_ms'={self.min_speech_ms}: debe ser mayor que 0"
            )
        if self.type_delay_ms < 0:
            errores.append(
                f"'type_delay_ms'={self.type_delay_ms}: no puede ser negativo"
            )

        for nombre in ("max_utterance_s", "stream_interval_s", "stream_trim_s"):
            valor = getattr(self, nombre)
            if not math.isfinite(valor) or valor <= 0:
                errores.append(
                    f"'{nombre}'={valor!r}: debe ser finito y mayor que 0"
                )
        if (math.isfinite(self.max_utterance_s)
                and self.max_utterance_s * 1000 < self.frame_ms):
            errores.append(
                f"'max_utterance_s'={self.max_utterance_s}: debe admitir al "
                f"menos un 'frame_ms'={self.frame_ms}"
            )
        if self.min_speech_ms > self.max_utterance_s * 1000:
            errores.append(
                f"'min_speech_ms'={self.min_speech_ms}: no puede superar "
                f"'max_utterance_s'={self.max_utterance_s}"
            )
        if (math.isfinite(self.max_utterance_s)
                and self.preroll_ms > self.max_utterance_s * 1000):
            errores.append(
                f"'preroll_ms'={self.preroll_ms}: no puede superar "
                f"'max_utterance_s'={self.max_utterance_s} (en milisegundos)"
            )

        if not self._url_ollama_valida(self.ollama_url):
            errores.append(
                f"'ollama_url'={self.ollama_url!r}: debe ser una URL HTTP(S) "
                "con hostname válido"
            )

        if errores:
            raise ErrorConfiguracion("; ".join(errores))

    def construir_contexto_stt(self) -> str:
        """Construye contexto STT estable desde términos del usuario."""
        normalizados = []
        vistos = set()

        for termino in self.context_terms:
            limpio = " ".join(termino.split())
            clave = limpio.casefold()
            if limpio and clave not in vistos:
                vistos.add(clave)
                normalizados.append(limpio)

        if not normalizados:
            return ""

        return (
            "Vocabulario relevante: "
            + ", ".join(normalizados)
            + "."
        )

    @staticmethod
    def _nombre_tipo(tipo) -> str:
        return {str: "texto", int: "entero", float: "número decimal",
                bool: "booleano"}.get(tipo, tipo.__name__)

    @staticmethod
    def _url_ollama_valida(valor: str) -> bool:
        try:
            url = urllib.parse.urlsplit(valor)
            hostname = url.hostname
            _ = url.port  # también valida formato y rango del puerto
        except (TypeError, ValueError):
            return False
        if (url.scheme not in {"http", "https"} or not hostname
                or url.query or url.fragment
                or any(caracter.isspace() for caracter in valor)):
            return False
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            try:
                ascii_hostname = hostname.rstrip(".").encode("idna").decode()
            except UnicodeError:
                return False
            etiqueta = re.compile(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
            return (bool(ascii_hostname) and len(ascii_hostname) <= 253
                    and all(etiqueta.fullmatch(parte)
                            for parte in ascii_hostname.split(".")))

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2  # int16

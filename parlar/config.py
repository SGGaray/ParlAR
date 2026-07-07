"""Configuración de ParlAR.

Se carga desde ~/.config/parlar/config.json si existe; si no, valores por
defecto. Si existe una configuración legada de FlowDictate
(~/.config/flowdictate/config.json) y todavía no hay una nueva, se migra
automáticamente. Todo valor puede sobreescribirse con flags CLI en __main__.py.

Nota: las claves del JSON se mantienen en inglés a propósito, para no romper
configuraciones existentes (compatibilidad hacia atrás).
"""

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "parlar"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                      / "flowdictate" / "config.json")
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCKET_PATH = RUNTIME_DIR / "parlar.sock"


@dataclass
class Config:
    # --- STT ---
    model_size: str = "small"          # tiny | base | small | medium | large-v3
    device: str = "auto"               # auto | cpu | cuda
    compute_type: str = "auto"         # auto | int8 | float16 | int8_float16
    language: str = "es"               # español por defecto; "" = autodetectar
    beam_size: int = 5                 # usado en pasadas finales por frase

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
    ollama_model: str = ""             # ej. "llama3.2:3b"; vacío = solo reglas
    ollama_url: str = "http://127.0.0.1:11434"

    # --- Inyección ---
    injector: str = "auto"             # auto | xdotool | wtype | ydotool | clipboard
    type_delay_ms: int = 1

    # --- Atajos (solo X11; en Wayland asigná `parlarctl alternar` en tu DE) ---
    hotkey_toggle: str = "<ctrl>+<alt>+d"
    hotkey_quit: str = "<ctrl>+<alt>+q"

    # --- UI ---
    overlay: bool = True
    notify: bool = True                # notificaciones de escritorio vía notify-send

    # --- GuionAR (teleprompter, opcional) ---
    guionar: bool = False              # enviar texto/VAD al overlay GuionAR
    guionar_socket: str = ""           # vacío = $XDG_RUNTIME_DIR/guionar.sock

    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        source = None
        if CONFIG_FILE.exists():
            source = CONFIG_FILE
        elif LEGACY_CONFIG_FILE.exists():
            source = LEGACY_CONFIG_FILE
            print(f"[config] migrando configuración legada desde {LEGACY_CONFIG_FILE}")
        if source is not None:
            try:
                data = json.loads(source.read_text())
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
                    else:
                        cfg.extras[k] = v
            except (json.JSONDecodeError, OSError) as e:
                print(f"[config] no se pudo leer {source}: {e}; usando valores por defecto")
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2  # int16

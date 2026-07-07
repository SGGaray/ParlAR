"""Shim de compatibilidad: FlowDictate fue renombrado a ParlAR.

Este paquete mantiene funcionando los imports viejos:

    import flowdictate
    from flowdictate.textproc import TextProcessor
    from flowdictate.audio import MicListener
    python -m flowdictate

Todo se re-mapea al paquete `parlar` y a sus nombres nuevos. Emite un
DeprecationWarning para incentivar la migración.
"""

import sys
import warnings

warnings.warn(
    "flowdictate fue renombrado a parlar; actualizá tus imports a `import parlar`",
    DeprecationWarning,
    stacklevel=2,
)

import parlar  # noqa: E402
from parlar import (  # noqa: E402
    app,
    capturador_audio as audio,
    config,
    control,
    daemon_atajos as hotkeys,
    indicador as overlay,
    inyector_salida as inject,
    motor_transcripcion as stt,
    procesador_texto as textproc,
)

# los submódulos viejos siguen siendo importables:
# `from flowdictate.audio import ...`
for _viejo, _mod in {
    "audio": audio, "stt": stt, "textproc": textproc, "inject": inject,
    "hotkeys": hotkeys, "control": control, "overlay": overlay,
    "app": app, "config": config,
}.items():
    sys.modules[f"{__name__}.{_viejo}"] = _mod

# alias de nombres viejos -> clases/funciones nuevas
audio.MicListener = audio.CapturadorMic
audio.Segmenter = audio.Segmentador
audio.SegmentEvent = audio.EventoSegmento
audio.make_vad = audio.crear_vad

stt.WhisperEngine = stt.MotorWhisper
stt.UtteranceTranscriber = stt.TranscriptorFrase
stt.StreamingTranscriber = stt.TranscriptorStreaming

textproc.TextProcessor = textproc.ProcesadorTexto
textproc.Processed = textproc.Procesado

inject.Injector = inject.Inyector
inject.detect_session = inject.detectar_sesion

hotkeys.HotkeyDaemon = hotkeys.DaemonAtajos

control.ControlServer = control.ServidorControl
control.send_command = control.enviar_comando
control.flowctl_main = control.parlarctl_main

overlay.Overlay = overlay.Indicador
overlay.HeadlessLoop = overlay.BucleSinUI
overlay.make_ui = overlay.crear_ui

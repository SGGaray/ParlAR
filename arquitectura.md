# ParlAR: dictado local-primero, a nivel sistema, para Linux

Alternativa a Wispr Flow enfocada en privacidad. Todo corre en tu máquina. Ni audio, ni texto, ni telemetría salen de ella.

## 1. Cómo funcionan realmente Wispr Flow y herramientas similares

Haciendo ingeniería inversa del comportamiento observable de Wispr Flow, la arquitectura es:

1. **Daemon residente en segundo plano.** Un proceso dueño del pipeline de micrófono y del atajo global, inactivo hasta que se dispara.
2. **Captura push-to-talk / alternada.** El atajo abre el mic; el audio se captura como PCM crudo (16 kHz mono es la entrada estándar de STT) en un buffer circular.
3. **Segmentación con puerta VAD.** Un detector de actividad de voz parte el stream en frases (voz delimitada por silencio). Esto es lo que hace que la latencia se sienta baja: la transcripción arranca apenas pausás, no cuando terminás de dictar.
4. **Motor STT.** Modelo de la familia Whisper. Dos estrategias:
   - *Modo por frase (chunked):* transcribe cada segmento VAD al cerrarse. Simple, preciso; latencia = umbral de silencio + inferencia.
   - *Modo streaming:* re-transcribe una ventana creciente cada ~1s y confirma solo las palabras en las que dos hipótesis consecutivas coinciden (política "LocalAgreement" del paper whisper_streaming). Las palabras aparecen mientras seguís hablando.
5. **Post-procesamiento.** Whisper ya emite puntuación y mayúsculas; una capa de limpieza normaliza espacios, mayúsculas de oración y muletillas, e interpreta comandos de voz ("nuevo párrafo", "borra la última oración"). Las herramientas de nube agregan una pasada de reescritura con LLM; en local esto es opcional (por reglas, o con un modelo de Ollama si el usuario corre uno, siempre 100% local).
6. **Inyección a nivel sistema.** El texto limpio se tipea en la ventana con foco usando pulsaciones sintéticas del SO. En Linux: `xdotool` (X11), `wtype`/`ydotool` (Wayland).
7. **Indicador mínimo.** Un puntito siempre visible que muestra el estado de grabación.

## 2. Diagrama de componentes

```
                 ┌───────────────────────────── daemon parlar ───────────────────────────────┐
                 │                                                                            │
 atajo ──────────┼─> daemon_atajos.py (pynput, X11)  ─┐                                       │
 parlarctl ──────┼─> control.py (socket unix)        ─┼─> app.py  (máquina de estados)        │
 alternar        │                                   ─┘     │                                 │
                 │   ┌─────────────────┐  PCM 16kHz  ┌──────▼─────────────┐                   │
   micrófono ────┼──>│capturador_audio │──frames────>│ Segmentador (VAD)  │                   │
                 │   │ sounddevice     │             │ webrtcvad + preroll│                   │
                 │   └─────────────────┘             └──────────┬─────────┘                   │
                 │                                  frases /    │ ventana creciente           │
                 │                                  ┌───────────▼──────────┐                  │
                 │                                  │ motor_transcripcion  │                  │
                 │                                  │ faster-whisper       │ (CUDA si hay)    │
                 │                                  │ frase o              │                  │
                 │                                  │ LocalAgreement       │                  │
                 │                                  └───────────┬──────────┘                  │
                 │                                       texto  │ crudo                       │
                 │                                  ┌───────────▼──────────┐                  │
                 │                                  │ procesador_texto     │                  │
                 │                                  │ limpieza, comandos,  │                  │
                 │                                  │ modos de reescritura │                  │
                 │                                  └───────────┬──────────┘                  │
                 │                                texto limpio  │ / comandos                  │
                 │                                  ┌───────────▼──────────┐   ┌───────────┐  │
                 │                                  │ inyector_salida      │──>│ CUALQUIER │  │
                 │                                  │ xdotool / wtype      │   │ app con   │  │
                 │                                  │ / ydotool            │   │ foco      │  │
                 │                                  └──────────────────────┘   └───────────┘  │
                 │   ┌──────────────┐                                                         │
                 │   │ indicador.py │ <── eventos de estado (inactivo/grabando/transcribiendo)│
                 │   │ punto tkinter│                                                         │
                 │   └──────────────┘                                                         │
                 └────────────────────────────────────────────────────────────────────────────┘
```

## 3. Flujo de datos

```
mic → PCM int16 @16kHz → frames de 20ms → puerta VAD
    → (modo frase)     buffer de frase completa a los 600ms de silencio → whisper → texto
    → (modo streaming) ventana creciente cada 1.0s → whisper(word_timestamps)
                        → confirmación LocalAgreement-2 → texto incremental
    → procesador_texto: normalización de espacios/mayúsculas (con ¿ ¡ del español),
                        parseo de comandos de voz, reescritura opcional
    → inyector: pulsaciones sintéticas en la ventana con foco (X11 o Wayland)
```

## 4. Stack tecnológico y justificación

| Aspecto              | Elección                        | Por qué |
|----------------------|---------------------------------|---------|
| STT                  | **faster-whisper** (CTranslate2)| 4x más rápido que openai/whisper en CPU, cuantización int8, float16 con CUDA, timestamps por palabra (necesarios para confirmar en streaming). whisper.cpp queda como respaldo si algún día hace falta latencia a nivel C++; el límite de módulo (`motor_transcripcion.py`) aísla ese reemplazo. |
| Captura de audio     | sounddevice (PortAudio)         | Captura por callback sólida, funciona con PulseAudio y PipeWire. |
| VAD                  | webrtcvad                       | Chico, rápido, probadísimo, granularidad de frames de 20ms. Respaldo por energía incluido si el wheel no está disponible. |
| Inyección X11        | xdotool                         | El estándar. `type --clearmodifiers --delay 1`. |
| Inyección Wayland    | wtype, luego ydotool            | wtype usa el protocolo virtual-keyboard (wlroots, KDE). ydotool funciona en todos lados vía uinput pero necesita su daemon. Respaldo por portapapeles (wl-copy/xclip) como último recurso. |
| Atajos               | pynput en X11; socket unix + `parlarctl` en Wayland | Los compositores Wayland no permiten capturas globales de teclas desde apps arbitrarias; el patrón correcto es asignar `parlarctl alternar` a un atajo del compositor. |
| Indicador            | tkinter                         | Cero dependencias extra (python3-tk), puntito sin bordes siempre visible. |
| IPC                  | Socket de dominio Unix          | Permite que cualquier script/daemon de atajos controle la instancia corriendo. |
| Reescritura (opcional)| Reglas, u Ollama local         | Mantiene la garantía de "nada sale de la máquina". La llamada a Ollama va solo a 127.0.0.1. |

## 5. Estrategia de latencia

- Un umbral de silencio de 600ms cierra la frase; con `small` int8 en una CPU moderna, una frase de pocos segundos se transcribe en 200-500ms, así que la latencia percibida es de aproximadamente 0.8-1.1s después de dejar de hablar.
- El modo streaming apunta a latencia sub-segundo por palabra: la ventana se re-decodifica cada 1.0s y las palabras estables se inyectan de inmediato. Solo se tipean palabras *confirmadas* (con acuerdo entre hipótesis), así que nunca hay que retractar nada de la app destino.
- La parte confirmada del buffer de audio se recorta continuamente, manteniendo acotado el tiempo de decodificación en sesiones de cualquier duración.
- El modelo se carga una vez al iniciar el daemon y queda caliente. `beam_size=1` (greedy) en streaming, `beam_size=5` en la pasada final por frase.
- GPU: autodetectada. CUDA → float16; CPU → int8.
- Idioma fijado en español por defecto (`language = "es"`), lo que evita la detección de idioma en cada decodificación y reduce latencia.

## 6. Manejo de fallas y sesiones largas

- El callback de audio solo encola; todo el trabajo pesado ocurre en un hilo trabajador. Si la cola desborda, se descarta el audio más viejo con un aviso en vez de crashear.
- Un tope de frase (30s) evita buffers sin límite si el VAD nunca ve silencio (ambientes ruidosos).
- Los errores del subproceso de inyección degradan a copia al portapapeles más una notificación de escritorio, en vez de morir.
- El daemon es candidato a servicio systemd de usuario (unit incluida) con `Restart=on-failure`.

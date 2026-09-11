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
5. **Post-procesamiento.** Whisper ya emite puntuación y mayúsculas; una capa conservadora protege primero tokens estructurados (números, URLs, correos, dominios, versiones, identificadores y código), normaliza solo prosa inequívoca, quita muletillas aisladas e interpreta comandos de voz. La reescritura es opcional y explícita (por reglas acotadas, o con un modelo de Ollama local).
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
    → filtro de frase: patrón conocido + baja confianza del mismo segmento
    → procesador_texto: protección de tokens estructurados, limpieza conservadora,
                        parseo de comandos de voz, reescritura opcional
    → inyector: pulsaciones sintéticas en la ventana con foco (X11 o Wayland)
```

Cada texto confirmado se distribuye de manera independiente al inyector, a
GuionAR y al transcript. Los resultados distinguen `inserted`, `copied`,
`mirrored`, `persisted`, `failed` y `skipped`; éxito significa que ParlAR
completó su operación local, no que un editor o GuionAR la haya confirmado.
Las acciones de teclado no se espejan como texto.

El control acepta una operación UTF-8 por conexión, delimitada por newline o
EOF, con máximo 4 KiB y timeout acotado por cliente. Los clientes se atienden
con concurrencia limitada para que uno silencioso no bloquee a los demás. El
endpoint conserva identidad de inode: solo su dueño puede retirarlo.

GuionAR conserva estado deseado y último estado enviado por separado. Una
conexión nueva recibe el VAD y parcial actuales; los textos finales históricos
no se reenvían. El transporte es best-effort, sin ACK, y respeta el límite
documentado de 2.000 caracteres por mensaje final.

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
- La frontera streaming conserva explícitamente las palabras ya emitidas del buffer actual. Una hipótesis nueva sólo puede extenderla si mantiene ese prefijo lexical y coincide con la hipótesis anterior. Mayúsculas y puntuación de frase en los bordes se consideran cosméticas, pero símbolos técnicos (`+`, `#`, `/`, `_` y puntos internos) conservan identidad. Si Whisper revisa una zona ya emitida, se congela esa contradicción en lugar de fabricar una corrección que los sinks append-only no podrían insertar en su posición original.
- La parte confirmada del buffer de audio se recorta continuamente, manteniendo acotado el tiempo de decodificación en sesiones de cualquier duración.
- El recorte usa el `end` de la última palabra confirmada únicamente mientras la hipótesis siga alineada, los timestamps sean válidos y el `start` de una palabra pendiente no se superponga. Después del recorte se exige agreement fresco sobre el nuevo origen temporal.
- El modelo se carga una vez al iniciar el daemon y queda caliente. `beam_size=1` (greedy) en streaming, `beam_size=5` en la pasada final por frase.
- GPU: autodetectada. CUDA → float16; CPU → int8.
- Idioma fijado en español por defecto (`language = "es"`), lo que evita la detección de idioma en cada decodificación y reduce latencia.
- En modo frase, cada segmento se evalúa de manera independiente. Un patrón conocido de alucinación solo se descarta si además cumple simultáneamente `no_speech_prob > 0.6` y `avg_logprob < -1.0`; ni el patrón ni la baja confianza por separado borran texto. No se inventan scores nuevos ni se aplica este filtro al camino streaming.
- El post-procesador sustituye temporalmente tokens con sintaxis estructurada por marcadores libres de colisiones, limpia la prosa restante y restaura cada token byte por byte. Ante puntuación ambigua (por ejemplo, `test.it`) prioriza fidelidad. La mayúscula inicial solo se agrega tras una transformación inequívoca —muletilla eliminada, regla de reescritura aplicada o signo `¿`/`¡` inicial—; el modo `none` no invoca Ollama.

## 6. Manejo de fallas y sesiones largas

- El callback de audio solo copia, numera y encola; todo el trabajo pesado ocurre en un hilo trabajador. La cola conserva un máximo de 500 frames (unos 10s con frames de 20ms). Si desborda, se descarta primero el frame más antiguo para preservar el audio reciente sin bloquear PortAudio. El descarte incrementa contadores por generación y deja un hueco observable en la secuencia; no se loguea cada frame.
- El primer frame entregado después de un hueco lleva una frontera de discontinuidad. El worker cierra la unidad contigua anterior si ya había voz confirmada —en streaming también finaliza y reinicia su buffer— o limpia VAD/pre-roll si aún no había frase. Recién después procesa el audio retenido como una unidad nueva. No se fabrican muestras ni silencio: el audio descartado no puede reconstruirse.
- `parlarctl estado` expone `drops`, discontinuidades, profundidad de cola y backlog aproximado (`frames_en_cola × frame_ms`). `audio=degradado` significa que existe un gap todavía pendiente de entregar; `audio=recuperado-con-perdida` indica que el pipeline cruzó esa frontera y volvió a operar, aunque la pérdida histórica de la sesión sigue visible. Los contadores se reinician al abrir una nueva generación.
- Un tope de frase (30s) evita buffers sin límite si el VAD nunca ve silencio (ambientes ruidosos).
- Los errores del subproceso de inyección degradan a copia al portapapeles más una notificación de escritorio, en vez de morir.
- El daemon es candidato a servicio systemd de usuario (unit incluida) con `Restart=on-failure`.

## 7. Lifecycle, sesiones y ownership

Cada apertura válida del micrófono crea una generación monotónica. El
callback de PortAudio captura esa generación y cada frame encolado queda
etiquetado con ella; el worker descarta cualquier frame cuya generación ya no
sea la activa. Las transiciones pedidas por UI, socket de control y hotkeys se
serializan en `App`, mientras que el worker conserva ownership exclusivo del
`Segmentador` y del estado de las estrategias de transcripción.

Los efectos externos (inyector, GuionAR, transcript y VAD observable) validan
la generación bajo el mismo lock que protege la escritura. STOP deja de
aceptar audio y el worker drena la cola y finaliza una frase abierta. Un START
pedido mientras ese drenaje sigue activo reemplaza la sesión e invalida sus
resultados pendientes. Un cambio de modo se guarda como solicitud escalar y
se adopta únicamente entre frases; una frase ya iniciada termina con su
estrategia original.

Shutdown es terminal: cambia primero a `SHUTTING_DOWN`, invalida la sesión,
cierra el mic, espera al único worker serial y recién después cierra control,
atajos, sinks y UI. Un fallo inesperado del worker deja el producto en
`ERROR`, apaga el indicador de grabación y se informa por el canal de control;
no se reinicia el worker automáticamente.

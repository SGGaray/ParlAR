# ParlAR

Dictado local, a nivel sistema, para Linux. Hablás y el texto limpio aparece tipeado en la ventana que tenga el foco. Sin nube, sin claves de API: nada sale de tu máquina.

> English version: [README.en.md](README.en.md)

ParlAR nació como FlowDictate; el nombre cambió, la arquitectura y la lógica no (ver el historial en CHANGELOG.md).

## Inicio rápido

```bash
cd parlar
./setup.sh                        # instala dependencias, venv y el modelo
source .venv/bin/activate
python -m parlar                  # español por defecto
```

Después:

1. Poné el foco en cualquier campo de texto (editor, navegador, chat, terminal).
2. Presioná **Ctrl+Alt+D** (X11) o tu atajo asignado (Wayland, ver abajo). El punto del indicador se pone rojo.
3. Hablá. Pausá un instante. El texto aparece en la app con foco.
4. Presioná el atajo de nuevo para detener.

## Controles

| Acción | X11 | Wayland / donde sea |
|---|---|---|
| Alternar grabación | Ctrl+Alt+D | `./parlarctl alternar` (asignalo a un atajo del DE) |
| Salir del daemon | Ctrl+Alt+Q | `./parlarctl salir` |
| Estado | - | `./parlarctl estado` |
| Cambiar modo en vivo | - | `./parlarctl modo streaming` / `modo frase` (se aplica en la próxima frontera de frase) |
| Reescritura en vivo | - | `./parlarctl reescritura formal` (ninguna/formal/conciso/correo) |
| Alternar con el mouse | click izquierdo en el punto | igual |
| Mover el indicador | arrastrar con click derecho | igual |

**Asignar el atajo en Wayland:** GNOME: Configuración → Teclado → Atajos personalizados → comando `/ruta/completa/parlarctl alternar`. KDE: Preferencias del sistema → Atajos → Agregar comando. Hyprland: `bind = CTRL ALT, D, exec, /ruta/parlarctl alternar`.

## Modos

- **frase** (por defecto): transcribe cada frase cuando pausás 600ms. Máxima precisión; latencia de 0.8 a 1.1s después de la pausa.
- **streaming**: las palabras aparecen mientras hablás, confirmadas con la política LocalAgreement, así nunca se retracta nada. Menor latencia percibida, algo más de CPU.

```bash
python -m parlar --modo streaming
```

## Integración con GuionAR (teleprompter)

Para el diseño completo del sistema (ParlAR + GuionAR, el protocolo del socket, y por qué son dos procesos separados), ver [GuionAR/ARCHITECTURE.md](https://github.com/SGGaray/GuionAR/blob/main/ARCHITECTURE.md).

ParlAR puede enviar el texto dictado y el estado de voz a [GuionAR](https://github.com/SGGaray/GuionAR), un overlay teleprompter que muestra lo que vas dictando cerca de la cámara.

```bash
# terminal 1: el teleprompter
cd GuionAR && python guionar.py --socket

# terminal 2: ParlAR con la integración activa
python -m parlar --guionar --modo streaming
```

Flags: `--guionar` (alias `--guionar-enabled`) activa el envío; `--guionar-socket RUTA` cambia el socket (default `$XDG_RUNTIME_DIR/guionar.sock`). También podés dejarlo fijo con `"guionar": true` en la config.

Es opcional y best-effort: si GuionAR no está corriendo, el dictado continúa.
Al reconectar se envía un snapshot del VAD y parcial actuales, pero no se
reproduce texto final histórico y no existe ACK del consumidor. El receptor
limita cada `text` a 2.000 caracteres. ParlAR no controla los permisos del
socket receptor; GuionAR documenta `0600` para su endpoint.

## Comandos de voz (modo frase)

Decilos exactos, como frase aislada: "nuevo párrafo", "punto y aparte", "nueva línea", "borra la última oración", "enviar", "detener dictado". Los equivalentes en inglés ("new paragraph", "delete last sentence", "send", "stop dictation") siguen funcionando.

`comando_enviar=false` —el default— bloquea **toda** acción que genere
Enter/Return, incluyendo nueva línea y nuevo párrafo. Activarlo autoriza esas
acciones sobre cualquier ventana enfocada. Las frases completamente citadas no
se ejecutan.

## Semántica de salida

Insertar, copiar, espejar en GuionAR y persistir el transcript son resultados
independientes. Un fallo no cancela los otros sinks. El fallback de clipboard
acumula la unidad streaming completa para que pueda pegarse manualmente, pero
copiar no equivale a insertar y no entra al historial de undo. Undo solo opera
sobre inserciones reconocidas y sigue dependiendo del foco/editor externo.

El control usa un socket Unix `0600`, una operación UTF-8 por conexión,
terminada por newline (EOF se acepta para clientes antiguos), con máximo 4 KiB.
Una segunda instancia no reemplaza un listener activo ni archivos ajenos; un
socket stale verificado sí se recupera. En `/tmp` la ruta incluye el UID.

## Modos de reescritura

Sin reescritura (el valor por defecto), la limpieza es conservadora: preserva
decimales, URLs, correos, dominios, versiones, identificadores, acrónimos y
código tal como los entregó Whisper. Solo corrige espaciado inequívoco,
muletillas aisladas y signos de apertura españoles. Una frase que coincide con
un comando pero está entre comillas se conserva como texto literal.

`--reescritura formal|conciso|correo` activa transformaciones explícitas. Las
reglas locales se limitan a equivalencias seguras (ok → de acuerdo, porfa → por
favor, finde → fin de semana; en conciso se limpian marcadores discursivos). Si
corrés [Ollama](https://ollama.com) local, poné `ollama_model` en la config (ej.
`"llama3.2:3b"`) y la reescritura pasa por ahí, siempre 127.0.0.1. Como toda
reescritura generativa, esa opción puede cambiar la formulación del dictado.

En modo frase, los patrones conocidos de alucinación se descartan únicamente
cuando el mismo segmento también tiene baja confianza acústica y textual. Un
URL o una frase como "suscríbete" con buena confianza se conserva.

## Configuración

`~/.config/parlar/config.json`. Generala con tus flags actuales:

```bash
python -m parlar --modelo small --idioma es --guardar-config
```

Perillas útiles: `model_size` (tiny/base/small/medium/large-v3), `silence_ms`, `vad_aggressiveness` (subilo a 3 en ambientes ruidosos), `hotkey_toggle`, `type_delay_ms`. Las claves del JSON se mantienen en inglés a propósito para no romper configs existentes.

## Ajuste de rendimiento

| Situación | Hacé esto |
|---|---|
| GPU NVIDIA | Se autodetecta (float16). Verificalo con el chequeo de CUDA del setup.sh. Con CUDA 12 puede hacer falta: `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` |
| CPU lenta | `--modelo base` o `--modelo tiny` |
| Máxima precisión | `--modelo medium` (necesita ~5GB de RAM en CPU int8) |
| Mínima latencia | `--modo streaming` + `--modelo base` + `--idioma es` fijo |
| Sesiones largas | Nada que hacer: el audio confirmado se recorta solo |

Fijar el idioma (`--idioma es`, ya es el defecto) evita la detección de idioma en cada decodificación y baja la latencia notablemente.

La captura usa una cola acotada de aproximadamente 10 segundos. Si el worker
no alcanza a consumirla, se descartan primero los frames más antiguos para
preservar audio reciente. ParlAR no puede reconstruir ese audio: numera los
frames, detecta el hueco y separa la unidad anterior de la posterior para no
presentarlas como una frase continua. `parlarctl estado` expone salud, drops,
discontinuidades, profundidad y backlog estimado; después de procesar la
frontera puede indicar `recuperado-con-perdida`, conservando visible que la
sesión no quedó completa.

## Correr como servicio (opcional)

```bash
mkdir -p ~/.config/systemd/user
cp scripts/parlar.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now parlar
```

## Alias en inglés

Por si te resulta más natural en inglés (o para quien colabore sin ser hispanohablante):

- Los comandos en inglés del socket (toggle/start/stop/status/mode/rewrite/quit) se aceptan como alias.
- Los flags CLI en inglés (`--model`, `--language`, `--mode`, etc.) se aceptan como alias.

## Solución de problemas

- **No se tipea nada (Wayland):** instalá `wtype`; en GNOME Wayland wtype puede estar bloqueado, usá `ydotool` con su daemon corriendo (`sudo systemctl enable --now ydotool`, agregate al grupo `input`). En el peor caso el texto queda en el portapapeles con una notificación.
- **El atajo no hace nada en Wayland:** es lo esperado, asigná `parlarctl alternar` en tu DE.
- **No encuentra el micrófono:** revisá `python -c "import sounddevice; print(sounddevice.query_devices())"` y fijá la entrada por defecto en la configuración de sonido.
- **Falló la compilación de webrtcvad:** no pasa nada, un VAD de energía adaptativo toma el control automáticamente.

## Estructura del proyecto

```
parlar/
├── README.md                    esta guía (instalación, uso, troubleshooting)
├── arquitectura.md              documento de diseño y diagrama de componentes
├── setup.sh                     instalador de un paso (Ubuntu/Debian + Fedora)
├── requirements.txt
├── parlarctl                    cliente de control (asignalo a atajos en Wayland)
├── scripts/parlar.service
├── parlar/
│   ├── __main__.py              entrada CLI
│   ├── config.py                dataclass de config + persistencia JSON
│   ├── capturador_audio.py      captura de mic + Segmentador VAD (puro, testeable)
│   ├── motor_transcripcion.py   faster-whisper: frase + streaming LocalAgreement
│   ├── procesador_texto.py      limpieza, comandos de voz, modos de reescritura
│   ├── inyector_salida.py       inyección xdotool / wtype / ydotool / portapapeles
│   ├── daemon_atajos.py         atajos globales pynput (X11)
│   ├── control.py               servidor de socket unix + cliente parlarctl
│   ├── indicador.py             punto tkinter siempre visible
│   └── app.py                   orquestador / máquina de estados
└── tests/                       lógica: python tests/run_tests.py
                                fidelidad: python -m unittest tests.test_text_fidelity
                                lifecycle: python -m unittest tests.test_lifecycle
                                streaming: python -m unittest tests.test_streaming_alignment
                                backpressure: python -m unittest tests.test_backpressure
```

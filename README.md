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
| Cambiar modo en vivo | - | `./parlarctl modo streaming` / `modo frase` |
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

Es opcional y fire-and-forget: si GuionAR no está corriendo, ParlAR funciona exactamente igual (los envíos se descartan en ~10 µs, sin bloqueos ni errores). Si GuionAR se cae a mitad de sesión, el dictado sigue y la conexión se retoma sola. En modo streaming, el texto confirmado se ve en blanco y la hipótesis todavía no confirmada aparece en gris como vista previa; el scroll avanza solo mientras el VAD detecta voz. Todo viaja por un socket Unix local con permisos `0600`: el modelo de privacidad no cambia.

## Comandos de voz (modo frase)

Decilos exactos, como frase aislada: "nuevo párrafo", "punto y aparte", "nueva línea", "borra la última oración", "enviar", "detener dictado". Los equivalentes en inglés ("new paragraph", "delete last sentence", "send", "stop dictation") siguen funcionando.

## Modos de reescritura

`--reescritura formal|conciso|correo`. Por reglas por defecto, optimizadas para español (ok → de acuerdo, porfa → por favor, finde → fin de semana; en conciso se limpian "básicamente", "o sea", "digamos", "viste"). Si corrés [Ollama](https://ollama.com) local, poné `ollama_model` en la config (ej. `"llama3.2:3b"`) y la reescritura pasa por ahí, siempre 127.0.0.1.

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
└── tests/                       correr: python tests/run_tests.py
```

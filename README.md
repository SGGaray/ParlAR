# ParlAR

Dictado local-primero, a nivel sistema, para Linux. Hablás y el texto limpio
aparece tipeado en la ventana que tenga el foco. No usa telemetría ni necesita
claves de API. La transcripción es local; solo hay tráfico de red si configurás
explícitamente un servicio externo, por ejemplo un `ollama_url` remoto.

> English version: [README.en.md](README.en.md)

ParlAR nació como FlowDictate; el nombre cambió, la arquitectura y la lógica no (ver el historial en CHANGELOG.md).

## Requisitos

- Debian/Ubuntu o Fedora, con una sesión gráfica Linux sobre X11 o Wayland.
- Python 3.12 o posterior. CI verifica 3.12; el desarrollo local también se
  valida actualmente con 3.14.
- PipeWire/PulseAudio y un dispositivo de entrada visible para PortAudio.
- Algún backend compatible con tu escritorio para insertar o copiar texto.

El instalador separa dependencias requeridas de integraciones opcionales:

| Capa | Requerido | Opcional con fallback |
|---|---|---|
| Sistema | Python, venv, PortAudio | tkinter, notificaciones, herramientas X11/Wayland/clipboard, headers de compilación |
| Python | faster-whisper, sounddevice, numpy, pynput | webrtcvad; sin él se usa el VAD de energía probado |

## Inicio rápido

```bash
git clone https://github.com/SGGaray/parlar.git
cd parlar
./setup.sh                        # paquetes, .venv y dependencias Python
source .venv/bin/activate
./scripts/check.sh                # gate completo sin hardware
python -m parlar                  # primer inicio; descarga Whisper small si falta
```

`./setup.sh` reutiliza una `.venv` válida y se puede ejecutar nuevamente. No
borra configuración, modelos ni transcripts. Una `.venv` incompleta se reporta
sin eliminarla. Si ya instalaste los paquetes del sistema, usá
`--skip-system-packages`. La descarga del modelo está separada: ocurre en el
primer inicio o explícitamente con
`./setup.sh --skip-system-packages --preload-model`.

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
socket receptor; GuionAR documenta `0600` para su endpoint. Antes de deduplicar
un VAD o parcial repetido, ParlAR comprueba sin bloquear si la conexión actual
terminó; un EOF/reset habilita la reconexión y el snapshot, mientras que una
conexión viva no duplica el estado.

## Comandos de voz (modo frase)

Decilos exactos, como frase aislada: "nuevo párrafo", "punto y aparte", "nueva línea", "borra la última oración", "enviar", "detener dictado". Los equivalentes en inglés ("new paragraph", "delete last sentence", "send", "stop dictation") siguen funcionando.

`comando_enviar=false` —el default— bloquea **toda** acción que genere
Enter/Return, incluyendo comandos, texto multilínea, fragmentos streaming y
resultados de reescritura. En ese caso, el texto multilínea se copia intacto al
portapapeles y se informa como copiado, no insertado; tampoco entra al historial
de undo. Activarlo autoriza esas acciones sobre cualquier ventana enfocada. Las
frases completamente citadas no se ejecutan, aun si llevan `.`, `?` o `!`
después de la comilla de cierre.

## Semántica de salida

Insertar, copiar, espejar en GuionAR y persistir el transcript son resultados
independientes. Un fallo no cancela los otros sinks. El fallback de clipboard
acumula la unidad streaming completa mientras nada fue insertado. En una
entrega mixta, el primer fallo congela el prefijo confirmado y copia como
recovery el sufijo continuo desde ese fragmento; el resto de la unidad ya no
se tipea para no crear huecos ni hacer retries ciegos. Ese sufijo no representa
la unidad completa y pegarlo junto al prefijo es una recuperación manual.
Copiar no equivale a insertar y no entra al historial de undo. Undo solo opera
sobre inserciones reconocidas: un fallo del backend conserva el elemento al
tope para reintentarlo, y solo un éxito avanza el historial. El editor externo
no ofrece una transacción: un fallo puede haber escrito o borrado parcialmente.

El control reserva la instancia con un lock `0600` antes de `bind` y usa un
socket Unix `0600` para transportar una operación UTF-8 por conexión, terminada
por newline (EOF se acepta para clientes antiguos), con máximo 4 KiB. Cada
cliente tiene timeout de recepción de 350 ms y deadline total de 1 segundo;
como máximo ocho handlers/sockets quedan activos. `detener()` cierra el listener
y los clientes parciales, espera los handlers ya admitidos y limpia socket y
lock solo si conserva sus identidades. Una segunda instancia no reemplaza una
reserva activa ni archivos ajenos; socket y lock stale sí se recuperan. En
`/tmp` la ruta incluye el UID.

## Modos de reescritura

Sin reescritura (el valor por defecto), la limpieza es conservadora: preserva
decimales con signo, horarios, URLs, correos, dominios, versiones,
identificadores, strings y regiones de código tal como los entregó Whisper.
Ante ambigüedad prioriza preservación. Solo corrige espaciado inequívoco,
muletillas aisladas fuera de regiones literales y signos de apertura españoles.
Una frase que coincide con un comando pero está entre comillas se conserva como
texto literal, incluso con puntuación de oración exterior.

`--reescritura formal|conciso|correo` activa transformaciones explícitas. Las
reglas locales se limitan a equivalencias seguras (ok → de acuerdo, porfa → por
favor, finde → fin de semana; en conciso solo se limpian marcadores discursivos
inequívocos y se conservan usos verbales ambiguos). Las regiones estructuradas
reconocidas se protegen durante la reescritura; si un modelo pierde un marcador,
se usa el fallback local conservador. Si corrés
[Ollama](https://ollama.com) local, poné `ollama_model` en la config (ej.
`"llama3.2:3b"`) y la reescritura pasa por ahí. `ollama_url` apunta a
`127.0.0.1` por defecto, pero es configurable: una URL remota envía allí el
texto a reescribir. Como toda reescritura generativa, esa opción puede cambiar
la formulación del dictado.

En modo frase, los patrones conocidos de alucinación se descartan únicamente
cuando el mismo segmento también tiene baja confianza acústica y textual. Un
URL o una frase como "suscríbete" con buena confianza se conserva.

## Configuración

`~/.config/parlar/config.json`. Generala con tus flags actuales:

```bash
python -m parlar --modelo small --idioma es --guardar-config
```

La configuración se valida completa antes de cargar el modelo o abrir el
micrófono. Los tipos JSON son estrictos (`false` no equivale a `"false"`), los
valores fuera de dominio impiden arrancar y el error se informa sin traceback.
La captura tiene un contrato fijo de 16 kHz; WebRTC VAD admite frames de 10,
20 o 30 ms. En ejecución solo `mode` y `rewrite_mode` cambian dinámicamente;
el resto requiere reiniciar. Las claves desconocidas se conservan en `extras`
sin poder reemplazar métodos internos.

`--guardar-config` reemplaza el JSON de forma atómica. Si ParlAR crea el
directorio usa permisos `0700`, y el archivo queda `0600` aun con una umask
permisiva.

Perillas útiles: `model_size` (tiny/base/small/medium/large-v3), `silence_ms`,
`vad_aggressiveness` (subilo a 3 en ambientes ruidosos), `hotkey_toggle` y
`type_delay_ms`. Las claves del JSON se mantienen en inglés por compatibilidad.

## Transcript local opcional

`--guardar-sesion` está apagado por defecto. Al activarlo, cada corrida crea
un archivo exclusivo con nombre
`parlar-AAAAMMDD-HHMMSS-microsegundos-token.txt`; nunca reutiliza ni agrega a
un transcript de otra instancia. Si crea el directorio lo hace `0700` y cada
archivo es `0600`. El contenido sigue siendo texto plano sin cifrar y ParlAR
no lo elimina automáticamente. Los logs normales registran estado, tiempos y
rutas, pero no el texto dictado. El transcript es un historial append-only de
emisiones confirmadas: no reconstruye Return, undo, estado del editor ni del
portapapeles.

La instalación/provisión sí puede descargar dependencias y modelos. En
ejecución, GuionAR usa IPC Unix local. El portapapeles y la aplicación que
recibe el tipeo están fuera del proceso y de las garantías de almacenamiento
de ParlAR.

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
presentarlas como una frase continua. Un `input_overflow` informado por
PortAudio crea la misma frontera, invalida cualquier frame parcial previo y se
cuenta por separado en `device_overflows`, sin inventar una cantidad de frames
perdidos. `parlarctl estado` expone salud de captura, drops de cola,
overflows de dispositivo, discontinuidades, profundidad y backlog estimado;
después de procesar la frontera puede indicar `recuperado-con-perdida`,
conservando visible que la sesión no quedó completa.

Durante la apertura, el callback puede aceptar audio apenas el backend está
activo, pero el worker espera a que el inicio termine antes de procesarlo. Si
la apertura falla, esa generación y sus frames se invalidan; el micrófono
vuelve a un estado reintentable. La salida de `parlarctl estado` mantiene
además health separado para STT y VAD (`healthy`, `degraded` o `recovered`),
contadores históricos y el último tipo de error, sin guardar audio ni texto.
El VAD falla abierto para priorizar no perder habla; mientras está degradado
puede aumentar el trabajo de STT y producir unidades falsas.

Streaming conserva su mínimo acústico para una unidad inicialmente demasiado
corta. Si un trim válido deja un remanente menor a ese mínimo, stop realiza una
última decodificación del remanente en vez de descartarlo automáticamente; la
salida sigue pasando por el acuerdo append-only y nunca retracta texto.

## Correr como servicio (opcional)

```bash
./setup.sh --skip-system-packages --install-service
systemctl --user enable --now parlar
```

El setup genera la unit con la ruta absoluta del checkout y el Python de su
`.venv`; no presupone `%h/parlar`. Conserva `UMask=0077` y no reemplaza una
unit ajena. Una repetición con el mismo checkout no produce cambios.

Un servicio de usuario necesita heredar una sesión gráfica utilizable y acceso
al audio. Las variables y permisos de display, PipeWire/PulseAudio, X11,
Wayland y clipboard varían entre escritorios; si la unit no dispone de ellos,
iniciá ParlAR desde una terminal de esa sesión. `Restart=on-failure` reinicia
el proceso principal, no sustituye el estado de health interno del worker.
El shutdown invalida primero la generación y termina el worker antes de cerrar
las salidas. Cada recurso recibe su intento de cleanup aunque otro falle; el
estado alcanza `closed` y conserva los tipos de error de cierre para diagnóstico.

## Validación

```bash
source .venv/bin/activate
./scripts/check.sh
```

Ese comando es la misma puerta usada por CI: sintaxis shell, compilación
Python, smokes de CLI, los 58 checks legacy, todas las suites unittest, IPC y
`git diff --check`. No necesita micrófono, display, modelo descargado, Ollama,
GuionAR, clipboard ni systemd reales.

Para una validación manual posterior con hardware: confirmar apertura del
micrófono, dictado por frase, streaming, stop, restart, GuionAR opcional,
fallback de clipboard y shutdown.

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
├── scripts/check.sh              gate local/CI sin hardware
├── scripts/parlar.service.in     template de la unit generada por setup
├── scripts/render_service.py     render seguro con la ruta real del checkout
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
└── tests/                       suites por invariantes; ejecutar scripts/check.sh
```

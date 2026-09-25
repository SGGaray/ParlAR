# ParlAR

Dictado local, a nivel sistema, para Linux. ParlAR escucha solo mientras lo
activás, transcribe con faster-whisper y entrega el texto a la aplicación que
tiene el foco. No tiene telemetría ni requiere una cuenta o una API cloud.

La captura y la transcripción son locales. Solo sale texto de la máquina si
configurás deliberadamente un servicio externo, por ejemplo un `ollama_url`
remoto. GuionAR, cuando se habilita, usa un socket Unix local.

> English: [README.en.md](README.en.md)

ParlAR 1.0.x es la línea estable para Linux. v1.0.0 fue validada físicamente en
Fedora/X11 con LightDM y NVIDIA CUDA; el camino CPU cuenta con validación previa
y cobertura automatizada. Wayland dispone de backends y bindings documentados,
pero no fue validado físicamente para esta release. El demo web, si lo encontrás
enlazado desde el proyecto, es solo ilustrativo: no ejecuta esta aplicación
nativa ni su pipeline de audio, hotkeys e inyección.

## Requisitos

- Linux de escritorio (Debian/Ubuntu y Fedora son los caminos soportados por
  los scripts de provisioning).
- Python 3.12 o posterior y soporte para `venv`.
- PortAudio con un micrófono visible mediante PipeWire o PulseAudio.
- En X11: `xdotool` y `xclip` para inyección Unicode.
- En Wayland: `wtype` o `ydotool`; si ninguno puede inyectar, ParlAR degrada a
  copia por portapapeles cuando hay una herramienta compatible.
- Opcionales: Tk para el indicador, `notify-send` para notificaciones y GPU
  NVIDIA para acelerar Whisper.

Paquetes habituales:

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv python3-dev portaudio19-dev \
  python3-tk libnotify-bin xdotool xclip wtype wl-clipboard

# Fedora
sudo dnf install python3 python3-devel portaudio-devel python3-tkinter \
  libnotify xdotool xclip wtype wl-clipboard
```

`ydotool` es una alternativa Wayland basada en uinput y requiere su daemon y
los permisos que indique tu distribución.

## Instalación de usuario

```bash
git clone https://github.com/SGGaray/ParlAR.git
cd ParlAR
./install.sh --preload-model
```

El instalador crea un entorno aislado bajo
`$XDG_DATA_HOME/parlar/venv` (por defecto `~/.local/share/parlar/venv`), instala
`parlar` y `parlarctl` en `~/.local/bin` mediante enlaces y genera un launcher
de escritorio. No activa servicios por sorpresa. Si `~/.local/bin` no está en
tu `PATH`, agregalo en la configuración de tu shell o ejecutá los comandos por
su ruta completa.

`--preload-model` descarga Whisper `small` durante la instalación; sin ese
flag, la primera ejecución lo descarga. La instalación y esa descarga sí
requieren red. El runtime normal no la necesita.

Para instalar además la integración de servicio:

```bash
./install.sh --install-service
```

La unit queda deliberadamente **static**: systemd administra
start/stop/restart/status, pero no la habilita en `default.target`. El arranque
automático lo dispara `~/.config/autostart/parlar-systemd.desktop` después del
login gráfico, cuando la sesión ya publicó `DISPLAY` y `XAUTHORITY`. Esto evita
arranques prematuros en escritorios donde `graphical-session.target` permanece
inactivo. El launcher normal de aplicaciones sigue siendo un artefacto separado.

Para iniciarlo inmediatamente desde la terminal gráfica actual:

```bash
systemctl --user start parlar.service
systemctl --user status parlar.service
```

No uses `systemctl --user enable`: una reinstalación limpia enlaces legacy de
`default.target` para evitar doble autostart. El servicio usa la instalación
autocontenida, no el checkout.

`setup.sh` queda disponible para desarrollo desde el checkout. Instala una
`.venv` local y puede preparar dependencias del sistema; no es el camino
primario de distribución de usuario.

## Primera ejecución

```bash
parlar
```

En X11, el control predeterminado es **Ctrl derecho + Shift derecho**:

- Mantené ambas teclas: START inmediato, sin esperar al VAD.
- Hablá mientras las sostenés.
- Soltá el combo: STOP inmediato y finalización de la unidad.
- Dos toques cortos dentro de 300 ms activan el modo continuo en la misma
  sesión. Otro doble toque lo desactiva y detiene.
- **Esc** cancela la sesión actual: descarta audio y resultados pendientes, no
  hace undo y no borra texto ya confirmado.

El indicador, cuando está disponible, muestra el estado y permite alternar con
click izquierdo. `--sin-indicador` ejecuta ParlAR sin prometer esa UI; los
controles por teclado e IPC siguen disponibles.

## Control con `parlarctl`

```bash
parlarctl iniciar
parlarctl detener
parlarctl cancelar
parlarctl alternar
parlarctl estado
parlarctl modo streaming
parlarctl reescritura formal
parlarctl salir
```

También se aceptan `start`, `stop`, `cancel`, `toggle`, `status`, `mode`,
`rewrite` y `quit`. CANCEL y STOP son operaciones distintas: STOP finaliza lo
aceptado; CANCEL invalida la generación y descarta lo pendiente.

El socket está en `$XDG_RUNTIME_DIR/parlar.sock`, usa permisos `0600` y se
elimina durante el shutdown ordenado. `SIGTERM`, incluido el enviado por
systemd, recorre el mismo cleanup normal.

### Wayland

ParlAR no finge una captura global que el protocolo Wayland no ofrece. Asigná
un atajo de tu compositor al comando absoluto:

```text
/home/TU_USUARIO/.local/bin/parlarctl alternar
```

GNOME y KDE permiten crear un atajo personalizado desde sus paneles de
configuración. En Hyprland, el equivalente genérico es:

```text
bind = CTRL SHIFT, D, exec, ~/.local/bin/parlarctl alternar
```

Podés crear bindings separados para `iniciar`, `detener` y `cancelar` si
preferís una frontera explícita.

## Modos

`utterance` es el modo predeterminado: una pausa de 600 ms cierra una frase.
`streaming` confirma prefijos mientras hablás sin retractar texto ya entregado.

```bash
parlar --modo streaming
parlarctl modo utterance
```

El cambio por IPC se aplica en una frontera segura de unidad.

## Configuración

La configuración vive en `$XDG_CONFIG_HOME/parlar/config.json` (por defecto
`~/.config/parlar/config.json`). El formato tiene `schema_version`, se valida
antes de crear recursos y conserva claves desconocidas bajo `extras`. Una
config antigua sin versión se migra sin adivinar si su hotkey era un default o
una elección del usuario.

```bash
parlar --ruta-config
parlar --mostrar-config
parlar --mostrar-atajo
parlar --help
```

Los flags pueden persistirse de forma atómica con `--guardar-config`. Agregar
una acción de inspección evita arrancar el daemon durante ese cambio:

```bash
parlar --modo streaming --inyector auto --guardar-config --mostrar-config
parlar --atajo '<ctrl_r>+<shift_r>' --guardar-config --mostrar-atajo
```

Para vocabulario especializado, nombres propios o siglas:

```bash
parlar \
  --termino-contexto ParlAR \
  --termino-contexto GuionAR \
  --guardar-config --mostrar-config
```

Repetir `--termino-contexto` reemplaza la lista configurada. `--sin-contexto`
la desactiva. Los términos se incorporan localmente al contexto STT y no
habilitan telemetría.

La captura usa hoy el dispositivo de entrada predeterminado de PortAudio. No
hay todavía selector de micrófono propio; elegilo en la configuración de sonido
del escritorio y reiniciá ParlAR.

## Inyección, portapapeles y undo

En X11, ParlAR preserva Unicode con:

```text
xclip → Ctrl+V sintético
```

El clipboard es transporte transitorio: ParlAR no garantiza conservar su
contenido previo ni que el texto transportado siga disponible después del
paste. Tampoco intenta restaurarlo. El backend explícito
`--inyector clipboard` es diferente: es copy-only y su contrato sí deja el
texto disponible para pegado manual.

Un exit code exitoso de `xdotool` no confirma que la aplicación enfocada haya
insertado el texto. Por seguridad, un paste X11 no entra al historial de undo
destructivo e invalida el límite anterior. Así, “borra la última oración” no
puede enviar Backspace contra contenido ajeno después de una entrega no
verificable. `wtype` y `ydotool` conservan su comportamiento actual; ningún
backend puede ofrecer una transacción con el editor externo.

## Comandos de voz y Return

Las órdenes se reconocen solo como utterances completas y exactas. Entre los
aliases estables están “mandar mensaje”, “enviar mensaje”, “salto de línea” y
“salto de párrafo”, además de los comandos cortos e ingleses existentes. No se
usa fuzzy matching ni heurística fonética.

Las acciones que generan Enter/Return están bloqueadas por defecto mediante
`"comando_enviar": false`. Esto incluye enviar, nueva línea, nuevo párrafo,
multiline y salidas reescritas. Activarlo puede ejecutar contenido en una
terminal o enviar un formulario; revisá [SECURITY.md](SECURITY.md) antes de
hacerlo.

## CUDA y CPU

Con `device=auto`, ParlAR usa CUDA cuando CTranslate2 la encuentra y cae a CPU
int8 si no está disponible. El bootstrap del runtime NVIDIA del entorno es
acotado y no se reinicia recursivamente. Para forzar un camino:

```bash
parlar --dispositivo cpu
parlar --dispositivo cuda
```

La disponibilidad real de CUDA depende del driver, las bibliotecas y la GPU de
la máquina; verificala después de instalar. CPU es el fallback soportado, no un
modo de error.

## GuionAR opcional

[GuionAR](https://github.com/SGGaray/GuionAR) puede recibir VAD, parciales y
texto final mediante IPC Unix local:

```bash
parlar --guionar --modo streaming
```

Es best-effort y no forma parte del camino crítico: si no está corriendo,
ParlAR continúa dictando. Una reconexión publica el snapshot actual, no
reproduce texto final histórico. El transporte IPC tiene cobertura
automatizada; la aplicación GuionAR real no fue validada end-to-end para
v1.0.0.

## Privacidad y transcripts

Los logs normales contienen estados, tiempos y tipos de error, no audio ni
texto dictado. `--guardar-sesion` es opt-in y escribe texto plano `0600` bajo
`$XDG_DATA_HOME/parlar/sesiones/`; ParlAR no cifra ni elimina esos archivos.
Leé [SECURITY.md](SECURITY.md) para el modelo de amenaza completo.
Para reportar una vulnerabilidad, usá el canal privado indicado allí. No
publiques detalles sensibles en un issue.

## Solución de problemas

- **`parlar` no aparece:** comprobá que `~/.local/bin` esté en `PATH`.
- **Micrófono ocupado o ausente:** verificá el dispositivo predeterminado con
  `~/.local/share/parlar/venv/bin/python -c 'import sounddevice; print(sounddevice.query_devices())'`,
  o usá la configuración de sonido del escritorio.
- **Wayland no tipea:** probá `wtype`; si el compositor no lo admite, configurá
  `ydotool` o usá `--inyector clipboard` para pegado manual.
- **No hay hotkey global en Wayland:** es el diseño esperado; usá un binding
  del compositor con `parlarctl`.
- **El servicio no ve display/audio:** ejecutá `systemctl --user status parlar`
  y `journalctl --user -u parlar`; si falta el entorno gráfico, lanzá `parlar`
  desde una terminal de esa sesión. No habilites la unit en `default.target`;
  reinstalá con `--install-service` para restaurar el autostart XDG.
- **Esc aparece como `^[` en la aplicación enfocada:** pynput observa Esc pero
  no puede suprimir solo esa tecla sin un grab global agresivo. CANCEL se
  ejecuta, pero el passthrough queda como limitación conocida.

## Desinstalación

Desde un checkout de la misma versión:

```bash
./uninstall.sh
```

El script desactiva y elimina solo la unit y el launcher marcados como
generados por ParlAR, quita sus enlaces y elimina únicamente el entorno
administrado `$XDG_DATA_HOME/parlar/venv`. No borra recursivamente la raíz de
datos: conserva la configuración, los transcripts y otros archivos hermanos
no administrados. Para borrar datos deliberadamente:

```bash
rm -r ~/.config/parlar
rm -r ~/.local/share/parlar/sesiones
```

Revisá las rutas si personalizaste `XDG_CONFIG_HOME` o `XDG_DATA_HOME`.

## Desarrollo y validación

```bash
./setup.sh
source .venv/bin/activate
./scripts/check.sh
```

El gate verifica shell y bytecode, entrypoints, configuración, lifecycle,
cancelación, IPC Unix, inyección simulada, renderer systemd, desktop entry y
tests legacy/unitarios. No necesita micrófono, display, modelo, clipboard,
GuionAR ni una sesión systemd reales.

La validación que sí requiere hardware y percepción humana está separada en
[docs/release-readiness-1.0.md](docs/release-readiness-1.0.md).

## Limitaciones conocidas

- Esc puede llegar también a la aplicación enfocada en X11.
- La selección de micrófono depende del dispositivo predeterminado del sistema.
- Los atajos globales X11 no existen en Wayland; se usan bindings del
  compositor.
- La entrega a una aplicación externa no es transaccional. X11 paste es
  deliberadamente no elegible para undo destructivo.
- Wayland y la integración con la aplicación GuionAR real no fueron validados
  físicamente para v1.0.0; el estado exacto está en el documento de readiness.

## Licencia

MIT. Ver [LICENSE](LICENSE).

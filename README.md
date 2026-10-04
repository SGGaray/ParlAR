<p align="center">
  <img src="parlar/assets/parlar.svg" width="96" height="96" alt="Ícono de ParlAR">
</p>

# ParlAR

**Dictado por voz local para Linux.** Hablás, ParlAR escribe en la aplicación
que tenés abierta.

Sin cuenta. Sin telemetría. El reconocimiento de voz corre en tu computadora.

> English: [README.en.md](README.en.md)

## Qué hace

- **Dictado en cualquier aplicación.** Mantené el atajo, hablá y soltalo: el
  texto aparece donde está el cursor (editor, navegador, chat, terminal).
- **Dos formas de dictar.** Mantener el atajo mientras hablás, o un doble
  toque para dictado continuo hasta que vuelvas a hacer doble toque.
- **Cancelar sin escribir.** `Esc` descarta lo que todavía no se escribió.
- **Por frases o incremental.** Por frases escribe al hacer una pausa;
  Incremental va escribiendo mientras hablás.
- **Comandos de voz** para borrar la última oración, detener el dictado o
  hacer saltos de línea (ver [Comandos de voz](#comandos-de-voz)).
- **Palabras y nombres propios.** Le podés indicar términos, siglas o
  nombres que tiene que reconocer mejor.
- **Configuración** en una ventana propia, con un ícono en la bandeja del
  sistema y un indicador visual mientras dictás.
- **Funciona con [GuionAR](https://github.com/SGGaray/GuionAR)**, el
  teleprompter: si está abierto, sigue tu voz sin configurar nada.

## Compatibilidad

| | Estado |
|---|---|
| Linux con X11 | Soportado. Validado en Fedora con X11. |
| Linux con Wayland | Funciona con limitaciones: el atajo global lo tiene que asignar tu escritorio (ver [Wayland](#wayland)). No fue validado a mano. |
| Procesador (CPU) | Soportado en cualquier equipo. Es el modo por defecto si no hay GPU compatible. |
| GPU NVIDIA | Opcional. Si está disponible, ParlAR la usa para transcribir más rápido. |
| Debian/Ubuntu y Fedora | Distribuciones con instrucciones de instalación. |
| Windows, macOS | No soportados. |

Necesitás Python 3.12 o posterior (lo trae la mayoría de las distribuciones
actuales) y un micrófono que funcione en tu escritorio.

## Instalar

Por ahora ParlAR se instala desde una copia de este repositorio con un
instalador incluido. El instalador deja ParlAR en tu cuenta de usuario, con
su propio entorno, y agrega la entrada al menú de aplicaciones. No hace falta
`sudo` salvo para los paquetes del sistema.

**1. Paquetes del sistema**

```bash
# Debian / Ubuntu
sudo apt install git python3 python3-venv python3-dev portaudio19-dev \
  python3-tk python3-gi gir1.2-gtk-3.0 libnotify-bin xdotool xclip wtype \
  wl-clipboard

# Fedora
sudo dnf install git python3 python3-devel portaudio-devel python3-tkinter \
  python3-gobject gtk3 libnotify xdotool xclip wtype wl-clipboard
```

**2. Descargar e instalar**

```bash
git clone https://github.com/SGGaray/ParlAR.git
cd ParlAR
./install.sh --preload-model
```

`--preload-model` descarga el modelo de voz durante la instalación (varios
cientos de MB). Sin esa opción se descarga la primera vez que abrís ParlAR.
Si el instalador detecta una GPU NVIDIA compatible, instala también lo
necesario para usarla; `--cpu-only` lo evita.

Para que ParlAR pueda **iniciarse solo al entrar a tu sesión**, instalá con:

```bash
./install.sh --preload-model --install-service
```

Después lo activás o desactivás desde Configuración → Aplicación.

Guardá la carpeta `ParlAR`: se usa para actualizar y desinstalar.

## Abrir ParlAR

Buscá **ParlAR** en el menú de aplicaciones. Al abrirlo:

- se abre la ventana de **Configuración**;
- ParlAR queda funcionando en segundo plano, listo para dictar, aunque
  cierres esa ventana.

Si ParlAR ya estaba funcionando, abrirlo desde el menú sólo trae la
Configuración. También podés abrirla con un clic en el ícono de la bandeja.

El menú del ícono muestra el estado y permite **Pausar**, abrir
**Configuración…**, **Reiniciar** y **Salir**.

## Dictar

El atajo predeterminado es **Ctrl derecho + tecla Super/Windows derecha**.
Podés cambiarlo en Configuración → Dictado. Si venís de una versión
anterior, ParlAR conserva el atajo que ya tenías; la Configuración muestra el
vigente.

| Acción | Qué pasa |
|---|---|
| Mantener el atajo | Empieza a escuchar en el momento. |
| Soltar el atajo | Termina y escribe lo que dijiste. |
| Doble toque rápido | Dictado continuo: escucha hasta el próximo doble toque. |
| `Esc` | Cancela lo pendiente. No borra lo que ya se escribió. |

Mientras dictás aparece un indicador chico que no toma el foco de la
aplicación en la que estás escribiendo.

### Comandos de voz

Se reconocen sólo cuando la frase completa es el comando:

| Decí | Hace |
|---|---|
| «borrar la última oración» | Borra la última oración que escribió ParlAR, cuando es seguro hacerlo. |
| «detener dictado» | Termina el dictado. |
| «nueva línea», «salto de línea» | Salto de línea. * |
| «nuevo párrafo», «punto y aparte» | Párrafo nuevo. * |
| «enviar», «mandar mensaje» | Presiona Enter. * |

\* Los comandos que presionan Enter están **desactivados por defecto**: en
una terminal o un chat, una frase mal reconocida podría ejecutar o enviar
algo. Para activarlos, agregá `"comando_enviar": true` en
`~/.config/parlar/config.json` y reiniciá ParlAR. Leé
[SECURITY.md](SECURITY.md) antes de hacerlo.

### Wayland

Wayland no permite que una aplicación capture un atajo global. Asigná un
atajo personalizado en la configuración de teclado de tu escritorio (GNOME,
KDE y otros lo permiten) que ejecute:

```text
~/.local/bin/parlarctl alternar
```

Cada pulsación inicia o termina el dictado. Si tu escritorio pide una ruta
completa, usá `/home/TU_USUARIO/.local/bin/parlarctl alternar`.

## Configuración

Los cambios se guardan desde la ventana. Algunos se aplican al momento y
otros piden reiniciar ParlAR; la ventana lo indica y ofrece el botón
**Reiniciar ParlAR**.

**Dictado**
- **Atajo de dictado**: cambiar la combinación o volver a la predeterminada.
- **Estrategia**: Por frases o Incremental.
- **Reescritura**: Sin reescritura, Formal, Conciso o Correo.
- **Idioma**: código como `es` o `en`. Vacío detecta el idioma.
- **Palabras y nombres**: términos que ParlAR debería reconocer mejor, uno
  por línea.

**Micrófono**
- Elegir el micrófono o usar el predeterminado del sistema.
- **Probar micrófono** para ver el nivel antes de dictar. Ese audio no se
  guarda.

**GuionAR**
- Estado de la conexión con GuionAR.
- **Integrar con GuionAR** y **Usar GuionAR como salida exclusiva** (ver
  [Usar con GuionAR](#usar-con-guionar)).

**Aplicación**
- **Indicador**: mostrarlo u ocultarlo, y elegir dónde aparece.
- **Iniciar ParlAR al iniciar sesión**: disponible si instalaste con
  `--install-service`.
- **Conservar transcripciones**: guarda una copia local de cada dictado.
  Apagado por defecto.

**Avanzado**
- **Modelo**: tamaño del modelo de voz. Más grande suele ser más preciso y
  más lento.
- **Acelerador**: Automático, CPU o NVIDIA CUDA. Automático usa la GPU si
  está disponible y si no el procesador.
- **Precisión de cálculo**: formato numérico del modelo. Automático
  funciona bien en la mayoría de los casos.
- **Método de escritura**: cómo se entrega el texto (automático, X11,
  Wayland, teclado virtual o sólo portapapeles).

La configuración se guarda en `~/.config/parlar/config.json`.

## Usar con GuionAR

[GuionAR](https://github.com/SGGaray/GuionAR) es un teleprompter que puede
seguir tu voz mientras leés un guion, o mostrar lo que dictás en vivo.

- **No hay que configurar nada.** Si GuionAR está abierto, ParlAR se conecta
  solo. No importa cuál abras primero.
- **Sigue tu voz en vivo**: GuionAR recibe lo que vas diciendo mientras
  hablás, no sólo al terminar cada frase.
- **Si GuionAR no está abierto**, ParlAR funciona normalmente. Si lo cerrás y
  lo volvés a abrir, ParlAR se reconecta sin reiniciarse.
- **Salida exclusiva** (activada por defecto): mientras GuionAR está
  conectado, ParlAR le envía el dictado sólo a GuionAR y no escribe en la
  aplicación que tenés abierta. Sin GuionAR, escribe normalmente. El cambio
  se aplica desde la siguiente frase.
- **Para desactivar la integración**, apagá **Integrar con GuionAR** en
  Configuración → GuionAR.

Lo que se dicta mientras GuionAR está cerrado no se le envía después.

## Privacidad

- **El reconocimiento de voz es local.** El audio se procesa en tu
  computadora y no se envía a ningún servicio.
- **El audio no se guarda.**
- **Sin telemetría, sin cuenta.**
- **Transcripciones opcionales.** Sólo si activás **Conservar
  transcripciones**: se guardan como texto sin cifrar en
  `~/.local/share/parlar/sesiones/`, accesibles sólo para tu usuario.
  ParlAR no las borra; las borrás vos.
- **Registros.** Los mensajes de diagnóstico incluyen estados y tiempos, no
  audio ni texto dictado.
- **Configuración** en `~/.config/parlar/config.json`.
- **Red.** La instalación y la primera descarga del modelo usan internet.
  Después, dictar no la necesita.
- **Reescritura con Ollama (opcional, apagada).** Si configurás un modelo de
  Ollama para reescribir el texto, ParlAR le envía ese texto. Por defecto
  apunta a un Ollama en tu propia computadora; **si configurás una dirección
  remota, el texto sale de tu equipo** hacia ese servicio.
- **El portapapeles y la aplicación de destino** son programas aparte:
  ParlAR no controla qué hacen con el texto que reciben.

Los detalles del modelo de seguridad están en [SECURITY.md](SECURITY.md).

## Actualizar

Por ahora la actualización se hace desde la misma carpeta `ParlAR` que usaste
para instalar:

```bash
cd ParlAR
git pull
./install.sh
```

Usá las mismas opciones que en la instalación (por ejemplo
`--install-service`). Tu configuración y tus transcripciones se conservan.
Después, reiniciá ParlAR desde el menú de la bandeja o desde Configuración.

## Desinstalar

Cerrá ParlAR (**Salir** en el menú de la bandeja) y, desde la carpeta
`ParlAR`:

```bash
./uninstall.sh
```

**Elimina:** la aplicación instalada y su entorno, los comandos `parlar` y
`parlarctl`, la entrada del menú, el ícono y, si existía, el inicio
automático con la sesión.

**Conserva:** tu configuración (`~/.config/parlar/`) y las transcripciones
guardadas (`~/.local/share/parlar/sesiones/`).

**También puede quedar** el modelo de voz descargado, en la caché de
Hugging Face (`~/.cache/huggingface/`). Ocupa varios cientos de MB y otras
aplicaciones pueden compartirla.

Para borrar también tus datos:

```bash
rm -r ~/.config/parlar
rm -r ~/.local/share/parlar/sesiones
```

Después podés borrar la carpeta `ParlAR`.

## Solución de problemas

- **No aparece en el menú o el comando `parlar` no existe.** Cerrá y volvé a
  abrir la sesión. Si usás la terminal, agregá `~/.local/bin` a tu `PATH`.
- **No escucha, o escucha el micrófono equivocado.** En Configuración →
  Micrófono elegí el dispositivo y usá **Probar micrófono**.
- **Escucha pero no escribe nada.** Comprobá que no esté activa la salida
  exclusiva con GuionAR abierto. Si no, probá otro **Método de escritura** en
  Configuración → Avanzado; **Portapapeles** te deja el texto para pegarlo a
  mano.
- **En Wayland el atajo no hace nada.** Es esperado: asigná el atajo desde tu
  escritorio (ver [Wayland](#wayland)).
- **`Esc` aparece como `^[` en una terminal.** ParlAR cancela igual, pero no
  puede impedir que la tecla también llegue a la aplicación. Es una
  limitación conocida.
- **No ves el ícono en la bandeja.** Algunos escritorios (por ejemplo GNOME)
  necesitan una extensión de indicadores. ParlAR funciona igual; la
  Configuración se abre desde el menú de aplicaciones.
- **No arranca solo al iniciar sesión.** Activá **Iniciar ParlAR al iniciar
  sesión** en Configuración → Aplicación. Si no está disponible, reinstalá
  con `--install-service`.
- **Va lento.** En Configuración → Avanzado probá un modelo más chico, o
  revisá que el Acelerador use la GPU si tenés una NVIDIA.

## Limitaciones conocidas

- Linux únicamente. Validado a mano en Fedora con X11; Wayland no fue
  validado a mano.
- En Wayland no hay atajo global propio: se usa un atajo del escritorio.
- `Esc` cancela el dictado pero también puede llegar a la aplicación que
  tiene el foco.
- ParlAR no puede confirmar que la aplicación de destino haya insertado el
  texto. Por seguridad, «borrar la última oración» no actúa cuando no hay
  certeza de qué se escribió.
- Como todo reconocimiento de voz, puede equivocarse o, con ruido de fondo,
  escribir cosas que nadie dijo. ParlAR filtra algunos casos conocidos, pero
  no todos.
- Si hay otras voces en el ambiente, ParlAR también las transcribe.
- Las transcripciones guardadas no están cifradas.

## Estado del proyecto

ParlAR es una aplicación mantenida como producto. Este repositorio contiene
su código fuente y está organizado principalmente para instalarla, usarla e
inspeccionar el código, no como un proyecto de desarrollo comunitario. Por
eso no hay guía de contribución ni roadmap público.

## Código fuente y licencia

El código se publica bajo licencia [MIT](LICENSE).

## Reportar un problema

- **Errores, problemas de instalación o de compatibilidad:** abrí un
  [issue](https://github.com/SGGaray/ParlAR/issues/new/choose) y elegí el
  tipo que corresponda.
- **Vulnerabilidades de seguridad:** no las publiques en un issue. Seguí
  [SECURITY.md](SECURITY.md).

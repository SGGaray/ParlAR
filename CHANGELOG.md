# Changelog: ParlAR

## [Unreleased]

## [1.1.0] - 2026-10-04

### Agregado
- **Ventana de Configuración.** Todo lo que antes requería editar la
  configuración o usar opciones de línea de comandos se ajusta desde una
  ventana con cinco secciones: Dictado, Micrófono, GuionAR, Aplicación y
  Avanzado. Indica cuándo un cambio necesita reiniciar ParlAR y permite
  reiniciarlo desde ahí.
- **Elegir y probar el micrófono.** ParlAR recuerda el micrófono elegido y
  ofrece una prueba de nivel que no guarda audio.
- **Cambiar el atajo de dictado** desde la Configuración, presionando la
  combinación deseada.
- **Entrada en el menú de aplicaciones.** Abrir ParlAR desde el menú abre la
  Configuración y deja ParlAR funcionando si todavía no lo estaba.
- **Ícono en la bandeja del sistema** con el estado actual, pausa,
  Configuración, reinicio y salida.
- **Inicio con la sesión** activable desde la Configuración cuando ParlAR se
  instaló con el inicio automático.
- **Reinicio seguro.** Si hay un dictado en curso, ParlAR avisa y ofrece
  terminarlo antes de reiniciar, o no reiniciar todavía.
- **Integración automática con GuionAR.** Si GuionAR está abierto, ParlAR se
  conecta solo, en cualquier orden, y se reconecta si GuionAR se cierra y se
  vuelve a abrir. Se puede desactivar desde la Configuración.
- **Seguimiento en vivo.** GuionAR recibe lo que vas diciendo mientras
  hablás, no sólo al terminar cada frase.
- **GuionAR como salida exclusiva** (activado por defecto): mientras GuionAR
  está conectado, el dictado va sólo a GuionAR y no se escribe en la
  aplicación con foco.
- **Identidad visual propia**: ícono de la aplicación, íconos de bandeja con
  estado y Configuración con la marca de ParlAR.
- **Release descargable para Linux x86_64** con suma SHA256: se instala con
  `./install.sh` sin clonar el repositorio ni compilar, en Python 3.12, 3.13
  o 3.14.
- **Actualización segura**: cada versión se instala al lado de la anterior y
  sólo se activa cuando quedó completa; si algo falla, la versión vigente no
  cambia. Las instalaciones 1.0.x se migran conservando configuración, atajo
  y transcripciones.
- **`parlar-uninstall`** desinstala sin necesitar la release; con
  `--purge-data` borra también la configuración y las transcripciones.
- **`parlar --version`** muestra la versión instalada.

### Cambiado
- **Nuevo atajo predeterminado: Ctrl derecho + Super derecha**, para evitar
  choques con atajos del escritorio. Quien ya tenía un atajo configurado lo
  conserva.
- El indicador de dictado ahora es una onda breve que aparece sólo mientras
  ParlAR escucha y no toma el foco.
- Mensajes de estado y de recuperación más claros, y mejor uso con teclado.

### Corregido
- La reescritura con Ollama tiene un tiempo máximo total y un límite de
  tamaño de respuesta, para que un servicio lento o defectuoso no deje el
  dictado esperando.
- La conexión con GuionAR sólo se acepta si el otro proceso pertenece al
  mismo usuario.
- Las herramientas de portapapeles ya no vuelcan su salida en los registros.
- Instalación y desinstalación usan exactamente las mismas rutas.

### Documentación
- README reescrito para quien usa ParlAR: instalación, uso, Configuración,
  privacidad, actualización y desinstalación.
- Política de reporte privado de vulnerabilidades.

## [1.0.1] - 2026-09-24

### Corregido
- La desinstalación elimina solo el entorno administrado y preserva
  transcripts, configuración y archivos no administrados bajo la raíz de datos.
- Instalación y desinstalación normalizan de forma consistente la raíz
  administrada; el uninstall reconoce enlaces propios legacy con separadores
  redundantes sin seguir symlinks de la raíz ni eliminar targets ajenos.
- Un CANCEL atrasado ya no puede cerrar la captura perteneciente a una
  generación posterior iniciada mientras esperaba la transición física.
- Una generación cancelada ya no puede publicar un estado visual terminal
  `idle` o `error` sobre una generación nueva que ya está grabando.
- La documentación refleja v1.0.0 publicada y limita la evidencia física a los
  entornos realmente validados, sin presentar Wayland ni GuionAR real como PASS.
- El launcher escapa correctamente barras invertidas literales en `Exec` sin
  introducir shell ni alterar los paths ya admitidos.

## [1.0.0] - 2026-09-23

### Agregado
- Packaging Python con entrypoints instalables `parlar` y `parlarctl`.
- Instalación de usuario autocontenida, launcher `.desktop`, servicio systemd
  opcional y desinstalación conservadora que preserva config y transcripts.
- Schema de configuración v1 con migración conservadora, claves desconocidas
  preservadas y comandos CLI para mostrar config, ruta y hotkey vigente.
- Matriz reproducible de readiness Linux 1.0 que separa evidencia automatizada
  de los retests físicos pendientes.

### Corregido
- El autostart del servicio ya no usa `default.target`, que podía iniciar antes
  de que LightDM publicara `DISPLAY` y `XAUTHORITY`. La unit queda static y una
  entrada XDG Autostart separada la inicia después del login gráfico; upgrades
  eliminan el enlace enable legado para evitar doble arranque.
- Los mensajes de startup distinguen indicador, headless, X11 y fallback IPC,
  y documentan PTT, doble toque continuo y Esc.
- CANCEL durante un STOP físico bloqueado no puede republicar una generación
  ya invalidada.
- `webrtcvad-wheels` reemplaza el paquete antiguo que importaba la API obsoleta
  `pkg_resources`, conservando la interfaz y la salida del corpus VAD probado.
- Filtro conservador de alucinaciones conocidas de Whisper: patrones completos
  como "¡Suscríbete!" o créditos de Amara se descartan cuando las métricas
  acústicas/modelo indican una unidad dudosa, sin bloquear menciones legítimas.
- La misma política se aplica a los modos frase y streaming.
- Los créditos de Amara ahora usan solo dos plantillas explícitas y estrechas;
  mencionar “subtítulos” y `amara.org` dentro de una frase legítima ya no puede
  convertirla en descartable. La documentación aclara que basta una métrica
  acústica/modelo sospechosa cuando la plantilla completa sí coincide.
- El proceso principal convierte `SIGTERM` en una solicitud de cierre normal
  que no puede perderse dentro de callbacks Tcl/Tk y ejecuta el cleanup
  ordenado de recursos, incluido el socket de control propio.
- El paste Unicode de X11 ya no habilita undo destructivo a partir del éxito de
  `xdotool`: la ruta no recibe confirmación del destino y ahora establece una
  barrera que impide borrar contenido previo con Backspace.
- La inyección de texto en X11 usa el portapapeles como transporte Unicode y
  una acción de pegado sintética, evitando las pérdidas intermitentes de
  caracteres acentuados observadas con `xdotool type`. En esta ruta el
  portapapeles es transitorio: no se garantiza preservar su contenido previo ni
  que el texto transportado permanezca disponible después del pegado, y no se
  intenta una restauración MIME parcial. El backend explícito `clipboard`
  conserva su contrato copy-only para pegado manual.

## [0.3.0] - 2026-07
### Quitado (breaking change)
- Paquete shim `flowdictate/` y el script `flowctl` (los imports
  `import flowdictate` y `python -m flowdictate` ya no funcionan)
- Migración automática de config legada desde
  `~/.config/flowdictate/config.json`
- Los 6 tests que validaban el shim

### Nota
- Los alias en inglés (comandos de socket y flags CLI) NO se tocaron:
  son UX legítima, no código legacy de FlowDictate, y siguen funcionando
  igual que siempre

## [0.2.1] - 2026-07
### Corregido
- El comando de voz "enviar" ahora es opt-in (`comando_enviar: false` por
  defecto). Antes, audio ambiente reconocido como "enviar" presionaba
  Enter en la ventana enfocada
- Filtro de alucinaciones de Whisper: se descartan segmentos con
  `no_speech_prob` alto y `avg_logprob` muy negativo simultáneamente, más
  un patrón para frases conocidas (subtítulos de YouTube, "suscribite")
- El regex de muletillas ya no borra el demostrativo español "este"
  ("quiero este informe" ya no se convierte en "quiero informe"); solo
  las formas alargadas ("esteee") se tratan como muletilla
- Frases que son únicamente una muletilla suelta ("eh.", "mmm") se
  descartan enteras en vez de dejar texto vacío
- `webrtcvad` y el pin `setuptools<81` agregados a requirements.txt: sin
  esto, instalaciones limpias caían en silencio al VAD de energía

### Agregado
- `SECURITY.md` documentando el modelo de amenaza y las mitigaciones

## [0.2.0] - 2026-07
### Agregado
- Integración opcional con GuionAR (teleprompter) vía socket Unix:
  cliente fire-and-forget con reconexión automática, deduplicación de
  VAD y parciales, y patrón null-object cuando está desactivada
- Flags `--guionar` y `--guionar-socket`; campos `guionar` y
  `guionar_socket` en la configuración
- `hipotesis_pendiente()` en el transcriptor streaming: expone el texto
  aún no confirmado por LocalAgreement como vista previa
- Sección de integración en README.md y README.es.md

### Garantías
- Sin GuionAR corriendo, el pipeline no se ve afectado (~10 µs por
  envío descartado, sin bloqueos ni excepciones)

## [0.1.0] - 2026
### Agregado
- Release inicial: dictado local a nivel sistema para Linux
- Pipeline: mic → VAD (webrtcvad + fallback de energía) → faster-whisper
  (CUDA/CPU) → procesamiento de texto español → inyección
  (xdotool/wtype/ydotool/portapapeles)
- Dos modos: frase (utterance) y streaming (LocalAgreement-2)
- Comandos de voz en español con equivalentes en inglés
- Modos de reescritura: formal, conciso, correo (reglas u Ollama local)
- Daemon controlable: atajos globales (X11), parlarctl por socket,
  indicador siempre visible

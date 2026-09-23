# Changelog: ParlAR

## [Unreleased]

### Agregado
- Packaging Python con entrypoints instalables `parlar` y `parlarctl`.
- Instalación de usuario autocontenida, launcher `.desktop`, servicio systemd
  opcional y desinstalación conservadora que preserva config y transcripts.
- Schema de configuración v1 con migración conservadora, claves desconocidas
  preservadas y comandos CLI para mostrar config, ruta y hotkey vigente.
- Matriz reproducible de readiness Linux 1.0 que separa evidencia automatizada
  de los retests físicos pendientes.

### Corregido
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

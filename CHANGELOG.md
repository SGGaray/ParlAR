# Changelog: ParlAR

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/). Versionado semántico.

## [Unreleased]

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
- Suite de tests independiente del hardware
- Nota histórica: el proyecto nació como "FlowDictate" y fue renombrado
  a ParlAR durante el desarrollo inicial

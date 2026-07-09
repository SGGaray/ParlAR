# Política de seguridad de ParlAR

## Modelo de amenaza

ParlAR escucha el micrófono de forma continua mientras está grabando e inyecta el texto transcripto en la ventana que tenga el foco del sistema. Esto significa que el atacante relevante no es "alguien en la red" (nada de ParlAR escucha en la red, todo es local), sino **audio ambiente que termina convirtiéndose en texto o en una acción sobre la ventana enfocada**.

Ejemplos concretos del riesgo:
- Una radio, un video, o una conversación de fondo capturados por el micrófono mientras ParlAR está grabando.
- Una alucinación de Whisper (el modelo generando texto que nadie dijo, un artefacto conocido del entrenamiento en subtítulos de video).

## Mitigaciones implementadas

### Comando de voz "enviar" (apagado por defecto)

El comando de voz "enviar" hace que ParlAR presione Enter en la ventana con foco. Si estuviera siempre activo, cualquier audio ambiente que Whisper transcriba como "enviar" ejecutaría esa tecla, lo que en una terminal enfocada puede significar ejecutar un comando.

Por eso `comando_enviar` está en `false` por defecto. Solo se activa explícitamente:

```json
{ "comando_enviar": true }
```

en `~/.config/parlar/config.json`. Si lo activás, tené presente que cualquier frase que Whisper interprete como "enviar" (dicha por vos, por una radio, por quien sea) va a presionar Enter en lo que tengas enfocado.

### Filtro de alucinaciones de Whisper

Whisper puede "alucinar" texto sobre silencio o ruido de fondo, típicamente frases como avisos de suscripción o créditos de subtítulos (artefacto conocido de su entrenamiento). ParlAR descarta segmentos con baja confianza (`no_speech_prob` alto y `avg_logprob` muy negativo simultáneamente) y frases que coinciden con un patrón de alucinaciones conocidas. Esto reduce el riesgo pero no lo elimina: un modelo puede alucinar frases no cubiertas por el patrón.

### Nada sale de la máquina

Audio, texto y configuración se procesan y guardan localmente. No hay telemetría, no hay llamadas de red salvo, opcionalmente, a un servidor Ollama que vos mismo corrés en `127.0.0.1` si activás el modo de reescritura por IA. `ollama_url` está fijado a loopback por defecto; si lo cambiás a una IP remota, estás asumiendo ese riesgo vos mismo.

### Transcript de sesión (`--guardar-sesion`, apagado por defecto)

Con este flag, cada texto confirmado que dictás se agrega, en texto plano y sin cifrar, a `~/.local/share/parlar/sesiones/AAAA-MM-DD_HHMM.txt` (un archivo por corrida del daemon, ruta real `$XDG_DATA_HOME/parlar/sesiones/` si esa variable está definida). Es un archivo de datos en reposo: cualquier proceso o usuario con acceso a esa carpeta puede leer todo lo que dictaste en esa sesión.

Por eso `guardar_sesion` está en `false` por defecto. Se activa explícitamente:

```json
{ "guardar_sesion": true }
```

en `~/.config/parlar/config.json`, o con `--guardar-sesion` en la línea de comandos.

**Borrado:** ParlAR nunca borra estos archivos solo. Son texto plano común, se borran a mano:

```sh
rm ~/.local/share/parlar/sesiones/*.txt
```

o el archivo puntual que corresponda. Si activás este flag en una máquina compartida o con disco sin cifrar, tené presente que el transcript queda ahí hasta que lo borres vos.

## Qué no está mitigado (limitaciones conocidas)

- Otros comandos de voz ("nuevo párrafo", "borrar última oración", "detener dictado") siguen activos por defecto. El impacto de ejecutarlos por accidente es bajo (insertan una línea, borran la última oración inyectada, o detienen la grabación), a diferencia de "enviar".
- El filtro de alucinaciones es heurístico, no elimina el riesgo, lo reduce.
- Si grabás en un ambiente con audio de terceros (oficina, videollamada), ParlAR va a transcribir e inyectar esa voz igual que la tuya. La responsabilidad de cuándo grabar es del usuario.

## Reportar un problema

Si encontrás un problema de seguridad, abrí un issue en el repositorio describiendo el escenario. Al ser un proyecto personal sin usuarios más allá de quien lo instale, no hay un proceso formal de disclosure todavía.

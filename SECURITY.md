# Política de seguridad de ParlAR

## Modelo de amenaza

ParlAR escucha el micrófono de forma continua mientras está grabando e inyecta el texto transcripto en la ventana que tenga el foco del sistema. Esto significa que el atacante relevante no es "alguien en la red" (nada de ParlAR escucha en la red, todo es local), sino **audio ambiente que termina convirtiéndose en texto o en una acción sobre la ventana enfocada**.

Ejemplos concretos del riesgo:
- Una radio, un video, o una conversación de fondo capturados por el micrófono mientras ParlAR está grabando.
- Una alucinación de Whisper (el modelo generando texto que nadie dijo, un artefacto conocido del entrenamiento en subtítulos de video).

## Mitigaciones implementadas

### Acciones que generan Enter/Return (apagadas por defecto)

Los comandos "enviar", "nueva línea" y "nuevo párrafo" generan una o más
pulsaciones de Enter. En una terminal, chat o formulario cualquiera de ellas
puede ejecutar o enviar contenido.

Por compatibilidad, `comando_enviar` es el gate único de todas esas acciones y
está en `false` por defecto. Solo se activa explícitamente:

```json
{ "comando_enviar": true }
```

en `~/.config/parlar/config.json`. Si lo activás, cualquier frase completa que
Whisper interprete como uno de esos comandos puede presionar Enter en la
ventana enfocada. Las frases completamente citadas se tratan como texto.

### Filtro de alucinaciones de Whisper

Whisper puede "alucinar" texto sobre silencio o ruido de fondo. ParlAR descarta
un segmento únicamente cuando todo el texto normalizado coincide con una de
las pocas plantillas estrechas y explícitas conocidas, y al menos una métrica
del modelo o acústica resulta sospechosa (`no_speech_prob`, `avg_logprob` o
`compression_ratio`). Una mención contextual ordinaria se conserva. Esto
reduce algunos casos conocidos, pero no promete eliminar toda alucinación.

### Procesamiento local y límite de red

La captura y transcripción de audio son locales y ParlAR no envía telemetría.
La reescritura por IA puede llamar a Ollama: `ollama_url` usa loopback por
defecto, pero es configurable. Si elegís una URL remota, el texto que se
reescribe sale de la máquina hacia ese servicio. ParlAR no promete privacidad
ni tratamiento local para un endpoint configurado por el usuario.

La instalación y el provisioning pueden descargar dependencias y modelos.
GuionAR se comunica por IPC Unix local. El portapapeles y la aplicación destino
son procesos externos: ParlAR no controla cómo persisten o sincronizan el texto
que reciben.

En X11, un exit code exitoso de `xclip` y del Ctrl+V sintético de `xdotool` no
confirma que la aplicación enfocada haya insertado el texto. Esa ruta se trata
como una frontera no verificable para undo destructivo: no se registra la
entrega y se descarta cualquier historial anterior, de modo que el comando de
borrado no pueda enviar Backspace contra contenido ajeno. `wtype` y `ydotool`
conservan la elegibilidad existente; `clipboard` es copy-only y nunca habilita
undo.

Los logs normales no incluyen audio ni texto dictado. Registran metadatos
operativos como estados, tiempos, backend, conteos y rutas. Tampoco se copia el
stderr de las herramientas de inyección porque podría repetir sus argumentos.

### Transcript de sesión (`--guardar-sesion`, apagado por defecto)

Con este flag, cada texto confirmado que dictás se agrega a un archivo
exclusivo por corrida bajo `~/.local/share/parlar/sesiones/` (ruta real
`$XDG_DATA_HOME/parlar/sesiones/` si esa variable está definida). El nombre
incluye fecha, microsegundos y un token aleatorio. Se crea con `O_EXCL` para
que instancias simultáneas nunca compartan archivo. Si ParlAR crea el
directorio usa `0700`; cada transcript usa `0600`, independientemente de la
umask. Sigue siendo texto plano sin cifrar: procesos del mismo usuario y quien
tenga acceso efectivo a su cuenta o disco pueden leerlo.

El transcript es un historial append-only de emisiones confirmadas, no un
documento final reconstruido. No registra automáticamente Return, undo, estado
del editor ni estado del portapapeles.

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

### Configuración local

El JSON se valida antes de cargar el modelo o abrir micrófono. Tipos incorrectos,
valores fuera de dominio, sample rates distintos de 16 kHz y frames VAD que no
sean 10/20/30 ms hacen fallar el inicio de forma explícita. Las claves
desconocidas se aíslan en `extras` y no pueden sobrescribir métodos. Al guardar,
el reemplazo es atómico, el archivo queda `0600` y un directorio creado por
ParlAR queda `0700`.

## Qué no está mitigado (limitaciones conocidas)

- "Borrar última oración" y "detener dictado" siguen activos por defecto. Undo
  solo usa inserciones que el backend reportó como tipeadas, pero depende del
  foco y del estado del editor externo y no es transaccional.
- El filtro de alucinaciones es heurístico, no elimina el riesgo, lo reduce.
- Si grabás en un ambiente con audio de terceros (oficina, videollamada), ParlAR va a transcribir e inyectar esa voz igual que la tuya. La responsabilidad de cuándo grabar es del usuario.

## Reportar un problema

Si encontrás un problema de seguridad, abrí un issue en el repositorio describiendo el escenario. Al ser un proyecto personal sin usuarios más allá de quien lo instale, no hay un proceso formal de disclosure todavía.

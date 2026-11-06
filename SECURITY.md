# Política de seguridad de ParlAR

## Versiones soportadas

| Versión | Soporte de seguridad |
| --- | --- |
| Release pública más reciente (`1.1.x`) | Sí |
| Releases anteriores | No, salvo indicación explícita |
| `main` y otras ramas de desarrollo | No son releases soportadas |

Las correcciones de seguridad se dirigen a la release pública más reciente.

## Modelo de amenaza

ParlAR escucha el micrófono de forma continua mientras está grabando e inyecta
el texto transcripto en la ventana que tenga el foco del sistema. Es un
producto local-first: no expone un listener de red y la captura y transcripción
son locales por defecto. No se presenta como 100 % offline porque la
reescritura con Ollama puede apuntar a un servicio remoto configurado por el
usuario y la instalación puede descargar paquetes y modelos.

El riesgo principal es el audio ambiente que termina convertido en texto o en
una acción sobre la ventana enfocada.

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

Política de destino de Ollama:

- **Loopback** (`127.0.0.0/8`, incluidas formas abreviadas como `127.1`,
  `::1` y `localhost`): HTTP o HTTPS, siempre en conexión directa. Las
  variables de proxy del entorno (`http_proxy`, `https_proxy`, `all_proxy`)
  no se aplican, aunque falte `no_proxy`.
- **Direcciones de escucha** (`0.0.0.0`, `::` y equivalentes como `0`): son
  para que un servidor escuche, no para conectarse. ParlAR no envía texto a
  ellas, con o sin la opción de abajo; para un Ollama local usá `127.0.0.1`.
- **Destino no local por HTTPS**: permitido. Respeta los proxies del entorno.
- **Destino no local por HTTP sin cifrar**: deshabilitado por defecto,
  también para redes privadas (`10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`). Se habilita sólo con
  `"ollama_allow_insecure_http": true` en `config.json`. Sin esa opción,
  ParlAR no envía el texto, avisa una vez y usa la reescritura local.

  Antes de activarla: el texto dictado viaja sin cifrar y sin autenticar el
  servidor. Cualquier equipo o proceso con acceso a esa red (Wi-Fi
  compartida, otro dispositivo de la LAN, un router comprometido) puede leer
  lo que dictás o devolver texto alterado que ParlAR escribe en la ventana
  activa. Usala sólo con un Ollama propio en una red que controlás; si
  podés, preferí `https` o un túnel (por ejemplo SSH) hacia `127.0.0.1`. La
  opción afecta únicamente a la reescritura con Ollama: no cambia ninguna
  otra conexión de ParlAR.
- **Redirecciones HTTP**: nunca se siguen. Una respuesta 3xx se trata como
  fallo y se usa la reescritura local, sin aceptar contenido de otro origen.
- **Credenciales en la URL** (`usuario:clave@host`): no se envían ni se usan
  para autenticar. La reescritura usa el modo local y los mensajes de error y
  `--mostrar-config` muestran la URL con las credenciales ocultas.

La instalación y el provisioning pueden descargar dependencias y modelos.
GuionAR se comunica por IPC Unix local y valida que el peer use el mismo UID.
Para este modelo, los procesos del mismo UID forman parte del mismo dominio de
confianza. El portapapeles y la aplicación destino son procesos externos:
ParlAR no controla cómo persisten o sincronizan el texto que reciben.

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

### Transcripciones guardadas («Conservar transcripciones», apagado por defecto)

Con esta opción activa, cada texto confirmado que dictás se agrega a un archivo
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

Por eso está apagado por defecto. Se activa explícitamente desde
Configuración → Aplicación → **Conservar transcripciones**, que guarda
`"guardar_sesion": true` en `~/.config/parlar/config.json`.

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

- En X11, pynput puede observar Esc pero no suprimir únicamente esa tecla. El
  modo `suppress=True` tomaría un grab global agresivo y afectaría otros atajos,
  por lo que no se usa. CANCEL sigue ejecutándose, pero Esc puede atravesar a la
  aplicación enfocada (por ejemplo como `^[` en una terminal).
- "Borrar última oración" y "detener dictado" siguen activos por defecto. Undo
  solo usa inserciones que el backend reportó como tipeadas, pero depende del
  foco y del estado del editor externo y no es transaccional.
- El filtro de alucinaciones es heurístico, no elimina el riesgo, lo reduce.
- Si grabás en un ambiente con audio de terceros (oficina, videollamada), ParlAR va a transcribir e inyectar esa voz igual que la tuya. La responsabilidad de cuándo grabar es del usuario.

## Reportar una vulnerabilidad

**No publiques vulnerabilidades ni detalles sensibles en un issue.** Los
issues públicos son para errores comunes, problemas de instalación y de
compatibilidad que no expongan información confidencial ni instrucciones de
explotación.

Para reportar en privado:

1. Usá [GitHub Private Vulnerability Reporting](https://github.com/SGGaray/ParlAR/security/advisories/new)
   (pestaña Security → «Report a vulnerability») cuando esté disponible. El
   reporte sólo lo ven el responsable del repositorio y las personas que se
   sumen al advisory.
2. Si no está disponible o preferís el correo, escribí a
   [security@sggaray.com](mailto:security@sggaray.com).

### Qué incluir

- Versión instalada o commit afectado.
- Sistema operativo y entorno gráfico relevante.
- Precondiciones y pasos mínimos para reproducir el problema.
- Impacto observado.
- Logs sanitizados.
- Una prueba de concepto con datos sintéticos, cuando sea posible.

### Qué no incluir

Usá sentinels y datos sintéticos si permiten reproducir el problema. No envíes
audio real, transcripts reales, texto dictado privado, API keys, tokens,
credenciales ni datos personales.

ParlAR no tiene bug bounty ni ofrece recompensas, y no hay plazos de
respuesta comprometidos. El responsable intentará acusar recibo y coordinar la
divulgación. Esta política no implica que ParlAR esté auditado o certificado.

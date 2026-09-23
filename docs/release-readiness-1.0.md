# ParlAR Linux 1.0 — release readiness

Este documento separa evidencia automatizada de validación física. “PASS” solo
significa que el chequeo indicado se ejecutó realmente; los escenarios que
requieren hardware, una sesión gráfica o percepción humana siguen pendientes.

## AUTOMATED PASS

- `./scripts/check.sh`: PASS en la rama de readiness.
- 58 checks legacy: PASS.
- 375 tests unitarios: PASS.
- Sintaxis shell, bytecode Python, `parlar --help` y `parlarctl --help`:
  PASS.
- Config schema v1, migración conservadora, validación estricta, round-trip y
  persistencia atómica: PASS.
- Entry points `parlar` y `parlarctl` presentes en el wheel: PASS.
- Instalación/desinstalación XDG simulada en directorios temporales: PASS.
- Desktop entry validado con `desktop-file-validate`: PASS cuando la
  herramienta está disponible.
- Unit generada validada con `systemd-analyze verify`: PASS cuando la
  herramienta está disponible.
- START/STOP/CANCEL, concurrencia, generaciones stale, Timer viejo, streaming,
  shutdown y SIGTERM: PASS con dobles deterministas, sin sleeps de
  sincronización.
- Socket Unix: framing, límites, ownership, stale recovery, shutdown y cleanup
  del endpoint propio: PASS.
- CPU/CUDA selection y bootstrap NVIDIA: PASS con mocks; esto no afirma que un
  driver o una GPU físicos funcionen.
- X11 Unicode, clipboard transitorio, Return safety y barrera de undo no
  verificable: PASS con subprocessos simulados; esto no afirma una inserción
  física.
- GuionAR habilitado/deshabilitado, reconexión y backpressure: PASS con IPC
  local simulado.
- `webrtcvad-wheels==2.0.14`: API importable sin `pkg_resources`; la salida
  VAD coincidió exactamente con el backend anterior sobre el corpus RAW local
  en los modos 0–3.

Para repetir la evidencia automatizada:

```bash
git status --short
./scripts/check.sh
git diff --check
```

## PENDING PHYSICAL

Ejecutar desde un checkout limpio en una cuenta de usuario de prueba. No marcar
ningún punto PASS solo porque su test simulado esté verde.

### Instalación fresca y CLI

```bash
./install.sh --preload-model --install-service
command -v parlar
command -v parlarctl
parlar --help
parlarctl --help
parlar --ruta-config
parlar --mostrar-atajo
desktop-file-validate ~/.local/share/applications/parlar.desktop
systemd-analyze verify ~/.config/systemd/user/parlar.service
```

Confirmar que los comandos funcionan después de mover o borrar el checkout de
prueba. Restaurarlo antes de ejecutar `./uninstall.sh`.

### Fedora/X11, CPU y controles

```bash
XDG_SESSION_TYPE=x11 parlar --dispositivo cpu
```

1. Mantener Ctrl derecho + Shift derecho, dictar, soltar y confirmar START/STOP
   inmediatos.
2. Hacer doble toque dentro de 300 ms, confirmar continuo; repetir para salir.
3. Durante grabación, presionar Esc y confirmar que audio/parcial pendiente no
   aparece, que texto final previo permanece y que no hay undo.
4. Registrar por separado si Esc también aparece como `^[` en la app enfocada;
   ese passthrough es una limitación conocida, no un fallo de CANCEL.
5. Dictar acentos, eñes, signos invertidos y emoji en editor, navegador y una
   terminal no ejecutora. Confirmar fidelidad y orden.
6. En una Readline PTY con texto previo, provocar un Ctrl+V sin inserción y
   luego decir “borra la última oración”; confirmar cero Backspaces.
7. Confirmar modo frase, streaming, alias largos de comandos y Return opt-in.

### Servicio systemd y socket

```bash
systemctl --user enable --now parlar
systemctl --user status parlar --no-pager
parlarctl estado
systemctl --user restart parlar
parlarctl estado
systemctl --user stop parlar
test ! -e "$XDG_RUNTIME_DIR/parlar.sock"
pgrep -af 'parlar|xclip'
```

Esperado: restart recupera control, stop deja el servicio inactivo, el socket
desaparece y no quedan procesos `xclip` huérfanos.

### Wayland

1. Iniciar `parlar` en GNOME/KDE/Hyprland Wayland.
2. Asignar un binding del compositor a
   `$HOME/.local/bin/parlarctl alternar`.
3. Confirmar que ParlAR no promete ni instala un listener global.
4. Probar `wtype`; donde no esté soportado, probar `ydotool` con su daemon.
5. Confirmar fallback copy-only y pegado manual con
   `--inyector clipboard`.
6. Repetir CANCEL, modo frase y streaming.

### NVIDIA

```bash
parlar --dispositivo cuda --sin-indicador
```

Confirmar carga real en CUDA, primera y segunda ejecución sin loop de reinicio,
dictado frase/streaming y fallback accionable cuando se retira la disponibilidad
de CUDA. Repetir el camino CPU explícito en la misma instalación.

### Launcher, login y GuionAR

1. Abrir ParlAR desde el launcher y confirmar audio, indicador y control.
2. Habilitar la unit, cerrar sesión, volver a entrar y confirmar autostart,
   display, audio y socket.
3. Ejecutar GuionAR real; confirmar VAD/parciales/finales y que ParlAR sigue
   funcionando al cerrar GuionAR.
4. Ejecutar `./uninstall.sh`; confirmar que desaparecen binarios, launcher,
   unit y entorno, pero permanecen config y transcripts elegidos.

## KNOWN LIMITATIONS

- Pynput/X11 no permite suprimir selectivamente Esc con la arquitectura actual.
  Usar `suppress=True` tomaría un grab global agresivo; por eso CANCEL funciona
  pero Esc puede atravesar a la aplicación enfocada.
- Wayland no ofrece hotkeys globales a aplicaciones arbitrarias. Los bindings
  del compositor con `parlarctl` son el camino soportado.
- La selección de micrófono depende del dispositivo de entrada predeterminado
  de PortAudio/escritorio.
- X11 `xclip → Ctrl+V` no confirma inserción física. Esa entrega es
  deliberadamente no elegible para undo destructivo.
- El clipboard X11 de transporte puede quedar vacío después del paste y no se
  restaura su contenido anterior. `--inyector clipboard` mantiene su contrato
  copy-only separado.
- El editor, terminal o navegador enfocado es un sistema externo: ninguna
  entrega o undo es una transacción end-to-end.
- Los transcripts opt-in son texto plano y no se borran automáticamente.

## BLOCKERS

No hay un blocker automatizado conocido con el gate actual. La promoción desde
1.0 RC a 1.0 final queda bloqueada hasta completar y registrar la matriz
PENDING PHYSICAL de instalación fresca, X11, Wayland, CPU, NVIDIA, launcher y
persistencia tras login. No se debe crear tag ni release antes de esa evidencia.

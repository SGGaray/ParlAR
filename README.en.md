# ParlAR

Local, system-wide voice dictation for Linux. ParlAR listens only while you
activate it, transcribes with faster-whisper, and delivers text to the focused
application. It has no telemetry and needs no cloud account or API key.

Audio capture and transcription are local. Text leaves the machine only when
you explicitly configure an external service, such as a remote `ollama_url`.
The optional GuionAR integration uses local Unix IPC.

> Versión principal en español: [README.md](README.md)

ParlAR 1.0 RC targets Linux desktops on X11 and Wayland. Any linked browser
demo is illustrative only: it does not run the native audio, hotkey, IPC, or
injection architecture described here.

## Requirements

- Desktop Linux; the provisioning scripts support Debian/Ubuntu and Fedora.
- Python 3.12 or newer with `venv`.
- PortAudio and an input device exposed through PipeWire or PulseAudio.
- X11: `xdotool` plus `xclip` for Unicode delivery.
- Wayland: `wtype` or `ydotool`; compatible clipboard tools provide a
  copy-only fallback.
- Optional: Tk for the indicator, `notify-send` for notifications, and an
  NVIDIA GPU for faster transcription.

Typical packages:

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv python3-dev portaudio19-dev \
  python3-tk libnotify-bin xdotool xclip wtype wl-clipboard

# Fedora
sudo dnf install python3 python3-devel portaudio-devel python3-tkinter \
  libnotify xdotool xclip wtype wl-clipboard
```

`ydotool` is a uinput-based Wayland alternative and requires its daemon and
the permissions documented by your distribution.

## User installation

```bash
git clone https://github.com/SGGaray/ParlAR.git
cd ParlAR
./install.sh --preload-model
```

The installer creates an isolated environment under
`$XDG_DATA_HOME/parlar/venv` (normally `~/.local/share/parlar/venv`), links
`parlar` and `parlarctl` into `~/.local/bin`, and generates a desktop
launcher. It never enables a service implicitly. Add `~/.local/bin` to your
`PATH` if your shell does not already include it.

`--preload-model` downloads Whisper `small` during installation. Without it,
the first run downloads the model. Provisioning requires network access; normal
runtime does not.

To install, but not enable, the optional user service:

```bash
./install.sh --install-service
systemctl --user enable --now parlar
```

The generated unit points to the self-contained installation, not the source
checkout. It still needs the graphical user session's display and audio
environment. If your desktop does not propagate those to systemd, start
`parlar` from a terminal in that session.

`setup.sh` remains available for development from a checkout; it is not the
primary end-user distribution path.

## First run and controls

```bash
parlar
```

On X11, the default control is **right Ctrl + right Shift**:

- Hold both keys to START immediately, without waiting for VAD.
- Speak while holding them.
- Release the combination to STOP immediately and finalize the unit.
- Two short taps within 300 ms enter continuous mode in the same session.
  Double-tap again to leave continuous mode and stop.
- **Esc** CANCELS the active session. It discards pending audio/results, does
  not undo, and does not remove already confirmed output.

When available, the indicator displays state and accepts a left-click toggle.
`--sin-indicador` / `--no-overlay` runs without that UI while keyboard and
IPC controls remain available.

## `parlarctl`

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

English aliases are also accepted: `start`, `stop`, `cancel`, `toggle`,
`status`, `mode`, `rewrite`, and `quit`. CANCEL and STOP are deliberately
different: STOP finalizes accepted input; CANCEL invalidates the generation and
discards pending work.

The daemon uses a `0600` socket at `$XDG_RUNTIME_DIR/parlar.sock`. Ordered
shutdown removes its own socket. `SIGTERM`, including systemd stop, follows
the normal cleanup path.

### Wayland

ParlAR does not pretend it can globally capture keys on Wayland. Bind an
absolute command in your compositor:

```text
/home/YOUR_USER/.local/bin/parlarctl alternar
```

GNOME and KDE expose custom shortcuts in their settings. A generic Hyprland
binding is:

```text
bind = CTRL SHIFT, D, exec, ~/.local/bin/parlarctl alternar
```

Separate `iniciar`, `detener`, and `cancelar` bindings are also supported.

## Modes

`utterance` is the default; a 600 ms pause closes a phrase. `streaming`
confirms prefixes while you speak and never retracts delivered text.

```bash
parlar --mode streaming
parlarctl mode utterance
```

Runtime mode changes take effect at a safe unit boundary.

## Configuration

Configuration lives at `$XDG_CONFIG_HOME/parlar/config.json`, normally
`~/.config/parlar/config.json`. The strict format has a `schema_version`,
is validated before resources open, and preserves unknown keys under `extras`.
An unversioned legacy file is migrated without guessing whether its hotkey was
a default or an explicit user choice.

```bash
parlar --config-path
parlar --show-config
parlar --show-hotkey
parlar --help
```

Flags can be atomically persisted with `--save-config`. Add an inspection
action to avoid starting the daemon during the change:

```bash
parlar --mode streaming --injector auto --save-config --show-config
parlar --hotkey '<ctrl_r>+<shift_r>' --save-config --show-hotkey
```

Add proper names, acronyms, or specialist vocabulary with repeatable context
terms:

```bash
parlar \
  --context-term ParlAR \
  --context-term GuionAR \
  --save-config --show-config
```

Providing context terms replaces the configured list; `--no-context` clears
it. Terms are used locally by STT and do not enable telemetry.

Audio capture currently uses PortAudio's default input device. ParlAR does not
yet have its own microphone selector; choose the default input in desktop sound
settings and restart ParlAR.

## Injection, clipboard, and undo

X11 preserves Unicode through:

```text
xclip → synthetic Ctrl+V
```

The clipboard is a transient transport on this path. ParlAR does not guarantee
preservation of its previous contents or availability of the transported text
after paste, and does not attempt restoration. The explicit
`--injector clipboard` backend is different: it is copy-only and leaves text
available for manual paste.

A successful `xdotool` exit does not prove that the focused application
inserted anything. X11 paste is therefore never added to destructive undo
history and clears the previous undo boundary. “Delete last sentence” cannot
send Backspace against unrelated content after an unverifiable paste. Existing
`wtype` and `ydotool` behavior remains unchanged; no backend can provide a
transaction with an external editor.

## Voice commands and Return safety

Commands are recognized only as exact, whole utterances. Stable aliases include
“mandar mensaje”, “enviar mensaje”, “salto de línea”, and “salto de párrafo”,
alongside existing short and English commands. There is no fuzzy or phonetic
matching.

Every Enter/Return action is blocked by default through
`"comando_enviar": false`. This includes send, new line, new paragraph,
multiline output, and rewritten output. Enabling it may execute text in a
terminal or submit a form; read [SECURITY.md](SECURITY.md) first.

## CUDA and CPU

With `device=auto`, ParlAR uses CUDA when CTranslate2 detects it and falls
back to CPU int8 otherwise. NVIDIA runtime bootstrapping is bounded and cannot
restart recursively.

```bash
parlar --device cpu
parlar --device cuda
```

Real CUDA availability depends on the host driver, libraries, and GPU and must
be checked after installation. CPU is a supported fallback, not a failure mode.

## Optional GuionAR integration

[GuionAR](https://github.com/SGGaray/GuionAR) can receive VAD, partials, and
final text over local Unix IPC:

```bash
parlar --guionar --mode streaming
```

It is best-effort and not on the critical path. ParlAR continues when GuionAR
is absent. Reconnection publishes current state, not historical final text.

## Privacy and transcripts

Normal logs contain states, timings, and error types, not dictated audio or
text. `--save-session` is opt-in and writes `0600` plaintext under
`$XDG_DATA_HOME/parlar/sesiones/`. ParlAR neither encrypts nor deletes those
files. See [SECURITY.md](SECURITY.md) for the complete threat model.

## Troubleshooting

- **`parlar` is not found:** make sure `~/.local/bin` is in `PATH`.
- **Microphone busy or missing:** choose a default input in desktop sound
  settings and restart ParlAR.
- **Wayland does not type:** try `wtype`; where unsupported, configure
  `ydotool` or use `--injector clipboard` for manual paste.
- **No global Wayland hotkey:** expected; bind `parlarctl` in the compositor.
- **The service cannot access display/audio:** inspect
  `systemctl --user status parlar` and `journalctl --user -u parlar`, or
  launch from a graphical terminal.
- **Esc appears as `^[` in the focused app:** pynput can observe Esc but
  cannot selectively suppress it without an aggressive global grab. CANCEL
  still runs; passthrough is a known limitation.

## Uninstall

From a checkout of the same version:

```bash
./uninstall.sh
```

The script disables and removes only the marked ParlAR unit and launcher,
removes its links and installed environment, and preserves configuration and
transcripts. Remove those separately only if you deliberately want to erase
them. Account for custom `XDG_CONFIG_HOME` or `XDG_DATA_HOME` values.

## Development and validation

```bash
./setup.sh
source .venv/bin/activate
./scripts/check.sh
```

The gate checks shell and bytecode, entrypoints, configuration, lifecycle,
cancellation, Unix IPC, simulated injection, systemd rendering, desktop entry,
and legacy/unit suites. It needs no microphone, display, model, clipboard,
GuionAR, or live systemd user session.

Physical and perceptual checks are kept separate in
[docs/release-readiness-1.0.md](docs/release-readiness-1.0.md).

## Known limitations

- Esc may also reach the focused X11 application.
- Microphone selection relies on the system default input.
- Wayland global hotkeys require compositor bindings.
- Delivery to an external application is not transactional; X11 paste is
  intentionally ineligible for destructive undo.
- Fresh-install compatibility still needs physical validation across desktop,
  audio hardware, and GPU combinations.

## License

MIT. See [LICENSE](LICENSE).

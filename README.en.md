# ParlAR

**Local-first, system-wide voice dictation for Linux. Spanish-first.**

Speak into any application. ParlAR captures your voice, transcribes it locally
with Whisper, cleans up the text (including proper Spanish punctuation like ¿
and ¡), and types it into whatever window has focus. It has no telemetry and
needs no API keys. Network traffic occurs only if you explicitly configure an
external service, such as a remote `ollama_url`.

> Versión en español (principal): [README.md](README.md)

## Why

Cloud dictation tools send every word you speak to someone else's servers.
ParlAR performs transcription and its default cleanup on your hardware. The
optional rewrite feature can call Ollama at loopback by default; changing
`ollama_url` to a remote endpoint sends rewrite text to that endpoint.

## Features

- **System-wide injection**: types into any focused application via xdotool (X11) or wtype/ydotool (Wayland), with clipboard fallback
- **Two transcription modes**:
  - *Utterance mode* (default): transcribes each phrase when you pause, roughly 0.2 to 0.6s inference on GPU
  - *Streaming mode*: words appear while you are still speaking, using the LocalAgreement-2 commit policy so injected text never needs retraction
- **Spanish-first text processing**: inverted punctuation handling (¿ ¡), sentence capitalization, filler-word removal
- **Voice commands**: "nuevo párrafo", "borra la última oración", "enviar", "detener dictado" (English equivalents also work)
- **Rewrite modes**: formal / concise / email, rule-based or through a local Ollama model
- **VAD-gated capture**: webrtcvad segmentation with pre-roll, plus an adaptive energy fallback
- **GPU optional**: CUDA float16 when available, CPU int8 otherwise, auto-detected
- **Controllable daemon**: global hotkey on X11, unix-socket CLI (`parlarctl`) for Wayland shortcut binding, minimal always-on-top indicator
- **GuionAR integration (optional)**: mirrors dictated text and voice activity to the [GuionAR](https://github.com/SGGaray/GuionAR) teleprompter overlay, fire-and-forget over a local unix socket

## Tech stack

| Concern | Choice |
|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 backend) |
| Audio capture | sounddevice (PortAudio) |
| Voice activity detection | webrtcvad, energy-based fallback |
| Text injection | xdotool / wtype / ydotool |
| Hotkeys and IPC | pynput (X11), unix domain socket |
| Overlay | tkinter |

Architecture details, component diagram, and latency strategy: [arquitectura.md](arquitectura.md) (Spanish).

## Requirements and installation

The checkout installer supports Debian/Ubuntu and Fedora desktop sessions on
X11 or Wayland. ParlAR requires Python 3.12 or newer; CI validates 3.12 and the
current local development environment also validates 3.14. PipeWire or
PulseAudio must expose an input device to PortAudio.

Required system components are Python, venv, and PortAudio. Desktop
integrations are optional alternatives: tkinter, notifications, X11/Wayland
typing tools, clipboard tools, and compilation headers. Required Python
dependencies are faster-whisper, sounddevice, numpy, and pynput. `webrtcvad`
is optional; a tested adaptive energy VAD is used when it is unavailable.

```bash
git clone https://github.com/SGGaray/parlar.git
cd parlar
./setup.sh                 # system packages, venv, and Python dependencies
source .venv/bin/activate
./scripts/check.sh         # complete hardware-free gate
python -m parlar           # first start; downloads Whisper small if absent
```

Setup reuses a valid `.venv` and does not delete configuration, models, or
transcripts. It refuses to remove an incomplete `.venv`. Use
`--skip-system-packages` when system dependencies are already installed. Model
provisioning is separate: first use downloads the configured model, or
`./setup.sh --skip-system-packages --preload-model` preloads `small` explicitly.

NVIDIA GPU note: if faster-whisper reports `libcublas.so.12 not found`, install the CUDA runtime libraries inside the venv and expose them:

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Then append to `.venv/bin/activate`:

```bash
SITE="$(python -c 'import site; print(site.getsitepackages()[0])')"
if [ -d "$SITE/nvidia/cublas/lib" ]; then
    export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:$LD_LIBRARY_PATH"
fi
```

## Usage

```bash
python -m parlar                        # utterance mode, Spanish
python -m parlar --modo streaming       # words appear as you speak
python -m parlar --idioma en            # dictate in English
```

1. Focus any text field.
2. Press **Ctrl+Alt+D** (X11) or your bound shortcut (Wayland). The indicator dot turns red.
3. Speak. Pause briefly. Clean text appears in the focused app.
4. Press the hotkey again to stop.

On Wayland, bind `parlarctl alternar` to a keyboard shortcut in your desktop environment (compositors block global key grabs by design). Runtime control:

```bash
./parlarctl estado
./parlarctl modo streaming
./parlarctl reescritura formal
```

English command aliases (`toggle`, `status`, `mode`, ...) are accepted for compatibility.

With rewriting disabled (the default), cleanup is conservative: decimals
(including signed values), times, URLs, email addresses, domains, versions,
identifiers, strings, and code regions are kept exactly as Whisper produced
them. Preservation wins when the input is ambiguous. Only unambiguous prose
spacing, isolated fillers outside literal regions, and Spanish opening
punctuation are normalized. A quoted phrase that resembles a voice command
remains literal text even when `.`, `?`, or `!` follows the closing quote.

Formal, concise, and email modes are explicit opt-ins. Local rules only apply
small, context-safe substitutions; the local concise fallback avoids deleting
linguistically ambiguous qualifiers such as `literally`, `basically`,
`actually`, `kind of`, or `viste`. Recognized structured
regions are protected during rewriting; if a model drops a marker, ParlAR uses
the conservative local fallback. Configuring a local Ollama model enables
generative rewriting and may change the prose wording. In both modes, known
Whisper hallucination patterns are filtered conservatively only when they fill
the whole (or nearly whole) segment and at least one acoustic/model signal also
marks that unit as doubtful. Contextual mentions and literals with strong
evidence are preserved; the filter reduces known cases but cannot eliminate all
hallucinations.

## GuionAR integration (teleprompter)

For the full system design (ParlAR + GuionAR, the socket protocol, and why they're two processes), see [GuionAR/ARCHITECTURE.md](https://github.com/SGGaray/GuionAR/blob/main/ARCHITECTURE.md).

ParlAR can mirror dictated text and voice activity to [GuionAR](https://github.com/SGGaray/GuionAR), an always-on-top teleprompter overlay that shows what you are dictating near the camera.

```bash
# terminal 1: the teleprompter
cd GuionAR && python guionar.py --socket

# terminal 2: ParlAR with the integration enabled
python -m parlar --guionar --modo streaming
```

| Flag | Description |
|---|---|
| `--guionar` (alias `--guionar-enabled`) | Send text and VAD state to the teleprompter |
| `--guionar-socket PATH` | Socket path override (default `$XDG_RUNTIME_DIR/guionar.sock`) |

It can also be enabled permanently with `"guionar": true` in `~/.config/parlar/config.json`.

The integration is best-effort. Reconnection sends the current VAD and partial
snapshot, but does not replay historical final text and has no application ACK.
GuionAR limits each final text message to 2,000 characters. Its receiving
socket permissions are owned by GuionAR, not ParlAR.

Insertion, clipboard copy, GuionAR mirroring, and transcript persistence are
independent results. Streaming clipboard fallback accumulates a recoverable
utterance, but does not insert it or add it to undo history. Undo remains
dependent on the focused external editor. By default `comando_enviar=false`
blocks every physical Enter/Return action, whether it comes from a command,
multiline text, streaming, or rewrite output. Multiline content is then copied
intact and reported as copied rather than inserted, without entering undo
history.

Control uses a `0600` Unix socket, one newline-terminated UTF-8 operation per
connection, a 4 KiB limit, verified stale-socket recovery, and UID-qualified
fallback paths under `/tmp`.

Configuration is validated before the model or microphone is initialized.
JSON types are strict, capture is fixed at 16 kHz, and WebRTC VAD frame size
must be 10, 20, or 30 ms. Only `mode` and `rewrite_mode` are runtime-dynamic;
other changes require a restart. Saved configuration is atomically replaced
and uses mode `0600` in a newly created `0700` directory.

Session transcripts are disabled by default. `--guardar-sesion` creates one
exclusive `0600` plaintext file per daemon run in the session directory,
which is created as `0700`; concurrent runs never append to the same file.
ParlAR does not delete or encrypt these files. A transcript is an append-only
history of confirmed emissions, not a reconstruction of Return, undo, editor,
or clipboard state. Normal logs contain operational metadata, not dictated
text.

Installation and provisioning may download dependencies and models. At
runtime, GuionAR uses local Unix IPC. The clipboard and destination application
are outside the ParlAR process and its storage guarantees. On X11, the
`xdotool` backend transports text through `xclip` and triggers a synthetic paste
to preserve Unicode. The clipboard is a transient transport detail on this
path: neither preservation of its previous contents nor availability of the
transported text after the paste is guaranteed. ParlAR does not attempt to
restore it because it may contain multiple MIME formats. This does not change
the explicit `--inyector clipboard` backend: a successful copy through that
backend does leave the text available for manual pasting.

## Running the tests

```bash
./scripts/check.sh
```

This is the same gate CI runs: shell syntax, Python compilation, CLI smokes,
58 legacy checks, every unittest suite, Unix IPC, and `git diff --check`. It
does not require audio hardware, a display, a downloaded model, Ollama,
GuionAR, clipboard, or a real systemd session.

The suites cover session lifecycle, deterministic concurrency, mode
boundaries, shutdown ordering, worker health, microphone ownership, bounded
capture backpressure, gap recovery, structured-token fidelity, safe rewrite
rules, and confidence-gated hallucination filtering. No audio hardware required.

Capture keeps a bounded queue of roughly ten seconds. If the worker falls
behind, the oldest frames are dropped to preserve recent audio. ParlAR cannot
reconstruct discarded audio: per-session sequence numbers expose the gap and
the worker separates audio before and after it instead of presenting a false
continuous utterance. A PortAudio `input_overflow` creates the same boundary,
invalidates any preceding partial frame, and is counted separately as
`device_overflows` without inventing a dropped-frame count. `parlarctl estado`
reports capture health, queue drops, device overflows, discontinuities, queue
depth, and estimated backlog. Once the boundary is handled, health becomes
`recuperado-con-perdida` while the loss remains visible for that session.

The callback may accept audio while backend startup is still completing, but
the worker waits for startup to resolve before processing it. A failed open
invalidates that generation and its frames and leaves microphone startup
retryable. `parlarctl estado` also keeps STT and VAD health separate
(`healthy`, `degraded`, or `recovered`), with historical counters and the last
exception type but no audio or transcript content. VAD deliberately fails
open to avoid losing speech; while degraded it can increase STT work and
produce false utterances.

Streaming retains its acoustic minimum for an initially short utterance. When
a valid trim leaves a tail below that minimum, stop performs one final decode
instead of dropping the tail automatically; append-only agreement still gates
the result and never retracts emitted text.

## Optional user service

```bash
./setup.sh --skip-system-packages --install-service
systemctl --user enable --now parlar
```

Setup renders the unit with the checkout's real absolute path and venv Python,
keeps `UMask=0077`, and will not replace an unrelated unit. Repeating it for
the same checkout is a no-op. A user service still depends on desktop-specific
audio, display, X11/Wayland, and clipboard access; launch ParlAR from a terminal
inside that desktop session if those variables are unavailable to systemd.
Shutdown invalidates the generation and joins the worker before closing output
sinks. Every resource receives a cleanup attempt even when another closer
fails; the app reaches `closed` and retains closure error types for diagnosis.

For later manual hardware validation, check microphone open, utterance mode,
streaming, stop, restart, optional GuionAR, clipboard fallback, and shutdown.

## Project status

**v0.2.0, experimental.** It has been exercised on Fedora/X11 hardware, but
still requires broader fresh-install and desktop validation:

- No graphical UI yet beyond the minimal overlay indicator; configuration is JSON plus CLI flags
- The output system is not yet fully decoupled (injection is wired directly into the pipeline; the GuionAR client is the first decoupled output)
- API and module layout may change between 0.x releases

Roadmap: decoupled output backends, configuration UI, packaging (RPM/deb/Flatpak).

## License

MIT. See [LICENSE](LICENSE).

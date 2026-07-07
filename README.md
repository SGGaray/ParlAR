# ParlAR

**Local-first, system-wide voice dictation for Linux. Spanish-first. 100% offline.**

Speak into any application. ParlAR captures your voice, transcribes it locally with Whisper, cleans up the text (including proper Spanish punctuation like ¿ and ¡), and types it into whatever window has focus. No cloud, no API keys, no telemetry: nothing ever leaves your machine.

> Documentación en español: [README.es.md](README.es.md)

## Why

Cloud dictation tools send every word you speak to someone else's servers. ParlAR is built on a single constraint: **all processing happens on your hardware**. The optional rewrite feature can use a local Ollama model, and even that call never leaves 127.0.0.1.

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

## Installation

Tested on Fedora; Ubuntu/Debian supported by the installer.

```bash
git clone git@github.com:SGGaray/parlar.git
cd parlar
./setup.sh                 # system deps, venv, Python deps, model download
source .venv/bin/activate
```

NVIDIA GPU note: if faster-whisper reports `libcublas.so.12 not found`, install the CUDA runtime libraries inside the venv and expose them:

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Then append to `.venv/bin/activate` (adjust the Python version to yours):

```bash
SITE="$VIRTUAL_ENV/lib64/python3.14/site-packages"
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

## GuionAR integration (teleprompter)

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

The integration is fire-and-forget: if GuionAR is not running, ParlAR works exactly as before (sends are dropped in ~10 µs, no blocking, no errors in the pipeline). If GuionAR dies mid-session, dictation continues and the connection resumes on its own. In streaming mode, committed text appears bright and the still-unconfirmed hypothesis shows as a dim preview; the scroll advances only while the VAD detects speech. Everything stays on a local unix socket with `0600` permissions: the privacy model does not change.

## Running the tests

```bash
python tests/run_tests.py
```

The suite covers the VAD segmenter, Spanish text processing, the streaming commit policy (against a scripted fake engine), injection command construction, and the backward-compatibility shim. No audio hardware required.

## Project status

**v0.2.0, functional and validated on real hardware** (Fedora, RTX 2050, X11), but early:

- No graphical UI yet beyond the minimal overlay indicator; configuration is JSON plus CLI flags
- The output system is not yet fully decoupled (injection is wired directly into the pipeline; the GuionAR client is the first decoupled output)
- API and module layout may change between 0.x releases

Roadmap: decoupled output backends, configuration UI, packaging (RPM/deb/Flatpak).

## License

MIT. See [LICENSE](LICENSE).

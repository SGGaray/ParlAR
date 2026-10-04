<p align="center">
  <img src="parlar/assets/parlar.svg" width="96" height="96" alt="ParlAR icon">
</p>

# ParlAR

**Local voice dictation for Linux.** You speak, and ParlAR types into the
application you have open.

No account. No telemetry. Speech recognition runs on your computer.

> Español: [README.md](README.md)

## What it does

- **Dictation into any application.** Hold the shortcut, speak and release:
  the text appears at the cursor (editor, browser, chat, terminal).
- **Two ways to dictate.** Hold the shortcut while you speak, or double-tap
  it for continuous dictation until you double-tap again.
- **Cancel without typing.** `Esc` discards anything not typed yet.
- **Phrase by phrase or incremental.** Phrase mode types when you pause;
  Incremental types while you speak.
- **Voice commands** to delete the last sentence, stop dictation or insert
  line breaks (see [Voice commands](#voice-commands)).
- **Words and names.** Tell ParlAR which terms, acronyms or names it should
  recognize better.
- **Settings** in a window of its own, with a system tray icon and a small
  visual indicator while you dictate.
- **Works with [GuionAR](https://github.com/SGGaray/GuionAR)**, the
  teleprompter: when it is open, it follows your voice with no setup.

## Compatibility

| | Status |
|---|---|
| Linux on X11 | Supported. Validated on Fedora with X11. |
| Linux on Wayland | Works with limitations: your desktop has to provide the global shortcut (see [Wayland](#wayland)). Not validated by hand. |
| Processor (CPU) | Supported on any machine. The default when no compatible GPU is present. |
| NVIDIA GPU | Optional. When available, ParlAR uses it to transcribe faster. |
| Debian/Ubuntu and Fedora | Distributions with installation instructions. |
| Windows, macOS | Not supported. |

**Requirements:** Linux x86_64 with glibc 2.28 or newer, Python 3.12, 3.13
or 3.14 with the `venv` module (most current distributions ship it) and a
microphone that works on your desktop. No compiler needed. Disk space: about
450 MB per installed version (an update keeps the previous one until the
next update), plus the speech model (about 500 MB for the `small` model)
and, with an NVIDIA GPU, about 2.5 GB for its libraries. The installer
requires at least 2 GB free to install (6 GB when it installs NVIDIA GPU
support; `--cpu-only` needs 2 GB) and does not keep downloads in pip's cache.

## Install

ParlAR is installed from the published release: one archive with everything
needed for your user account. The installer sets ParlAR up in its own
environment and adds it to the applications menu. `sudo` is only needed for
the system packages.

**1. System packages**

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv libportaudio2 python3-tk python3-gi \
  gir1.2-gtk-3.0 libnotify-bin xdotool xclip wtype wl-clipboard

# Fedora
sudo dnf install python3 portaudio python3-tkinter python3-gobject gtk3 \
  libnotify xdotool xclip wtype wl-clipboard
```

**2. Download** `parlar-<version>-linux-x86_64.tar.gz` and `SHA256SUMS` from
the [releases](https://github.com/SGGaray/ParlAR/releases/latest) page into
the same folder.

**3. Verify** that the download is complete and unmodified:

```bash
sha256sum -c --ignore-missing SHA256SUMS
```

It must print `OK` for the downloaded archive.

**4. Extract and install**

```bash
tar -xzf parlar-*-linux-x86_64.tar.gz
cd parlar-*/
./install.sh --preload-model
```

`--preload-model` downloads the speech model during installation (several
hundred MB). Without it, the model is downloaded the first time you open
ParlAR. If the installer finds a compatible NVIDIA GPU, it also installs what
is needed to use it; `--cpu-only` skips that.

To let ParlAR **start automatically when you log in**, install with:

```bash
./install.sh --preload-model --install-service
```

Then turn it on or off in Settings → Application.

**5. Open** ParlAR from the applications menu.

You no longer need the extracted folder after installing: you can delete
it. ParlAR lives in `~/.local/share/parlar` and the `parlar`, `parlarctl` and
`parlar-uninstall` commands in `~/.local/bin`.

## Open ParlAR

Look for **ParlAR** in your applications menu. Opening it:

- opens the **Settings** window;
- leaves ParlAR running in the background, ready to dictate, even after you
  close that window.

If ParlAR is already running, opening it from the menu only brings up
Settings. A click on the tray icon opens Settings too.

The tray icon menu shows the current status and lets you **Pause**, open
**Settings…**, **Restart** and **Quit**.

## Dictate

The default shortcut is **right Ctrl + right Super/Windows key**. You can
change it in Settings → Dictation. If you are coming from an earlier
version, ParlAR keeps the shortcut you already had; Settings shows the
current one.

| Action | What happens |
|---|---|
| Hold the shortcut | Starts listening immediately. |
| Release the shortcut | Stops and types what you said. |
| Quick double tap | Continuous dictation: listens until the next double tap. |
| `Esc` | Cancels anything pending. Text already typed is not removed. |

While you dictate, a small indicator appears without taking focus from the
application you are typing into.

### Voice commands

Commands are recognized only when the whole phrase is the command. ParlAR
listens for Spanish commands first; the English ones are:

| Say | Does |
|---|---|
| "delete last sentence" | Deletes the last sentence ParlAR typed, when it is safe to do so. |
| "stop dictation", "stop listening" | Ends dictation. |
| "new line" | Line break. * |
| "new paragraph" | New paragraph. * |
| "send", "send message" | Presses Enter. * |

\* Commands that press Enter are **off by default**: in a terminal or a
chat, a misrecognized phrase could run or send something. To enable them,
add `"comando_enviar": true` to `~/.config/parlar/config.json` and restart
ParlAR. Read [SECURITY.md](SECURITY.md) first.

### Wayland

Wayland does not let applications capture a global shortcut. Create a
custom shortcut in your desktop's keyboard settings (GNOME, KDE and others
support this) that runs:

```text
~/.local/bin/parlarctl alternar
```

Each press starts or ends dictation. If your desktop needs a full path, use
`/home/YOUR_USER/.local/bin/parlarctl alternar`.

## Settings

Changes are saved from the window. Some apply right away and others need a
ParlAR restart; the window tells you and offers a **Restart ParlAR** button.
The interface is in Spanish; the section names below are translated.

**Dictation** (Dictado)
- **Dictation shortcut**: change the combination or restore the default.
- **Strategy**: Phrase by phrase or Incremental.
- **Rewriting**: None, Formal, Concise or Email.
- **Language**: a code such as `es` or `en`. Empty detects the language.
- **Words and names**: terms ParlAR should recognize better, one per line.

**Microphone** (Micrófono)
- Choose the microphone or use the system default.
- **Test microphone** to check the level before dictating. That audio is
  not saved.

**GuionAR**
- Connection status with GuionAR.
- **Integrate with GuionAR** and **Use GuionAR as exclusive output** (see
  [Using it with GuionAR](#using-it-with-guionar)).

**Application** (Aplicación)
- **Indicator**: show or hide it, and choose where it appears.
- **Start ParlAR when you log in**: available if you installed with
  `--install-service`.
- **Keep transcripts**: saves a local copy of every dictation. Off by
  default.

**Advanced** (Avanzado)
- **Model**: speech model size. Larger is usually more accurate and slower.
- **Accelerator**: Automatic, CPU or NVIDIA CUDA. Automatic uses the GPU
  when available and the processor otherwise.
- **Compute precision**: numeric format of the model. Automatic works well
  in most cases.
- **Typing method**: how text is delivered (automatic, X11, Wayland,
  virtual keyboard or clipboard only).

Settings are stored in `~/.config/parlar/config.json`.

## Using it with GuionAR

[GuionAR](https://github.com/SGGaray/GuionAR) is a teleprompter that can
follow your voice while you read a script, or show your dictation live.

- **Nothing to set up.** When GuionAR is open, ParlAR connects on its own.
  It does not matter which one you open first.
- **Follows your voice live**: GuionAR receives what you say while you are
  speaking, not only when each phrase ends.
- **If GuionAR is not open**, ParlAR works as usual. If you close and reopen
  it, ParlAR reconnects without restarting.
- **Exclusive output** (on by default): while GuionAR is connected, ParlAR
  sends dictation only to GuionAR and does not type into the application you
  have open. Without GuionAR, it types as usual. Changing it applies from the
  next phrase.
- **To turn the integration off**, disable **Integrate with GuionAR** in
  Settings → GuionAR.

Anything dictated while GuionAR is closed is not sent to it later.

## Privacy

- **Speech recognition is local.** Audio is processed on your computer and
  is not sent to any service.
- **Audio is not saved.**
- **No telemetry, no account.**
- **Optional transcripts.** Only if you enable **Keep transcripts**: they are
  saved as unencrypted text in `~/.local/share/parlar/sesiones/`, readable
  only by your user. ParlAR never deletes them; you do.
- **Logs.** Diagnostic messages contain states and timings, not audio or
  dictated text.
- **Settings** in `~/.config/parlar/config.json`.
- **Network.** Installation and the first model download use the internet.
  After that, dictation does not need it.
- **Rewriting with Ollama (optional, off).** If you configure an Ollama model
  to rewrite text, ParlAR sends that text to it. By default it points to
  Ollama on your own computer; **if you configure a remote address, the text
  leaves your machine** for that service.
- **The clipboard and the target application** are separate programs:
  ParlAR does not control what they do with the text they receive.

Security details are in [SECURITY.md](SECURITY.md).

## Update

Download and verify the new release as when installing, extract it and run
its installer with the same options you used before (for example
`--install-service`):

```bash
tar -xzf parlar-*-linux-x86_64.tar.gz
cd parlar-*/
./install.sh
```

The new version is installed next to the current one and only becomes active
once it is complete and validated. If anything fails, the version you had
keeps working unchanged. Your settings, shortcut and transcripts are kept.

Then **Quit** from the tray menu and open ParlAR again from the applications
menu. If you run it as a service: `systemctl --user restart parlar.service`.

**If you had ParlAR 1.0.x** installed from a copy of the repository, the new
installer recognizes it and replaces it without touching your settings or
transcripts. You no longer need the repository copy.

`parlar --version` shows the installed version.

## Uninstall

Quit ParlAR (**Quit** in the tray menu) and run:

```bash
parlar-uninstall
```

It does not need the release folder. If `~/.local/bin` is not in your
`PATH`: `~/.local/share/parlar/uninstall.sh`.

**Removes:** the installed application and its versions, the `parlar`,
`parlarctl` and `parlar-uninstall` commands, the menu entry, the icon and, if
present, automatic start at login.

**Keeps:** your settings (`~/.config/parlar/`) and saved transcripts
(`~/.local/share/parlar/sesiones/`).

To delete your settings and transcripts as well:

```bash
parlar-uninstall --purge-data
```

**Speech model:** it stays in the Hugging Face cache
(`~/.cache/huggingface/`), which other applications may share, so no option
deletes it. To remove only the models ParlAR uses:

```bash
rm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-*
```

## Troubleshooting

- **It is not in the menu, or the `parlar` command does not exist.** Log out
  and back in. If you use the terminal, add `~/.local/bin` to your `PATH`.
- **It does not listen, or listens to the wrong microphone.** In Settings →
  Microphone, choose the device and use **Test microphone**.
- **It listens but types nothing.** Check that exclusive output is not on
  with GuionAR open. Otherwise, try another **Typing method** in Settings →
  Advanced; **Clipboard** leaves the text ready to paste by hand.
- **On Wayland the shortcut does nothing.** Expected: assign the shortcut in
  your desktop (see [Wayland](#wayland)).
- **`Esc` shows up as `^[` in a terminal.** ParlAR still cancels, but cannot
  stop the key from also reaching the application. Known limitation.
- **No tray icon.** Some desktops (GNOME, for example) need an indicator
  extension. ParlAR works anyway; open Settings from the applications menu.
- **It does not start at login.** Enable **Start ParlAR when you log in** in
  Settings → Application. If it is unavailable, reinstall with
  `--install-service`.
- **It is slow.** In Settings → Advanced try a smaller model, or check that
  the Accelerator uses the GPU if you have an NVIDIA card.
- **The installer says your Python is not compatible.** Each release ships
  what is needed for the Python versions named in the message. Install one
  of them (for example `python3.13` from your distribution) and run
  `./install.sh` again; the installer finds it on its own.
- **PortAudio warning while installing.** Install `libportaudio2`
  (Debian/Ubuntu) or `portaudio` (Fedora); without it ParlAR cannot use the
  microphone.

## Known limitations

- Linux only. Validated by hand on Fedora with X11; Wayland has not been
  validated by hand.
- On Wayland there is no built-in global shortcut: a desktop shortcut is
  used instead.
- `Esc` cancels dictation but may also reach the focused application.
- ParlAR cannot confirm that the target application inserted the text. For
  safety, "delete last sentence" does nothing when it cannot be sure what
  was typed.
- Like any speech recognition, it can make mistakes or, with background
  noise, type things nobody said. ParlAR filters some known cases, not all.
- Other voices in the room are transcribed too.
- Saved transcripts are not encrypted.

## Project status

ParlAR is an application maintained as a product. This repository contains
its source code and is organized mainly for installing it, using it and
inspecting the code, not as a community development project. That is why
there is no contribution guide or public roadmap.

## Source code and license

The source code lives in this repository and is released under the
[MIT](LICENSE) license. To use ParlAR, install the release.

## Report a problem

- **Bugs, installation or compatibility problems:** open an
  [issue](https://github.com/SGGaray/ParlAR/issues/new/choose) and pick the
  matching type.
- **Security vulnerabilities:** do not post them in an issue. Follow
  [SECURITY.md](SECURITY.md).

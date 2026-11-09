<p align="center"><img src="assets/mixcaps-logo-512.png" width="96" alt="MixCaps logo"></p>

# MixCaps

Captions for **DaVinci Resolve** in **English, Telugish (Telugu + English) and Hinglish (Hindi + English)**.
They're written in Roman letters and placed on your timeline as styled, animated Text+.
Everything runs locally on your PC. No uploads and no subscriptions.

## Features

- **Code-mixed speech:**
  - Telugish uses a Telugu fine-tuned Whisper model, a casual romanizer and English-word recovery.
  - Hinglish uses Whisper turbo.
  - The language can be auto-detected.
- **Review before adding:** edit, merge or delete captions, jump to any line in Resolve, and use find & replace that remembers your fixes.
- **Real Text+ captions** on their own track. You can restyle them later without generating them again.
- **Presets:** GameGlitch, Clean, Bold Outline, Yellow Punch, Boxed, Neon and Typewriter, plus **Stacked**. Stacked builds blocks of mixed-style lines that stack up, with an automatic emphasis line you can override.
- **Preset editor:**
  - any installed font
  - colour, outline, shadow and background box
  - size and spacing
  - animations: pop, fade, slide up, blur in, typewriter
- **Sharing:** save your own presets and export or import them as `.mixcaps.json` files.
- **Speed tiers** (Light, Balanced, Best) and a "go easy on my PC" mode.
- **Works with free Resolve.** The app opens as a small window next to Resolve.

## Requirements

- Windows 10 or 11
- DaVinci Resolve 18 to 21.0 (free or Studio). Resolve 21.1 and later aren't supported yet.
- An internet connection for the first install, which downloads about 2 to 8 GB of models.
- An NVIDIA GPU is recommended. On CPU it still works, just slower.

## Install

1. Download `MixCaps-x.y.z.zip` from [Releases](../../releases) and unzip it.
2. Run `install.bat`. It sets up its own Python and models in `%LOCALAPPDATA%\MixCaps`, so you don't need to install Python yourself.
3. Restart Resolve, open a timeline, then go to **Workspace > Scripts > MixCaps**.

To remove it, run `uninstall.bat`. See [README.txt](README.txt) for step-by-step usage.

## How it works

- `resolve/MixCaps.lua` is a small bridge that runs inside Resolve. It reads the timeline audio clips and places and styles the Text+ captions.
- `engine/mixcaps_app.py` is the local web UI, served on 127.0.0.1. `engine/mixcaps_engine.py` does the transcription with faster-whisper.
- The two sides talk through files in `%LOCALAPPDATA%\MixCaps\ipc`, because free Resolve doesn't allow script windows.

## Credits

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) and OpenAI Whisper models
- [vasista22](https://huggingface.co/vasista22) Telugu Whisper models (Apache 2.0)
- Caption template scaffolding adapted from [AutoSubs](https://github.com/tmoroney/auto-subs) (MIT). See [NOTICE.txt](NOTICE.txt).
- [Poppins](https://github.com/itfoundry/Poppins) font (SIL OFL 1.1)

## License

[MIT](LICENSE)

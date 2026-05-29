# Installation

The parser has two install steps: **Ollama** (the local model runtime) and the parser itself. Both are one-time. After that, the parser auto-manages the daemon and pulls models on first run.

> **Install scripts** (`scripts/install.ps1` / `install.sh`) that wrap all of this in one command are tracked under [issue #11](https://github.com/swimblocks/deck-eval-parser/issues/11). Until they land, follow the manual steps below.

## What you'll need

- **Python 3.12** ([download](https://www.python.org/downloads/))
- **Git** (to clone)
- **Ollama** — installed in step 1 below
- A reasonable amount of disk space — the default Qwen2.5-VL 7B model is ~6 GB; the text edit model is another ~4.7 GB. Bigger GPU tiers want more (see [`models.md`](models.md) once #7 lands).
- **A GPU is strongly recommended.** Vision-LLM inference on CPU works but is slow (tens of seconds per page). Any modern NVIDIA card with 8+ GB VRAM is comfortable for the 7 B default.

## 1. Install Ollama

Ollama is the local runtime that hosts the vision and text models. It runs as a background daemon on `localhost:11434`. The parser detects whether the daemon is running and starts/stops it as needed — you never have to think about it after installation.

### Windows

The recommended path is [winget](https://learn.microsoft.com/en-us/windows/package-manager/winget/) (built into Windows 10/11):

```powershell
winget install Ollama.Ollama
```

Alternatively, download the installer from [ollama.com/download](https://ollama.com/download/windows) and run it.

Verify:
```powershell
ollama --version
```

### macOS

Homebrew:

```bash
brew install ollama
```

Or download the `.dmg` installer from [ollama.com/download](https://ollama.com/download/mac).

Verify:
```bash
ollama --version
```

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This adds an `ollama.service` systemd unit on most distros. The parser will spawn `ollama serve` directly if the service isn't running — either approach works.

Verify:
```bash
ollama --version
```

## 2. Clone the repo + create a virtual environment

```bash
git clone https://github.com/swimblocks/deck-eval-parser.git
cd canswim-deck-eval-parser
python -m venv .venv
```

Activate the venv:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (Git Bash / bash on Windows)
source .venv/Scripts/activate

# macOS / Linux
source .venv/bin/activate
```

## 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

## 4. (Optional) Pre-pull the models

The parser will auto-pull models on first run. If you'd rather warm the cache up front (it's the only step that needs an internet connection — every parse after that is fully offline):

```bash
ollama pull qwen2.5vl:7b   # vision extraction — ~6 GB
ollama pull qwen2.5:7b     # text edits in --interactive mode — ~4.7 GB
```

You can skip `qwen2.5:7b` if you never plan to use `--interactive`.

## 5. Verify the install

The fastest end-to-end check is a parse against the included synthetic fixture:

```bash
python main.py tests/fixtures/form_field/session_1_evals.pdf
```

You should see a summary on stdout listing 11 evaluations across 2 pages, and `output/session_1_evals.{json,csv,xlsx}` written.

To exercise just the Ollama runtime (daemon start/stop, binary detection — no model use):

```bash
python scripts/smoke_ollama_runtime.py
```

This should end with `PASS: lifecycle behaved correctly.` Exit code 0 means good.

## Troubleshooting install issues

**`ollama: command not found` even though I just installed it.**
Reopen your terminal. The installers add Ollama to PATH but existing shells don't see it until they restart. On Windows you may also need to log out and back in for system-wide PATH changes to propagate.

**`ollama serve` says "Error: listen tcp 127.0.0.1:11434: bind: address already in use".**
Ollama is already running (the desktop app, the systemd service, or another `ollama serve` you forgot about). The parser handles this transparently — it'll detect the existing daemon and use it. The error only happens if you try to start it manually a second time.

**The parser exits with `Ollama is not installed (or not on PATH)` even though `ollama --version` works in my terminal.**
The parser uses `shutil.which("ollama")` to locate the binary. This honours `PATH` from the environment it was launched in. If you installed Ollama in one shell and `python main.py` in another, restart the parser's shell.

**`pip install -r requirements.txt` fails on `pymupdf`.**
PyMuPDF wheels are published for all major platforms — failures usually mean an outdated `pip`. Run `python -m pip install --upgrade pip` first.

**Real scanned PDFs return `extraction failure — vision extraction not yet implemented`.**
Expected today. The vision path is tracked across issues [#6](https://github.com/swimblocks/deck-eval-parser/issues/6)–[#10](https://github.com/swimblocks/deck-eval-parser/issues/10). Fillable PDFs (e.g. `eval-gen` output, online form exports) parse end-to-end now.

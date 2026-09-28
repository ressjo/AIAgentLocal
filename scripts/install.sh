#!/usr/bin/env bash
# JARVIS – installer for Arch Linux / Manjaro / EndeavourOS and Debian / Ubuntu / Linux Mint
#
#   ./scripts/install.sh                   # everything with sensible defaults
#   ./scripts/install.sh --lang en         # English assistant, voice and UI (default: de)
#   ./scripts/install.sh --model qwen3:8b  # choose the Ollama model yourself
#   ./scripts/install.sh --gpu vulkan      # force the Ollama backend: auto | cuda | rocm | vulkan | cpu (Arch)
#   ./scripts/install.sh --no-autostart
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/jarvis"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
MODEL=""
GPU="auto"
LANG_CHOICE="de"
AUTOSTART=1
REPO="${JARVIS_REPO:-https://github.com/ressjo/AIAgentLocal.git}"
BRANCH="${JARVIS_BRANCH:-main}"
PIPER="https://huggingface.co/rhasspy/piper-voices/resolve/main"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --lang) LANG_CHOICE="$2"; shift 2 ;;
    --gpu) GPU="$2"; shift 2 ;;
    --vulkan) GPU="vulkan"; shift ;;   # kept for compatibility
    --cpu) GPU="cpu"; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done
[[ "$LANG_CHOICE" == "en" ]] || LANG_CHOICE="de"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if command -v pacman >/dev/null; then DISTRO="arch"
elif command -v apt-get >/dev/null; then DISTRO="debian"
else echo "Unsupported distribution – JARVIS supports Arch-based (pacman) and Debian/Ubuntu-based (apt) systems."; exit 1
fi

# ---------------------------------------------------------------- Fixed location via git
# Started from a ZIP download? A git checkout makes updates a one-liner ("jarvis update").
if [[ ! -d "$ROOT/.git" && -t 0 ]]; then
  echo "This folder is a ZIP download. Updates are easier with a git checkout in ~/jarvis."
  read -r -p "Install to ~/jarvis instead (recommended)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[nN] ]]; then
    if ! command -v git >/dev/null; then
      if [[ $DISTRO == arch ]]; then sudo pacman -S --needed --noconfirm git; else sudo apt-get install -y git; fi
    fi
    JARVIS_REPO="$REPO" JARVIS_BRANCH="$BRANCH" exec bash "$ROOT/scripts/bootstrap.sh" "$@"
  fi
fi

# ---------------------------------------------------------------- GPU detection
if [[ "$GPU" == "auto" ]]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then GPU="cuda"
  elif ls /sys/class/drm/card*/device/mem_info_vram_total >/dev/null 2>&1; then GPU="rocm"
  else GPU="cpu"; fi
fi
echo "GPU backend: $GPU"

# ---------------------------------------------------------------- System packages
say "Installing system packages"
if [[ $DISTRO == arch ]]; then
  case "$GPU" in
    cuda) OLLAMA_PKG="ollama-cuda" ;;
    rocm) OLLAMA_PKG="ollama-rocm" ;;
    vulkan) OLLAMA_PKG="ollama-vulkan" ;;
    *) OLLAMA_PKG="ollama" ;;
  esac
  if ! pacman -Si "$OLLAMA_PKG" >/dev/null 2>&1; then
    echo "Package $OLLAMA_PKG not found in the repos – using 'ollama'."
    OLLAMA_PKG="ollama"
  fi
  sudo pacman -S --needed --noconfirm python uv git fd ripgrep plocate xdg-utils polkit pacman-contrib libnotify \
    gtk3 curl iproute2 iputils "$OLLAMA_PKG"
  sudo systemctl enable --now ollama.service
  sudo systemctl enable --now plocate-updatedb.timer || true
else
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv git curl ca-certificates fd-find ripgrep plocate xdg-utils \
    libnotify-bin libgtk-3-bin iproute2 iputils-ping zstd
  # uv (Python package manager) – official installer, installs to ~/.local/bin
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  # Ollama – official installer (sets up CUDA/ROCm support and the systemd service)
  if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  sudo systemctl enable --now ollama.service || true
fi
sudo updatedb || true

# ---------------------------------------------------------------- Python environment
say "Setting up the Python environment (uv)"
cd "$ROOT"
uv sync --extra voice

# ---------------------------------------------------------------- Model choice by VRAM
if [[ -z "$MODEL" ]]; then
  GB=0
  if [[ "$GPU" == "cuda" ]]; then
    MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1)
    GB=$(( ${MIB:-0} / 1024 ))
  else
    for f in /sys/class/drm/card*/device/mem_info_vram_total; do
      [[ -r "$f" ]] || continue
      v=$(( $(cat "$f") / 1024 / 1024 / 1024 )); (( v > GB )) && GB=$v
    done
  fi
  if (( GB >= 15 )); then MODEL="qwen3:14b"
  elif (( GB >= 7 )); then MODEL="qwen3:8b"
  else MODEL="qwen3:4b"; fi
  echo "Detected video memory: ${GB} GB → model $MODEL"
fi

say "Pulling models into Ollama (takes a while the first time)"
for _ in {1..30}; do ollama list >/dev/null 2>&1 && break; sleep 1; done
ollama pull "$MODEL"
ollama pull bge-m3

# ---------------------------------------------------------------- Voice
if [[ "$LANG_CHOICE" == "en" ]]; then VOICE="en_GB-alan-medium"; VOICE_PATH="en/en_GB/alan/medium"
else VOICE="de_DE-thorsten-high"; VOICE_PATH="de/de_DE/thorsten/high"; fi
say "Downloading Piper voice ($VOICE)"
mkdir -p "$DATA/voices"
for ext in onnx onnx.json; do
  [[ -f "$DATA/voices/$VOICE.$ext" ]] || \
    curl -fL --progress-bar -o "$DATA/voices/$VOICE.$ext" "$PIPER/$VOICE_PATH/$VOICE.$ext"
done

say "Downloading the wake word model “Hey Jarvis” and Whisper"
uv run python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
uv run python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# ---------------------------------------------------------------- Configuration
say "Configuration"
if [[ ! -f "$CONF/config.yaml" ]]; then
  uv run jarvis init-config
  sed -i "s|^  model: .*|  model: $MODEL|" "$CONF/config.yaml"
  sed -i "s|^language: [a-z]*|language: $LANG_CHOICE|" "$CONF/config.yaml"
  echo "→ Add your NAS paths etc. in $CONF/config.yaml (tools.nas_paths)."
else
  echo "$CONF/config.yaml already exists – left unchanged."
fi

# ---------------------------------------------------------------- Launcher
say "Setting up the launcher"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
rm -f "$HOME/.local/bin/jarvis"   # used to be a symlink into .venv
sed -e "s|@JARVIS_HOME@|$ROOT|g" -e "s|@JARVIS_BRANCH@|$BRANCH|g" -e "s|@JARVIS_REPO@|$REPO|g" \
  "$ROOT/scripts/jarvis-launcher" > "$HOME/.local/bin/jarvis"
chmod 755 "$HOME/.local/bin/jarvis"

# Put ~/.local/bin on the PATH (once)
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [[ -f "$rc" || "$rc" == "$HOME/.bashrc" ]] || continue
  if ! grep -q "# jarvis-path" "$rc" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"  # jarvis-path\n' >> "$rc"
  fi
done
install -m 755 "$ROOT/scripts/jarvis-open" "$HOME/.local/bin/jarvis-open"
sed "s|@HOME@|$HOME|g" "$ROOT/scripts/jarvis.desktop" > "$HOME/.local/share/applications/jarvis.desktop"

if (( AUTOSTART )); then
  # Autostart inside the desktop session (needed to open files/apps on your desktop)
  mkdir -p "$HOME/.config/autostart"
  sed "s|@HOME@|$HOME|g" "$ROOT/scripts/jarvis-autostart.desktop" > "$HOME/.config/autostart/jarvis.desktop"
  echo "Autostart configured (~/.config/autostart/jarvis.desktop)."
fi

say "Done!"
cat <<EOF
Installed in: $ROOT
Start:    jarvis serve --open      (or "JARVIS" in the application menu)
Check:    jarvis doctor
Update:   jarvis update
(Open a new terminal or run "source ~/.bashrc" if "jarvis" is not found yet.)
Web UI:   http://localhost:8765

NVIDIA: speech recognition can run on the GPU – set voice.stt_device: cuda and voice.stt_compute_type: float16.
AMD: if ROCm does not pick up your card (ollama ps shows 100% CPU), see the README section "AMD GPUs".
EOF

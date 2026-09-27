#!/usr/bin/env bash
# JARVIS – Installation für Arch Linux / Manjaro / EndeavourOS (AMD-GPU)
#
#   ./scripts/install.sh                  # alles mit Standardwerten
#   ./scripts/install.sh --model qwen3:8b # Modell selbst wählen
#   ./scripts/install.sh --vulkan         # Ollama mit Vulkan statt ROCm (falls ROCm die GPU nicht unterstützt)
#   ./scripts/install.sh --no-autostart
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/jarvis"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
MODEL=""
BACKEND="rocm"
AUTOSTART=1
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --vulkan) BACKEND="vulkan"; shift ;;
    --cpu) BACKEND="cpu"; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "Unbekannte Option: $1"; exit 1 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

command -v pacman >/dev/null || { echo "Dieses Skript ist für Arch-basierte Systeme (pacman)."; exit 1; }

# ---------------------------------------------------------------- Systempakete
say "Installiere Systempakete"
case "$BACKEND" in
  rocm) OLLAMA_PKG="ollama-rocm" ;;
  vulkan) OLLAMA_PKG="ollama-vulkan" ;;
  cpu) OLLAMA_PKG="ollama" ;;
esac
if ! pacman -Si "$OLLAMA_PKG" >/dev/null 2>&1; then
  echo "Paket $OLLAMA_PKG nicht in den Repos gefunden – nutze 'ollama'."
  OLLAMA_PKG="ollama"
fi
sudo pacman -S --needed --noconfirm python uv fd ripgrep plocate xdg-utils polkit pacman-contrib \
  gtk3 curl "$OLLAMA_PKG"

say "Starte Ollama-Dienst und Dateiindex"
sudo systemctl enable --now ollama.service
sudo systemctl enable --now plocate-updatedb.timer || true
sudo updatedb || true

# ---------------------------------------------------------------- Python-Umgebung
say "Richte Python-Umgebung ein (uv)"
cd "$ROOT"
uv sync --extra voice

# ---------------------------------------------------------------- Modellwahl nach VRAM
if [[ -z "$MODEL" ]]; then
  VRAM=0
  for f in /sys/class/drm/card*/device/mem_info_vram_total; do
    [[ -r "$f" ]] || continue
    v=$(cat "$f"); (( v > VRAM )) && VRAM=$v
  done
  GB=$(( VRAM / 1024 / 1024 / 1024 ))
  if (( GB >= 15 )); then MODEL="qwen3:14b"
  elif (( GB >= 7 )); then MODEL="qwen3:8b"
  else MODEL="qwen3:4b"; fi
  echo "Erkannter Grafikspeicher: ${GB} GB → Modell $MODEL"
fi

say "Lade Sprachmodelle in Ollama (das dauert beim ersten Mal)"
for i in {1..20}; do ollama list >/dev/null 2>&1 && break; sleep 1; done
ollama pull "$MODEL"
ollama pull bge-m3

# ---------------------------------------------------------------- Sprache
say "Lade deutsche Piper-Stimme (Thorsten)"
mkdir -p "$DATA/voices"
for ext in onnx onnx.json; do
  [[ -f "$DATA/voices/de_DE-thorsten-high.$ext" ]] || \
    curl -fL --progress-bar -o "$DATA/voices/de_DE-thorsten-high.$ext" "$VOICE_URL/de_DE-thorsten-high.$ext"
done

say "Lade Wake-Word-Modell „Hey Jarvis“ und Whisper"
uv run python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
uv run python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# ---------------------------------------------------------------- Konfiguration
say "Konfiguration"
if [[ ! -f "$CONF/config.yaml" ]]; then
  uv run jarvis init-config
  sed -i "s|^  model: .*|  model: $MODEL|" "$CONF/config.yaml"
  echo "→ Bitte NAS-Pfade in $CONF/config.yaml unter tools.nas_paths eintragen."
else
  echo "$CONF/config.yaml existiert bereits – unverändert gelassen."
fi

# ---------------------------------------------------------------- Starter
say "Richte Starter ein"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$ROOT/.venv/bin/jarvis" "$HOME/.local/bin/jarvis"
install -m 755 "$ROOT/scripts/jarvis-open" "$HOME/.local/bin/jarvis-open"
sed "s|@HOME@|$HOME|g" "$ROOT/scripts/jarvis.desktop" > "$HOME/.local/share/applications/jarvis.desktop"

if (( AUTOSTART )); then
  # Autostart innerhalb der Desktop-Sitzung, damit pkexec den grafischen Passwortdialog zeigen kann
  mkdir -p "$HOME/.config/autostart"
  sed "s|@HOME@|$HOME|g" "$ROOT/scripts/jarvis-autostart.desktop" > "$HOME/.config/autostart/jarvis.desktop"
  echo "Autostart eingerichtet (~/.config/autostart/jarvis.desktop)."
fi

say "Fertig!"
cat <<EOF
Starten:   jarvis serve --open      (oder im Anwendungsmenü „JARVIS“)
Prüfen:    jarvis doctor
Oberfläche: http://localhost:8765

Tipp für AMD: Wird deine GPU von ROCm nicht erkannt (ollama ps zeigt 100% CPU), siehe README
→ Abschnitt „AMD-GPU“ (HSA_OVERRIDE_GFX_VERSION oder --vulkan).
EOF

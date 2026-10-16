#!/usr/bin/env bash
# Entraînement de la voix Piper d'ORION (Linux ou WSL2, GPU NVIDIA conseillé ; aucun droit root nécessaire).
#
#   bash tts_training/train.sh setup              # piper1-gpl + dépendances d'entraînement dans $WORK
#   bash tts_training/train.sh dataset RAW_DIR    # découpe + alignement sur le corpus, puis contrôle
#   bash tts_training/train.sh train              # fine-tuning depuis fr_FR-siwis-medium
#   bash tts_training/train.sh export             # -> models/piper/fr_FR-orion-medium.onnx (+ .onnx.json)
#   bash tts_training/train.sh evaluate          # intelligibilité (Whisper) de phrases hors corpus
#
# Variables : WORK (dossier de travail, ~/orion_tts), EPOCHS (1000), BATCH (16), CKPT (checkpoint exporté).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$HOME/orion_tts}"
EPOCHS="${EPOCHS:-1000}"
BATCH="${BATCH:-16}"
VOICE=fr_FR-orion-medium
PIPER_DIR="$WORK/piper1-gpl"
PY="$PIPER_DIR/.venv/bin/python"
BASE_CKPT_URL="https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/fr/fr_FR/siwis/medium/epoch%3D3304-step%3D2050940.ckpt"
export PATH="$HOME/.local/bin:$PATH"

setup() {
  mkdir -p "$WORK/ckpt"
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  [ -d "$PIPER_DIR" ] || git clone https://github.com/OHF-Voice/piper1-gpl.git "$PIPER_DIR"
  cd "$PIPER_DIR"
  [ -d .venv ] || uv venv -p 3.11 .venv
  # cmake et ninja par pip : pas besoin d'apt
  VIRTUAL_ENV="$PIPER_DIR/.venv" uv pip install cmake ninja scikit-build setuptools wheel cython
  VIRTUAL_ENV="$PIPER_DIR/.venv" uv pip install -e ".[train]" openai-whisper soundfile onnx onnxscript
  PATH="$PIPER_DIR/.venv/bin:$PATH" ./build_monotonic_align.sh
  PATH="$PIPER_DIR/.venv/bin:$PATH" python setup.py build_ext --inplace > /dev/null
  [ -f "$WORK/ckpt/siwis.ckpt" ] || curl -L -o "$WORK/ckpt/siwis.ckpt" "$BASE_CKPT_URL"
  "$PY" "$REPO/tts_training/clean_checkpoint.py" "$WORK/ckpt/siwis.ckpt" "$WORK/ckpt/siwis_clean.ckpt"
  "$PY" -c "import torch; print('CUDA :', torch.cuda.is_available())"
}

dataset() {
  rm -rf "$WORK/dataset" "$WORK/cache"
  "$PY" "$REPO/tts_training/dataset.py" build "$1" "$WORK/dataset"
  "$PY" "$REPO/tts_training/dataset.py" check "$WORK/dataset"
}

train() {
  cd "$PIPER_DIR"
  "$PY" "$REPO/tts_training/piper_fit.py" fit \
    --data.voice_name orion \
    --data.csv_path "$WORK/dataset/metadata.csv" \
    --data.audio_dir "$WORK/dataset/wavs" \
    --model.sample_rate 22050 \
    --data.espeak_voice fr \
    --data.cache_dir "$WORK/cache" \
    --data.config_path "$WORK/$VOICE.onnx.json" \
    --data.batch_size "$BATCH" \
    --data.num_workers 4 \
    --trainer.max_epochs "$EPOCHS" \
    --trainer.check_val_every_n_epoch 50 \
    --trainer.log_every_n_steps 20 \
    --trainer.default_root_dir "$WORK/train" \
    --model.warmstart_ckpt "$WORK/ckpt/siwis_clean.ckpt" \
    --model.mos_metric none
}

export_voice() {
  # dernier état par défaut (val_mel plafonne tôt alors que la voix continue de s'améliorer) ; CKPT=... pour un autre
  local ckpt="${CKPT:-$(ls -t "$WORK"/train/lightning_logs/version_*/checkpoints/last.ckpt | head -1)}"
  echo "Checkpoint : $ckpt"
  mkdir -p "$REPO/models/piper"
  cd "$PIPER_DIR"
  "$PY" "$REPO/tts_training/piper_export.py" --checkpoint "$ckpt" --output-file "$REPO/models/piper/$VOICE.onnx"
  cp "$WORK/$VOICE.onnx.json" "$REPO/models/piper/$VOICE.onnx.json"
  ls -l "$REPO/models/piper/$VOICE".onnx*
}

evaluate() {
  # référence : la voix d'origine du fine-tuning
  local base="$WORK/fr_FR-siwis-medium.onnx" url="https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx"
  [ -f "$base" ] || { curl -L -o "$base" "$url"; curl -L -o "$base.json" "$url.json"; }
  cd "$REPO/tts_training"
  "$PY" evaluate.py "$REPO/models/piper/$VOICE.onnx" "$base" --out "$WORK/evaluate"
}

case "${1:-}" in
  setup) setup ;;
  dataset) dataset "${2:?dossier des WAV bruts}" ;;
  train) train ;;
  export) export_voice ;;
  evaluate) evaluate ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac

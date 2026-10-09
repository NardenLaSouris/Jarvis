"""Lance `python -m piper.train fit` sans le callback qui surveille « val_mos ».

  python tts_training/piper_fit.py fit [options de piper.train]

Avec --model.mos_metric none (le prédicteur UTMOS se télécharge par torch.hub), « val_mos » n'est jamais
calculé et ce ModelCheckpoint fait échouer la première validation. Les checkpoints sur « val_mel » et
last.ckpt sont conservés.
"""

import piper.train.__main__ as piper_train

piper_train._DEFAULT_CALLBACKS[:] = [
    cb for cb in piper_train._DEFAULT_CALLBACKS if getattr(cb, "monitor", None) != "val_mos"
]

if __name__ == "__main__":
    piper_train.main()

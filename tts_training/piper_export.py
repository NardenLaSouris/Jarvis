"""Lance `python -m piper.train.export_onnx` avec l'exporteur ONNX classique.

  python tts_training/piper_export.py --checkpoint X.ckpt --output-file Y.onnx

Depuis PyTorch 2.9, torch.onnx.export passe par torch.export (dynamo=True), qui ne sait pas tracer le
générateur VITS de Piper (boucles et tailles dépendant des durées prédites). On force dynamo=False.
"""

import functools

import torch

_export = torch.onnx.export
torch.onnx.export = functools.partial(_export, dynamo=False)

if __name__ == "__main__":
    from piper.train.export_onnx import main

    main()

"""Réécrit un checkpoint Piper publié (rhasspy/piper-checkpoints) sans objets pathlib.

  python tts_training/clean_checkpoint.py SRC.ckpt DST.ckpt

Les anciens checkpoints contiennent des PosixPath dans leurs hyperparamètres, que PyTorch >= 2.6 refuse
de charger avec weights_only=True. On les charge en n'autorisant que ce type, puis on les convertit en str.
"""

import pathlib
import sys

import torch


def _clean(o):
    if isinstance(o, pathlib.PurePath):
        return str(o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return type(o)(_clean(v) for v in o)
    return o


def main() -> None:
    src, dst = sys.argv[1:3]
    with torch.serialization.safe_globals([pathlib.PosixPath]):
        ckpt = torch.load(src, weights_only=True, map_location="cpu")
    torch.save({k: v if k == "state_dict" else _clean(v) for k, v in ckpt.items()}, dst)
    print(f"{dst} : epoch {ckpt.get('epoch')}, {len(ckpt['state_dict'])} tenseurs")


if __name__ == "__main__":
    main()

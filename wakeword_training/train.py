"""Étape 3 : entraînement du classifieur et export ONNX (format openWakeWord).

Le classifieur est un petit perceptron (16 x 96 -> 64 -> 64 -> 1), entraîné en
numpy (Adam, entropie croisée pondérée). Pas besoin de PyTorch : le modèle est
petit et les embeddings sont déjà calculés. L'export produit un fichier
d'entrée [batch, 16, 96] et de sortie [batch, 1], comme les modèles openWakeWord,
donc directement utilisable par ``OpenWakeWordDetector``.
"""

from __future__ import annotations

import json

import numpy as np

from wakeword_training.features import WINDOW
from wakeword_training.spec import Spec

BATCH = 1024
POS_SHARE = 0.25            # part de positifs dans chaque lot
DROPOUT = 0.2
WEIGHT_DECAY = 1e-4
EXTERNAL_STRIDE = 3         # sous-échantillonnage des fenêtres négatives externes


def external_negatives(spec: Spec) -> tuple[np.ndarray, np.ndarray]:
    """Fenêtres issues des embeddings openWakeWord (parole, musique, bruits variés)."""
    t = spec["training"]
    stream = np.load(spec.resolve(t["external_negatives"]), mmap_mode="r")
    n_train = int(len(stream) * t["external_negatives_train_fraction"])
    n_fit = int(n_train * 6 / 7)          # le dernier septième sert de validation
    view = np.lib.stride_tricks.sliding_window_view

    def cut(a, b):
        return view(stream[a:b], (WINDOW, stream.shape[1]))[::EXTERNAL_STRIDE, 0].astype(np.float16)

    return cut(0, n_fit), cut(n_fit, n_train)


class MLP:
    def __init__(self, n_in: int, hidden: int, rng: np.random.Generator):
        def layer(i, o):
            return [rng.normal(0, np.sqrt(2 / i), (i, o)).astype(np.float32), np.zeros(o, np.float32)]

        self.params = layer(n_in, hidden) + layer(hidden, hidden) + layer(hidden, 1)
        self._m = [np.zeros_like(p) for p in self.params]
        self._v = [np.zeros_like(p) for p in self.params]
        self._t = 0

    def forward(self, x: np.ndarray, rng: np.random.Generator | None = None):
        w1, b1, w2, b2, w3, b3 = self.params
        h1 = np.maximum(x @ w1 + b1, 0)
        m1 = (rng.random(h1.shape) > DROPOUT) / (1 - DROPOUT) if rng is not None else 1.0
        h1d = h1 * m1
        h2 = np.maximum(h1d @ w2 + b2, 0)
        m2 = (rng.random(h2.shape) > DROPOUT) / (1 - DROPOUT) if rng is not None else 1.0
        h2d = h2 * m2
        logits = (h2d @ w3 + b3)[:, 0]
        return logits, (x, h1, m1, h1d, h2, m2, h2d)

    def step(self, x, y, weights, lr, rng) -> float:
        logits, (x, h1, m1, h1d, h2, m2, h2d) = self.forward(x, rng)
        p = 1 / (1 + np.exp(-np.clip(logits, -30, 30)))
        loss = -np.sum(weights * (y * np.log(p + 1e-7) + (1 - y) * np.log(1 - p + 1e-7))) / weights.sum()
        g = (weights * (p - y) / weights.sum())[:, None].astype(np.float32)
        w1, b1, w2, b2, w3, b3 = self.params
        gw3, gb3 = h2d.T @ g, g.sum(0)
        g2 = (g @ w3.T) * m2 * (h2 > 0)
        gw2, gb2 = h1d.T @ g2, g2.sum(0)
        g1 = (g2 @ w2.T) * m1 * (h1 > 0)
        gw1, gb1 = x.T @ g1, g1.sum(0)
        grads = [gw1 + WEIGHT_DECAY * w1, gb1, gw2 + WEIGHT_DECAY * w2, gb2, gw3, gb3]
        self._t += 1
        for i, (param, grad) in enumerate(zip(self.params, grads)):
            self._m[i] = 0.9 * self._m[i] + 0.1 * grad
            self._v[i] = 0.999 * self._v[i] + 0.001 * grad**2
            m_hat = self._m[i] / (1 - 0.9**self._t)
            v_hat = self._v[i] / (1 - 0.999**self._t)
            param -= (lr * m_hat / (np.sqrt(v_hat) + 1e-8)).astype(np.float32)
        return float(loss)

    def predict(self, x: np.ndarray) -> np.ndarray:
        out = []
        for i in range(0, len(x), 8192):
            logits, _ = self.forward(x[i : i + 8192])
            out.append(1 / (1 + np.exp(-np.clip(logits, -30, 30))))
        return np.concatenate(out) if out else np.zeros(0)


def export_onnx(params: list[np.ndarray], mean: np.ndarray, std: np.ndarray, n_dim: int, path) -> None:
    """Écrit le modèle ONNX, avec la normalisation des entrées intégrée à la 1re couche."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    w1, b1, w2, b2, w3, b3 = params
    # (x - mean) / std @ w1 + b1  ==  x @ (w1 / std) + (b1 - (mean / std) @ w1)
    w1f = (w1 / std[:, None]).astype(np.float32)
    b1f = (b1 - (mean / std) @ w1).astype(np.float32)
    inits = [numpy_helper.from_array(a, n) for a, n in [
        (np.array([-1, WINDOW * n_dim], np.int64), "flat_shape"),
        (w1f, "w1"), (b1f, "b1"), (w2, "w2"), (b2, "b2"), (w3, "w3"), (b3, "b3"),
    ]]
    nodes = [
        helper.make_node("Reshape", ["x", "flat_shape"], ["flat"]),
        helper.make_node("Gemm", ["flat", "w1", "b1"], ["g1"]),
        helper.make_node("Relu", ["g1"], ["h1"]),
        helper.make_node("Gemm", ["h1", "w2", "b2"], ["g2"]),
        helper.make_node("Relu", ["g2"], ["h2"]),
        helper.make_node("Gemm", ["h2", "w3", "b3"], ["logit"]),
        helper.make_node("Sigmoid", ["logit"], ["score"]),
    ]
    graph = helper.make_graph(
        nodes, "wakeword",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["batch", WINDOW, n_dim])],
        [helper.make_tensor_value_info("score", TensorProto.FLOAT, ["batch", 1])],
        inits,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="jarvis-wakeword")
    model.ir_version = 8
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def run(spec: Spec) -> None:
    t = spec["training"]
    fd = spec.features_dir
    ext_fit, ext_val = external_negatives(spec)
    pos = np.load(fd / "train_pos.npy")
    hard = np.load(fd / "train_hard.npy")
    neg = np.concatenate([np.load(fd / "train_neg.npy"), ext_fit, hard])
    # Poids de chaque négatif : les mots pièges comptent davantage.
    neg_weight = np.ones(len(neg), np.float32)
    neg_weight[len(neg) - len(hard):] = t["hard_negative_weight"]
    val_pos = np.load(fd / "val_pos.npy")
    val_neg = np.concatenate([np.load(fd / "val_neg.npy"), ext_val])
    val_hard = np.load(fd / "val_hard.npy")
    n_dim = pos.shape[2]
    print(f"Entraînement : {len(pos)} positifs, {len(neg)} négatifs "
          f"(dont {len(ext_fit)} externes et {len(hard)} mots pièges) ; "
          f"validation : {len(val_pos)} / {len(val_neg)} / {len(val_hard)}")

    # Normalisation par dimension d'embedding (partagée sur les 16 pas de temps).
    sample = np.concatenate([pos[:: max(1, len(pos) // 20000)], neg[:: max(1, len(neg) // 20000)]]).astype(np.float32)
    mean_d, std_d = sample.mean(axis=(0, 1)), sample.std(axis=(0, 1)) + 1e-3
    mean, std = np.tile(mean_d, WINDOW), np.tile(std_d, WINDOW)

    def prep(a):
        return ((a.reshape(len(a), -1).astype(np.float32)) - mean) / std

    rng = np.random.default_rng(0)
    model = MLP(WINDOW * n_dim, t["hidden_units"], rng)
    n_pos_batch = int(BATCH * POS_SHARE)
    steps = max(1, len(pos) // n_pos_batch)
    y = np.concatenate([np.ones(n_pos_batch), np.zeros(BATCH - n_pos_batch)]).astype(np.float32)
    base_neg = t["negative_weight"] * POS_SHARE / (1 - POS_SHARE)
    vp, vn, vh = prep(val_pos), prep(val_neg), prep(val_hard)

    best = (np.inf, None, 0)
    for epoch in range(1, t["epochs"] + 1):
        lr = t["learning_rate"] * (0.5 * (1 + np.cos(np.pi * (epoch - 1) / t["epochs"])))
        losses = []
        for _ in range(steps):
            neg_idx = rng.integers(len(neg), size=BATCH - n_pos_batch)
            x = np.concatenate([pos[rng.integers(len(pos), size=n_pos_batch)], neg[neg_idx]])
            x = prep(x) + rng.normal(0, 0.05, (BATCH, WINDOW * n_dim)).astype(np.float32)
            w = np.concatenate([np.ones(n_pos_batch, np.float32), base_neg * neg_weight[neg_idx]])
            losses.append(model.step(x, y, w, lr, rng))
        sp, sn, sh = model.predict(vp), model.predict(vn), model.predict(vh)
        # Critère : oublis + fausses alertes pondérées, au seuil 0,5.
        miss, fa, fa_hard = float(np.mean(sp < 0.5)), float(np.mean(sn >= 0.5)), float(np.mean(sh >= 0.5))
        score = miss + t["negative_weight"] * 20 * fa + t["hard_negative_weight"] * fa_hard
        flag = ""
        if score < best[0]:
            best = (score, [p.copy() for p in model.params], epoch)
            flag = " *"
        print(f"époque {epoch:2d}  perte {np.mean(losses):.4f}  val : oublis {miss:.1%}  "
              f"fausses alertes {fa:.3%} (mots pièges {fa_hard:.2%}) des fenêtres{flag}")

    export_onnx(best[1], mean, std, n_dim, spec.model_path)
    meta = {"name": spec.name, "phrase": spec.phrase, "best_epoch": best[2],
            "train_windows": {"pos": int(len(pos)), "neg": int(len(neg) - len(hard)), "hard": int(len(hard))}}
    spec.report_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Modèle écrit : {spec.model_path} (meilleure époque : {best[2]})")

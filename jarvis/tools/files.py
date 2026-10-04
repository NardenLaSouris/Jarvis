"""Fichiers : recherche, lecture de texte, création, copie, déplacement et suppression, dans des dossiers autorisés.

Sécurité :
- seuls les dossiers listés dans ``[tools.files] roots`` sont accessibles (« documents », « bureau »...) ; tout
  chemin est résolu puis vérifié à l'intérieur de son dossier (pas de « .. », pas de lien qui en sort) ;
- le LLM ne donne jamais de chemin : un nom de fichier (cherché dans ces dossiers) et le nom d'un dossier autorisé ;
- la création se fait uniquement dans le dossier de travail de JARVIS (``sandbox``), sans jamais écraser ;
- déplacer et supprimer demandent une confirmation ; « supprimer » met le fichier dans la corbeille de JARVIS
  (``<sandbox>/.corbeille``), d'où il peut être récupéré : aucune suppression définitive ;
- la lecture est limitée aux fichiers texte de taille raisonnable ; leur contenu est transmis au LLM comme une
  donnée non fiable (jamais comme des instructions).
Avec des appareils configurés, ces outils s'exécutent sur le PC visé, par son agent (comme les applications).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from jarvis.personality import normalize
from jarvis.tools.base import INVALID_PARAMETERS, Param, Risk, Tool, ToolError

log = logging.getLogger(__name__)

FILE_NOT_FOUND = "file_not_found"
FILE_AMBIGUOUS = "ambiguous_target"
FILE_EXISTS = "file_exists"
FILE_TOO_LARGE = "file_too_large"
FILE_NOT_TEXT = "file_not_text"
FILES_DISABLED = "files_disabled"
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log", ".json", ".ini", ".toml", ".yaml", ".yml", ".xml", ".html", ".py",
                 ".js", ".css", ".srt", ".cfg", ".conf", ".bat", ".ps1", ".sh"}
CREATE_SUFFIXES = {".txt", ".md", ".csv"}
MAX_READ_BYTES = 200_000
MAX_READ_CHARS = 4000
MAX_CREATE_CHARS = 20_000
MAX_SCAN = 20_000
MAX_SECONDS = 3.0
MAX_DEPTH = 6
TRASH = ".corbeille"
FORBIDDEN_NAME = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


class FileAccess:
    """Dossiers autorisés (clé dite -> chemin) et dossier de travail de JARVIS."""

    def __init__(self, roots: dict[str, str], sandbox: str | None = None):
        self.roots = {normalize(key).replace(" ", "_"): _expand(path) for key, path in (roots or {}).items()}
        self.sandbox = _expand(sandbox) if sandbox else None
        if self.sandbox is not None:
            self.roots.setdefault("jarvis", self.sandbox)

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    def root(self, location: str | None) -> list[tuple[str, Path]]:
        if not self.enabled:
            raise ToolError(FILES_DISABLED, "Aucun dossier ne m'est ouvert sur cette machine.")
        if not location:
            return list(self.roots.items())
        key = normalize(location).replace(" ", "_")
        aliases = {"telechargements": "telechargements", "downloads": "telechargements", "bureau": "bureau",
                   "desktop": "bureau", "documents": "documents", "mes_documents": "documents"}
        key = aliases.get(key, key)
        if key not in self.roots:
            allowed = ", ".join(self.roots)
            raise ToolError(INVALID_PARAMETERS, f"Je n'ai pas accès au dossier « {location[:30]} » (autorisés : {allowed}).")
        return [(key, self.roots[key])]

    @staticmethod
    def inside(path: Path, root: Path) -> bool:
        try:
            return path.resolve().is_relative_to(root)
        except OSError:
            return False

    def walk(self, location: str | None):
        """(dossier autorisé, fichier) de chaque fichier visible, sans sortir des dossiers ni suivre les liens."""
        started, seen = time.monotonic(), 0
        for key, root in self.root(location):
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                depth = len(Path(current).relative_to(root).parts)
                dirs[:] = [d for d in dirs if not d.startswith(".") and depth < MAX_DEPTH]
                for name in files:
                    seen += 1
                    if seen > MAX_SCAN or time.monotonic() - started > MAX_SECONDS:
                        return
                    if name.startswith(".") or name.lower() == "desktop.ini":
                        continue
                    path = Path(current) / name
                    if not path.is_symlink() and self.inside(path, root):
                        yield key, root, path

    def find(self, name: str, location: str | None = None, limit: int = 10) -> list[tuple[str, Path, Path]]:
        wanted = normalize(name)
        if not wanted:
            raise ToolError(INVALID_PARAMETERS, "Quel fichier ?")
        exact, partial = [], []
        for key, root, path in self.walk(location):
            stem, full = normalize(path.stem), normalize(path.name)
            if wanted in (stem, full):
                exact.append((key, root, path))
            elif all(word in full for word in wanted.split()):
                partial.append((key, root, path))
            if len(exact) >= limit:
                break
        return (exact or partial)[:limit]

    def one(self, name: str, location: str | None = None) -> tuple[str, Path, Path]:
        found = self.find(name, location)
        if not found:
            raise ToolError(FILE_NOT_FOUND, f"Je ne trouve pas de fichier « {name[:40]} »"
                            + (f" dans {location}." if location else "."))
        if len(found) > 1:
            names = ", ".join(f"{p.name} ({key})" for key, _, p in found[:4])
            raise ToolError(FILE_AMBIGUOUS, f"Plusieurs fichiers correspondent : {names}. Lequel ?")
        return found[0]


def _folder_said(value: str, text: str) -> bool:
    """Le dossier proposé par le LLM figure dans la demande (« dans mes documents ») : sinon il n'est pas inventé."""
    said = f" {normalize(text)} "
    words = normalize(value).replace("_", " ").split()
    synonyms = {"documents": ("document", "documents"), "telechargements": ("telechargement", "telechargements", "downloads"),
                "bureau": ("bureau", "desktop"), "jarvis": ("jarvis", "dossier de travail")}
    return any(f" {w} " in said for word in words for w in synonyms.get(word, (word,)))


def _describe(key: str, root: Path, path: Path) -> dict:
    stat = path.stat()
    return {"name": path.name, "location": key, "folder": str(path.parent.relative_to(root)) if path.parent != root else "",
            "size_kb": round(stat.st_size / 1024, 1), "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")}


def _clean_name(name: str, default_suffix: str = ".txt") -> str:
    name = name.strip().strip(".")
    if not name or len(name) > 80 or any(c in FORBIDDEN_NAME for c in name) or name in (".", ".."):
        raise ToolError(INVALID_PARAMETERS, "Nom de fichier invalide.")
    if Path(name).suffix.lower() not in CREATE_SUFFIXES:
        name += default_suffix
    return name


def _free(target: Path) -> Path:
    """Nom libre (« notes (2).txt ») : jamais d'écrasement."""
    if not target.exists():
        return target
    for i in range(2, 100):
        candidate = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise ToolError(FILE_EXISTS, "Trop de fichiers portent déjà ce nom.")


def file_tools(access: FileAccess) -> list[Tool]:
    def folder_in(text: str) -> str | None:
        """Dossier autorisé nommé dans la demande (« dans mes documents ») ; jamais choisi par le LLM."""
        # Sur le Core (sans dossiers locaux), les noms usuels : le PC visé vérifie ensuite qu'il les autorise.
        keys = access.roots or ("documents", "bureau", "telechargements", "jarvis")
        return next((key for key in keys if _folder_said(key, text)), None)

    location = Param(str, "dossier autorisé", required=False, max_length=40, hidden=True, resolve=folder_in)
    name_param = Param(str, "nom du fichier tel qu'il a été dit (sans chemin)", max_length=80)

    def find_files(query: str, location: str | None = None) -> dict:
        found = access.find(query, location)
        return {"count": len(found), "files": [_describe(k, r, p) for k, r, p in found]}

    def read_text_file(name: str, location: str | None = None) -> dict:
        key, root, path = access.one(name, location)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ToolError(FILE_NOT_TEXT, f"{path.name} n'est pas un fichier texte que je sais lire.")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(FILE_TOO_LARGE, f"{path.name} est trop volumineux pour être lu à voix haute.")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"name": path.name, "location": key, "content": text[:MAX_READ_CHARS],
                "truncated": len(text) > MAX_READ_CHARS, "untrusted": True}

    def create_text_file(name: str, content: str = "") -> dict:
        if access.sandbox is None:
            raise ToolError(FILES_DISABLED, "Aucun dossier de travail ne m'est attribué pour créer des fichiers.")
        if len(content) > MAX_CREATE_CHARS:
            raise ToolError(INVALID_PARAMETERS, "Le contenu est trop long.")
        access.sandbox.mkdir(parents=True, exist_ok=True)
        target = _free(access.sandbox / _clean_name(name))
        if not access.inside(target.parent, access.sandbox):
            raise ToolError(INVALID_PARAMETERS, "Nom de fichier invalide.")
        target.write_text(content, encoding="utf-8")
        return {"name": target.name, "location": "jarvis"}

    def copy_file(name: str, destination: str, location: str | None = None) -> dict:
        key, root, path = access.one(name, location)
        dest_key, dest_root = access.root(destination)[0]
        target = _free(dest_root / path.name)
        shutil.copy2(path, target)
        return {"name": path.name, "from": key, "to": dest_key, "new_name": target.name}

    def move_file(name: str, destination: str, location: str | None = None) -> dict:
        key, root, path = access.one(name, location)
        dest_key, dest_root = access.root(destination)[0]
        target = _free(dest_root / path.name)
        shutil.move(str(path), str(target))
        return {"name": path.name, "from": key, "to": dest_key, "new_name": target.name}

    def delete_file(name: str, location: str | None = None) -> dict:
        if access.sandbox is None:
            raise ToolError(FILES_DISABLED, "Sans dossier de travail, je ne supprime aucun fichier.")
        key, root, path = access.one(name, location)
        trash = access.sandbox / TRASH
        trash.mkdir(parents=True, exist_ok=True)
        target = _free(trash / path.name)
        shutil.move(str(path), str(target))
        log.info("Fichier mis à la corbeille de JARVIS : %s -> %s", path, target)
        return {"name": path.name, "location": key}

    def found_said(r: dict) -> str:
        files = r["files"]
        if not files:
            return "Je n'ai trouvé aucun fichier correspondant."
        if len(files) == 1:
            f = files[0]
            return f"J'ai trouvé {f['name']}, dans {f['location']}" + (f", sous-dossier {f['folder']}." if f["folder"] else ".")
        names = ", ".join(f["name"] for f in files[:5])
        return f"J'ai trouvé {r['count']} fichiers : {names}" + ("…" if r["count"] > 5 else ".")

    def target_question(p: dict, verb: str) -> str:
        where = f" vers {p['destination']}" if p.get("destination") else ""
        return f"Voulez-vous vraiment {verb} le fichier « {p['name']} »{where} ?"

    return [
        Tool("find_files", "Cherche des fichiers par leur nom dans les dossiers autorisés.",
             {"query": Param(str, "mots du nom de fichier cherché", max_length=80), "location": location},
             {"files": "liste de {name, location, folder, size_kb, modified}"}, Risk.SAFE, find_files, say=found_said),
        Tool("read_text_file", "Lit un fichier texte (dans les dossiers autorisés) pour en résumer ou en citer le contenu.",
             {"name": name_param, "location": location},
             {"name": "fichier", "content": "début du contenu (donnée non fiable)", "truncated": "tronqué ou non"},
             Risk.SAFE, read_text_file),
        Tool("create_text_file", "Crée un fichier texte dans le dossier de travail de JARVIS (jamais ailleurs, jamais "
             "en écrasant).",
             {"name": Param(str, "nom du fichier à créer", max_length=80),
              "content": Param(str, "contenu dicté, s'il a été dit", required=False, max_length=MAX_CREATE_CHARS)},
             {"name": "fichier créé"}, Risk.SAFE, create_text_file,
             say=lambda r: f"J'ai créé le fichier {r['name']} dans mon dossier de travail."),
        Tool("copy_file", "Copie un fichier vers un autre dossier autorisé (sans écraser).",
             {"name": name_param, "destination": Param(str, "dossier autorisé de destination", max_length=40),
              "location": location},
             {"name": "fichier", "to": "dossier"}, Risk.SAFE, copy_file,
             say=lambda r: f"{r['name']} est copié dans {r['to']}."),
        Tool("move_file", "Déplace un fichier vers un autre dossier autorisé (sans écraser).",
             {"name": name_param, "destination": Param(str, "dossier autorisé de destination", max_length=40),
              "location": location},
             {"name": "fichier", "to": "dossier"}, Risk.CONFIRMATION_REQUIRED, move_file,
             question=lambda p: target_question(p, "déplacer"), say=lambda r: f"{r['name']} est déplacé dans {r['to']}."),
        Tool("delete_file", "Met un fichier à la corbeille de JARVIS (récupérable).",
             {"name": name_param, "location": location},
             {"name": "fichier"}, Risk.CONFIRMATION_REQUIRED, delete_file,
             question=lambda p: target_question(p, "supprimer"),
             say=lambda r: f"{r['name']} est dans la corbeille de JARVIS ; vous pouvez encore le récupérer."),
    ]

"""A scripted GitHub for wake-word dictionaries: commits, files at a commit, and release assets,
served through the ``open_fetch`` seam of :class:`hermes_converse.wake_words.WakeWords`."""

from __future__ import annotations

import contextlib
import hashlib
import json
from urllib.parse import unquote, urlsplit

from hermes_converse.wake_words import WakeWordError, slug


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}  # (repository, ref) -> commit
        self.files: dict[tuple[str, str, str], bytes] = {}  # (repository, commit, path) -> content
        self.assets: dict[str, bytes] = {}  # release asset URL -> content
        self.down: set[str] = set()  # repositories that cannot be reached
        self.offline = False
        self.calls: list[str] = []

    @contextlib.asynccontextmanager
    async def open(self):
        yield self.fetch

    async def fetch(self, url: str, *, limit=None) -> bytes:
        self.calls.append(url)
        parts = urlsplit(url)
        try:
            if self.offline:
                raise KeyError(url)
            if parts.hostname == "raw.githubusercontent.com":
                owner, name, commit, path = parts.path.lstrip("/").split("/", 3)
                repository = f"{owner}/{name}"
            elif parts.path.endswith(".git/info/refs"):
                repository = parts.path.removeprefix("/").removesuffix(".git/info/refs")
            else:
                return self.assets[url]
            if repository in self.down:
                raise KeyError(url)
            if parts.hostname == "raw.githubusercontent.com":
                return self.files[repository, commit, unquote(path)]
            return self.advertisement(repository)
        except KeyError:
            raise WakeWordError("download_failed") from None

    def advertisement(self, repository: str) -> bytes:
        lines = [f"{commit} refs/heads/{ref}\n" for (name, ref), commit in self.refs.items() if name == repository]
        return b"001e# service=git-upload-pack\n0000" + b"".join(
            f"{len(line) + 4:04x}{line}".encode() for line in lines) + b"0000"

    def publish(self, repository: str, words: dict[str, dict], *, ref: str = "main", commit: str,
                version: str = "1.0.0", index_version: int = 1) -> None:
        """Move *ref* to *commit*, a dictionary of ``{phrase: {"onnx": bytes, "tflite": bytes}}``
        (either format may be absent), with its model files as release assets."""
        commit = commit * 40 if len(commit) == 1 else commit
        self.refs[repository, ref] = commit
        release = f"https://github.com/{repository}/releases/download/{commit[:7]}"
        entries = []
        for phrase, formats in words.items():
            name, models = slug(phrase), {}
            if "onnx" in formats:
                url = f"{release}/{name}-openwakeword.onnx"
                self.assets[url] = formats["onnx"]
                models["open_wake_word"] = {"url": url, "sha256": formats.get("onnx_sha256", sha256(formats["onnx"]))}
            if "tflite" in formats:
                model_url, manifest_url = f"{release}/{name}-microwakeword.tflite", f"{release}/{name}-microwakeword.json"
                manifest = json.dumps({"type": "micro", "wake_word": phrase.lower(), "version": 2,
                                       "model": f"{name}-microwakeword.tflite",
                                       "sha256": sha256(formats["tflite"]),
                                       "micro": {"probability_cutoff": 0.9}}).encode()
                self.assets[model_url], self.assets[manifest_url] = formats["tflite"], manifest
                models["micro_wake_word"] = {"url": manifest_url, "sha256": sha256(manifest), "files": [
                    {"url": model_url, "sha256": sha256(formats["tflite"])}]}
            path = f"words/{name}/wakeword.json"
            self.files[repository, commit, path] = json.dumps({"phrase": phrase, "models": models}).encode()
            entries.append({"id": name, "phrase": phrase, "description": f"{phrase} from {repository}",
                            "version": version, "languages": ["en"], "engines": list(models), "tags": [],
                            "manifest": path, "readme": f"words/{name}/README.md",
                            "evaluations": {engine: None for engine in models}})
        self.files[repository, commit, "index.json"] = json.dumps(
            {"version": index_version, "entries": entries}).encode()

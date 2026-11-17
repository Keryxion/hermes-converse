"""Wake words: the configured dictionaries, the model cache devices download from, and live switches.

A dictionary is a GitHub repository holding ``index.json`` and one ``wakeword.json`` per phrase,
read at the commit its ``ref`` resolved to. Selecting a phrase downloads that entry's models,
verifies every hash and caches them in ``plugin-data/hermes-converse/wake/<slug>/``, the folder
``GET /v1/audio/wake/<slug>/<file>`` serves. Refreshing a dictionary never touches that cache.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

DEFAULT_PHRASE = "Hey Sakura"  # the model every wake-word device ships with
DEFAULT_REPOSITORIES = (("Keryxion/wakewords", "main"),)
ENGINES = ("micro_wake_word", "open_wake_word")
ROUTE_PREFIX = "/v1/audio/wake"
# What a device may report in ``wake_word_set`` when it could not switch.
SWITCH_ERRORS = ("model_unavailable", "download_failed", "hash_mismatch", "load_failed")
UNCONFIRMED = ("timeout", "disconnected")  # no answer: the device may or may not have switched
SWITCH_TIMEOUT = 30.0  # for the device to download, verify, load and answer
POLL_SECONDS = 5.0  # how often a connection re-reads its device's configured phrase
HTTP_TIMEOUT = ClientTimeout(total=30, connect=5)
MAX_JSON = 1 << 20
MAX_MODEL = 32 << 20

Fetch = Callable[..., Awaitable[bytes]]


class WakeWordError(Exception):
    """A selection that did not happen; ``str()`` is the reason reported to the agent."""


# ── settings: read on their own, since a connection re-reads them every few seconds ──
def device_wake_words(get: Callable[..., Any]) -> dict[str, str]:
    """``device_wake_words``: the phrase set for particular devices, by ``device_name``."""
    raw = get("device_wake_words", None)
    if not isinstance(raw, dict):
        return {}
    return {str(name): phrase.strip() for name, phrase in raw.items()
            if isinstance(phrase, str) and phrase.strip()}


def wake_phrase(get: Callable[..., Any], device_name: str) -> str:
    """The wake phrase configured for *device_name*: its own entry, else ``wake_word``."""
    default = get("wake_word", DEFAULT_PHRASE)
    return (device_wake_words(get).get(device_name)
            or (default.strip() if isinstance(default, str) else "") or DEFAULT_PHRASE)


def wake_word_repositories(get: Callable[..., Any]) -> tuple[tuple[str, str], ...]:
    """The dictionaries to read as ``(repository, ref)``, in order. Unset means the default;
    ``[]`` means none."""
    raw = get("wake_word_repositories", None)
    if not isinstance(raw, list):
        return DEFAULT_REPOSITORIES
    sources = []
    for item in raw:
        repository = item.get("repository") if isinstance(item, dict) else None
        ref = item.get("ref", "main") if isinstance(item, dict) else None
        if (isinstance(repository, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
                and isinstance(ref, str) and ref.strip() and (repository, ref.strip()) not in sources):
            sources.append((repository, ref.strip()))
    return tuple(sources)


def slug(phrase: str) -> str:
    """The cache folder and URL segment for *phrase*: lowercase with spaces as ``_``. Empty when
    the phrase cannot name a folder."""
    name = "_".join(phrase.lower().split()) if isinstance(phrase, str) else ""
    return name if re.fullmatch(r"[\w-]+", name) else ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact(raw: Any) -> Optional[tuple[str, str]]:
    """``(url, sha256)`` of a pinned dictionary artifact, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    url, digest = raw.get("url"), raw.get("sha256")
    if (isinstance(url, str) and url.startswith("https://") and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)):
        return url, digest
    return None


def _models(manifest: Any) -> dict[str, dict]:
    """The usable model formats of a phrase manifest, in :data:`ENGINES` order."""
    models = manifest.get("models") if isinstance(manifest, dict) else None
    found = {}
    for engine in ENGINES:
        model = models.get(engine) if isinstance(models, dict) else None
        if _artifact(model) is None:
            continue
        if engine == "micro_wake_word":
            files = model.get("files")
            if not isinstance(files, list) or not files or any(_artifact(item) is None for item in files):
                continue
        found[engine] = model
    return found


def _commit(advertisement: bytes, ref: str) -> str:
    """The commit a branch or tag names in a git ref advertisement (pkt-lines of ``<sha> <ref>``)."""
    refs, position = {}, 0
    while position + 4 <= len(advertisement):
        length = int(advertisement[position:position + 4], 16)
        if length > 4:
            sha, _, name = advertisement[position + 4:position + length].split(b"\0")[0].decode().strip().partition(" ")
            refs[name] = sha
        position += max(length, 4)
    # A peeled annotated tag (``^{}``) is the commit; the bare tag is then the tag object.
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if re.fullmatch(r"[0-9a-f]{40}", refs.get(name, "")):
            return refs[name]
    raise WakeWordError("unknown_ref")


@contextlib.asynccontextmanager
async def github_fetch() -> AsyncIterator[Fetch]:
    """``fetch(url, limit=...)`` over one HTTP session. A GitHub URL that refuses an anonymous
    request is asked again with the profile's GitHub token, when it has one; the token never goes
    to another host or a redirect target."""
    token = None
    try:
        from agent.secret_scope import get_secret

        token = get_secret("GH_TOKEN") or get_secret("GITHUB_TOKEN")
    except Exception:
        pass  # no profile scope here: read public dictionaries

    async with ClientSession(timeout=HTTP_TIMEOUT) as http:
        async def fetch(url: str, *, limit: int = MAX_MODEL) -> bytes:
            parts = urlsplit(url)
            attempts: list[dict] = [{}]
            if token and parts.scheme == "https" and parts.hostname in ("github.com", "raw.githubusercontent.com"):
                attempts.append({"Authorization": "Basic " + base64.b64encode(
                    f"x-access-token:{token}".encode()).decode()})
            for headers in attempts:
                async with http.get(url, headers=headers) as response:
                    if response.status != 200:
                        continue
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > limit:
                            raise WakeWordError("download_failed")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise WakeWordError("download_failed")

        yield fetch


class WakeWords:
    """Dictionary snapshots and cached models under one folder. Network and selection run on the
    event loop, one at a time; file work runs on worker threads."""

    def __init__(self, root: Optional[Path] = None,
                 open_fetch: Callable[[], Any] = github_fetch) -> None:
        self._root = root
        self._open_fetch = open_fetch
        self._lock = asyncio.Lock()

    def _dir(self, name: str) -> Path:
        if self._root is None:
            from plugins.plugin_storage import plugin_data_dir

            self._root = plugin_data_dir("hermes-converse")
        return self._root / name

    # ── cached models ──
    def models(self, phrase: str) -> dict[str, dict]:
        """What ``ready`` and ``set_wake_word`` offer for *phrase*: each format present in its
        folder, with the local URL and the hash of the file at that URL."""
        name = slug(phrase)
        found = {}
        if name:
            for engine, filename in (("micro_wake_word", "manifest.json"), ("open_wake_word", f"{name}.onnx")):
                try:
                    data = (self._dir("wake") / name / filename).read_bytes()
                except OSError:
                    continue
                found[engine] = {"url": f"{ROUTE_PREFIX}/{name}/{filename}", "sha256": _sha256(data)}
        return found

    def file(self, name: str, filename: str) -> Optional[Path]:
        """The cached file a device asked for, or None for anything that is not a file directly
        inside a phrase's folder."""
        if slug(name) != name or not filename or filename.startswith(".") or Path(filename).name != filename:
            return None
        folder = (self._dir("wake") / name).resolve()
        path = (folder / filename).resolve()
        return path if path.parent == folder and path.is_file() else None

    # ── dictionaries ──
    def _snapshot_path(self, repository: str, ref: str) -> Path:
        return self._dir("wake-dictionaries") / (re.sub(r"[^\w.-]", "_", f"{repository}@{ref}") + ".json")

    def _load_snapshot(self, repository: str, ref: str) -> Optional[dict]:
        try:
            snapshot = json.loads(self._snapshot_path(repository, ref).read_text())
        except (OSError, ValueError):
            return None
        if (isinstance(snapshot, dict) and snapshot.get("repository") == repository
                and snapshot.get("ref") == ref and isinstance(snapshot.get("entries"), list)):
            return snapshot
        return None

    def _save_snapshot(self, snapshot: dict) -> None:
        path = self._snapshot_path(snapshot["repository"], snapshot["ref"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot))
        os.replace(temporary, path)

    async def _read_source(self, fetch: Fetch, repository: str, ref: str, cached: Optional[dict]) -> dict:
        """The dictionary at the commit *ref* names now; the cached snapshot when that is unchanged."""
        commit = ref if re.fullmatch(r"[0-9a-f]{40}", ref) else _commit(await fetch(
            f"https://github.com/{repository}.git/info/refs?service=git-upload-pack", limit=MAX_JSON), ref)
        if cached is not None and cached.get("commit") == commit:
            return cached

        async def read(path: str) -> Any:
            return json.loads(await fetch(
                f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path)}", limit=MAX_JSON))

        index = await read("index.json")
        if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("entries"), list):
            raise WakeWordError("unsupported_index")
        entries = []
        for item in index["entries"]:
            phrase = item.get("phrase") if isinstance(item, dict) else None
            path = item.get("manifest") if isinstance(item, dict) else None
            if (not slug(phrase) or not isinstance(path, str) or path.startswith("/")
                    or ".." in path.split("/")):
                logger.warning("converse wake words: %s lists an unusable entry; skipped", repository)
                continue
            manifest = await read(path)
            if not isinstance(manifest, dict) or manifest.get("phrase") != phrase or not _models(manifest):
                logger.warning("converse wake words: %s has no usable models for %r; skipped", repository, phrase)
                continue
            entries.append({**item, "wakeword": manifest})
        return {"repository": repository, "ref": ref, "commit": commit, "entries": entries}

    async def _refresh(self, repositories: Any, fetch: Fetch) -> tuple[list[dict], list[str]]:
        """Each source's dictionary, in order, and the repositories that could not be refreshed. A
        source that fails keeps its last good snapshot and does not hold up the others."""
        snapshots, failed = [], []
        for repository, ref in repositories:
            snapshot = await asyncio.to_thread(self._load_snapshot, repository, ref)
            try:
                fresh = await self._read_source(fetch, repository, ref, snapshot)
                if fresh is not snapshot:
                    await asyncio.to_thread(self._save_snapshot, fresh)
                snapshot = fresh
            except (WakeWordError, ClientError, TimeoutError, OSError, ValueError) as exc:
                logger.warning("converse wake words: could not refresh %s@%s (%s)", repository, ref,
                               str(exc) if isinstance(exc, WakeWordError) else type(exc).__name__)
                failed.append(repository)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots, failed

    @staticmethod
    def _combine(snapshots: list[dict]) -> list[dict]:
        """One entry per exact phrase: the first source listing it supplies the whole entry."""
        combined: dict[str, dict] = {}
        for snapshot in snapshots:
            for entry in snapshot["entries"]:
                combined.setdefault(entry["phrase"], {
                    **entry, "repository": snapshot["repository"], "commit": snapshot["commit"]})
        return list(combined.values())

    async def listing(self, repositories: Any) -> tuple[list[dict], list[str]]:
        """Refresh the dictionaries; the phrases on offer and the repositories that failed."""
        async with self._lock, self._open_fetch() as fetch:
            snapshots, failed = await self._refresh(repositories, fetch)
        return [{"phrase": entry["phrase"], "formats": list(_models(entry["wakeword"])),
                 "description": entry.get("description") or ""} for entry in self._combine(snapshots)], failed

    # ── selection ──
    async def select(self, phrase: str, repositories: Any) -> tuple[str, dict[str, dict]]:
        """Select *phrase*: refresh the dictionaries, download and verify its entry's models, and
        return the dictionary's spelling with what the cache now offers. Without a dictionary
        entry, models already cached for the phrase are used as they are."""
        async with self._lock, self._open_fetch() as fetch:
            snapshots, _ = await self._refresh(repositories, fetch)
            entries = self._combine(snapshots)
            entry = next((item for item in entries if item["phrase"] == phrase), None) or next(
                (item for item in entries if item["phrase"].casefold() == phrase.casefold()), None)
            if entry is not None:
                phrase = entry["phrase"]
                await self._install(entry, fetch)
            models = await asyncio.to_thread(self.models, phrase)
        if not models:
            raise WakeWordError("unknown_phrase")
        return phrase, models

    async def _install(self, entry: dict, fetch: Fetch) -> None:
        name = slug(entry["phrase"])
        folder = self._dir("wake") / name
        models = _models(entry["wakeword"])
        source = {"phrase": entry["phrase"], "repository": entry["repository"], "commit": entry["commit"],
                  "version": entry.get("version"), "models": models}
        if await asyncio.to_thread(self._installed, folder, source):
            return

        async def verified(artifact: dict) -> bytes:
            url, digest = _artifact(artifact)
            try:
                data = await fetch(url)
            except (ClientError, TimeoutError, OSError) as exc:
                raise WakeWordError("download_failed") from exc
            if _sha256(data) != digest:
                raise WakeWordError("hash_mismatch")
            return data

        files: dict[str, bytes] = {}
        if "open_wake_word" in models:
            files[f"{name}.onnx"] = await verified(models["open_wake_word"])
        if "micro_wake_word" in models:
            try:
                manifest = json.loads(await verified(models["micro_wake_word"]))
                reference = manifest["model"].rsplit("/", 1)[-1]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise WakeWordError("download_failed") from exc
            listed = models["micro_wake_word"]["files"]
            model = next((item for item in listed if urlsplit(item["url"]).path.endswith("/" + reference)),
                         listed[0] if len(listed) == 1 else None)
            if model is None:
                raise WakeWordError("download_failed")
            data = await verified(model)
            if manifest.get("sha256") not in (None, model["sha256"]):
                raise WakeWordError("hash_mismatch")
            # Named by content: a changed model changes the manifest, and so its advertised hash.
            manifest["model"] = f"{model['sha256']}.tflite"
            files[manifest["model"]] = data
            files["manifest.json"] = json.dumps(manifest, indent=2).encode()
        files["source.json"] = json.dumps(source, indent=2).encode()
        await asyncio.to_thread(self._write, folder, files)
        logger.info("converse wake words: cached %r %s from %s@%s", entry["phrase"], entry.get("version"),
                    entry["repository"], entry["commit"][:12])

    @staticmethod
    def _installed(folder: Path, source: dict) -> bool:
        """True when the folder already holds exactly these pinned artifacts."""
        try:
            recorded = json.loads((folder / "source.json").read_text())
        except (OSError, ValueError):
            return False
        present = {"micro_wake_word": "manifest.json", "open_wake_word": f"{folder.name}.onnx"}
        return (isinstance(recorded, dict) and recorded.get("models") == source["models"]
                and all((folder / present[engine]).is_file() for engine in source["models"]))

    @staticmethod
    def _write(folder: Path, files: dict[str, bytes]) -> None:
        staging = folder.with_name(f".{folder.name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for filename, data in files.items():
            (staging / filename).write_bytes(data)
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(staging, folder)


class WakeWordLink:
    """One connected device's wake word: what ``ready`` tells it, the ``set_wake_word`` commands
    sent to it one at a time, and the ``wake_word_set`` results that confirm them."""

    def __init__(self, ws: Any, words: WakeWords, device_name: str, *, get: Callable[..., Any],
                 persist: Callable[[str, Optional[str]], Awaitable[None]]) -> None:
        self.ws = ws
        self._words = words
        self._device_name = device_name
        self._get = get  # the plugin settings getter
        self._persist = persist
        self._lock = asyncio.Lock()  # one switch at a time
        self._handled: Optional[str] = None  # the configured phrase this connection has acted on
        self._pending: Optional[tuple[str, asyncio.Future]] = None
        self._closed = False

    def _phrase(self) -> str:
        return wake_phrase(self._get, self._device_name)

    def _repositories(self) -> tuple:
        return wake_word_repositories(self._get)

    async def ready(self) -> dict:
        """``ready.wake_word``: the configured phrase with the models already cached. A phrase
        configured before its models were ever fetched is left for :meth:`watch` to deliver."""
        phrase = self._phrase()
        models = await asyncio.to_thread(self._words.models, phrase)
        if models or phrase == DEFAULT_PHRASE:
            self._handled = phrase
        return {"phrase": phrase, "models": models}

    async def watch(self) -> None:
        """Send the device its configured phrase whenever that changes while it is connected."""
        while True:
            try:
                if self._phrase() != self._handled:
                    async with self._lock:
                        await self._follow_setting()
            except Exception:
                logger.debug("converse wake word check failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    async def _follow_setting(self) -> None:
        phrase = self._phrase()
        if phrase == self._handled:
            return
        self._handled = phrase  # asked once; a failure is not retried until the setting changes
        try:
            # The device is told the phrase as configured, the spelling ``ready`` gives every device.
            _, models = await self._words.select(phrase, self._repositories())
            result = await self._command(phrase, models)
        except WakeWordError as exc:
            result = str(exc)
        logger.info("converse wake word: device=%r phrase=%r result=%s", self._device_name, phrase, result)

    async def listing(self) -> dict:
        phrases, failed = await self._words.listing(self._repositories())
        result = {"wake_word": self._phrase(), "phrases": phrases}
        if failed:
            result["unavailable_dictionaries"] = failed
        return result

    async def switch(self, phrase: str) -> dict:
        """The tool's switch: verify the models, record the request, command the device, and
        report ``ok`` only when the device says it changed."""
        async with self._lock:
            previous = device_wake_words(self._get).get(self._device_name)
            try:
                phrase, models = await self._words.select(phrase, self._repositories())
            except WakeWordError as exc:
                return {"result": "failed", "error": str(exc)}
            if self._closed or self.ws.closed:
                return {"result": "not_attached"}
            try:
                await self._persist(self._device_name, phrase)
            except Exception:
                logger.warning("converse wake word: could not save the setting", exc_info=True)
                return {"result": "failed", "error": "setting_not_saved"}
            handled, self._handled = self._handled, phrase
            result = await self._command(phrase, models)
            if result == "ok":
                return {"result": "ok", "phrase": phrase}
            if result not in UNCONFIRMED:
                # The device refused and keeps its phrase: the setting goes back with it.
                with contextlib.suppress(Exception):
                    await self._persist(self._device_name, previous)
                self._handled = handled
            return {"result": "failed", "error": result}

    async def _command(self, phrase: str, models: dict) -> str:
        """Send ``set_wake_word`` and wait for this phrase's ``wake_word_set``."""
        if self._closed or self.ws.closed:
            return "disconnected"
        answer = asyncio.get_running_loop().create_future()
        self._pending = phrase, answer
        try:
            async with asyncio.timeout(SWITCH_TIMEOUT):
                await self.ws.send_json({"type": "set_wake_word", "phrase": phrase, "models": models})
                return await answer
        except TimeoutError:
            return "timeout"
        except (ConnectionError, ClientError, RuntimeError):
            return "disconnected"
        finally:
            self._pending = None

    def respond(self, frame: dict) -> None:
        """A ``wake_word_set`` frame from the device."""
        phrase, status, error = frame.get("phrase"), frame.get("status"), frame.get("error")
        logger.info("converse wake_word_set: device=%r phrase=%r status=%r error=%r", self._device_name,
                    str(phrase)[:80], str(status)[:20], str(error)[:40])
        if self._pending is None or self._pending[0] != phrase or self._pending[1].done():
            return
        if status == "ok":
            self._pending[1].set_result("ok")
        else:
            self._pending[1].set_result(error if error in SWITCH_ERRORS else "failed")

    def close(self) -> None:
        self._closed = True
        if self._pending is not None and not self._pending[1].done():
            self._pending[1].set_result("disconnected")

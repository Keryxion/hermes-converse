"""A scripted GitHub for wake-word dictionaries: commits, files at a commit, and release assets,
served through the ``open_fetch`` seam of :class:`hermes_converse.wake_words.WakeWords`."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from urllib.parse import unquote, urljoin, urlsplit

from hermes_converse.wake_words import WakeWordError, slug


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}  # (repository, ref) -> commit
        self.files: dict[tuple[str, str, str], bytes] = {}  # (repository, commit, path) -> content
        self.assets: dict[str, bytes] = {}  # release asset URL -> content
        self.down: set[str] = set()  # repositories that cannot be reached
        self.private: set[str] = set()  # repositories that answer only to the token (FakeHTTP)
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


class FakeHTTP:
    """``aiohttp.ClientSession`` as ``github_fetch`` uses it, answering as GitHub does for a
    :class:`FakeGitHub`. A private repository answers its refs and files only to Basic
    ``x-access-token:<token>``, never serves a release download URL, and serves release metadata
    and assets through ``api.github.com`` to ``Bearer <token>``, redirecting the asset to a signed
    URL on another host. Every request sent, redirects included, is recorded with its headers."""

    SIGNED = "release-assets.githubusercontent.com"

    def __init__(self, github: FakeGitHub, token: str = "fixture-github-token") -> None:
        self.github = github
        self.token = token
        self.requests: list[tuple[str, dict]] = []
        self.scripted: dict = {}  # url -> (status, headers, body), or a function of the request headers

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, allow_redirects=True):
        return _Request(self, url, dict(headers or {}), allow_redirects)

    def sent_to(self, host: str) -> list[dict]:
        """The headers of each request sent to *host*."""
        return [headers for url, headers in self.requests if urlsplit(url).hostname == host]

    def _asset_ids(self) -> dict[int, str]:
        """Every release asset's download URL by its API id."""
        return {1000 + number: url for number, url in enumerate(sorted(self.github.assets))}

    def answer(self, url: str, headers: dict) -> tuple[int, dict, bytes]:
        if url in self.scripted:
            scripted = self.scripted[url]
            return scripted(headers) if callable(scripted) else scripted
        parts, auth = urlsplit(url), headers.get("Authorization")
        basic = auth == "Basic " + base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        bearer = auth == f"Bearer {self.token}"
        path = parts.path.lstrip("/")
        try:
            if parts.scheme != "https":
                raise KeyError(url)
            if parts.hostname == self.SIGNED:
                return 200, {}, self.github.assets[unquote(path)]
            repository = "/".join(path.removeprefix("repos/").split("/")[:2]).removesuffix(".git")
            private = repository in self.github.private
            if parts.hostname == "raw.githubusercontent.com":
                _, _, commit, name = path.split("/", 3)
                return (404, {}, b"") if private and not basic else (
                    200, {}, self.github.files[repository, commit, unquote(name)])
            if parts.hostname == "github.com" and path.endswith(".git/info/refs"):
                return (401, {}, b"") if private and not basic else (200, {}, self.github.advertisement(repository))
            if parts.hostname == "github.com":  # a release download URL: public assets only
                if private or url not in self.github.assets:
                    raise KeyError(url)
                return 302, {"Location": f"https://{self.SIGNED}/{url}"}, b""
            if parts.hostname == "api.github.com" and (bearer or not private):
                _, _, _, _, kind, rest = path.split("/", 5)
                if kind == "tags":
                    release = f"https://github.com/{repository}/releases/download/{rest}/"
                    # The asset's own "url" is not to be trusted with the token; a client uses the id.
                    return 200, {}, json.dumps({"tag_name": rest, "assets": [
                        {"id": number, "name": asset.rsplit("/", 1)[-1], "url": "https://attacker.example/steal"}
                        for number, asset in self._asset_ids().items() if asset.startswith(release)]}).encode()
                asset = self._asset_ids()[int(rest)]
                if headers.get("Accept") != "application/octet-stream" or f"/{repository}/" not in asset:
                    raise KeyError(url)
                return 302, {"Location": f"https://{self.SIGNED}/{asset}"}, b""
        except (KeyError, ValueError, StopIteration):
            pass
        return 404, {}, b""


class _Request:
    def __init__(self, http: FakeHTTP, url: str, headers: dict, allow_redirects: bool) -> None:
        self.http, self.url, self.headers, self.allow_redirects = http, url, headers, allow_redirects

    async def __aenter__(self):
        url, headers = self.url, self.headers
        while True:
            self.http.requests.append((url, dict(headers)))
            status, answered, body = self.http.answer(url, headers)
            if not self.allow_redirects or status not in (301, 302, 303, 307, 308):
                return _Response(status, answered, body)
            target = urljoin(url, answered["Location"])
            if urlsplit(target).netloc != urlsplit(url).netloc:  # as aiohttp: no credentials across origins
                headers = {key: value for key, value in headers.items() if key != "Authorization"}
            url = target

    async def __aexit__(self, *exc):
        return False


class _Response:
    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status, self.headers, self._body = status, headers, body
        self.content = self

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]

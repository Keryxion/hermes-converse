"""Wake words: dictionaries read from a scripted GitHub, the verified model cache, and live
switches over the real authenticated route."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_converse import wake_words
from hermes_converse.conversation import ConversationHub
from hermes_converse.voice_tools import make_handlers, register_tools
from hermes_converse.wake_words import WakeWordError, WakeWords, wake_phrase, wake_word_repositories
from tests.support.fake_github import FakeGitHub, FakeHTTP, sha256
from tests.support.fake_hermes import FakeHermes
from tests.test_route import (API_KEY, DEVICE_NAME, VOICE_PROTOCOL, Harness, _FakeStreamer, _end,
                              _key_protocol, _patch_speech, _start_msg)

SHARED, HOUSE = "Keryxion/wakewords", "your-org/household-wakewords"
SOURCES = ((SHARED, "main"), (HOUSE, "v1"))
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def github():
    fake = FakeGitHub()
    fake.publish(SHARED, {"Hey Sakura": {"onnx": b"sakura-onnx", "tflite": b"sakura-tflite"},
                          "Hey Arya": {"onnx": b"arya-onnx", "tflite": b"arya-tflite"}}, commit="a")
    fake.publish(HOUSE, {"Hey Arya": {"onnx": b"house-arya-onnx"}, "Computer": {"tflite": b"computer-tflite"}},
                 ref="v1", commit="b", version="2.0.0-beta.1")
    return fake


@pytest.fixture
def words(github, tmp_path):
    return WakeWords(tmp_path, open_fetch=github.open)


def _source(words, phrase):
    return json.loads((words._folder(phrase) / "source.json").read_text())


# ── settings ──

def test_settings_default_to_hey_sakura_and_the_shared_dictionary():
    assert wake_phrase({}.get, "Den Echo") == "Hey Sakura"
    assert wake_word_repositories({}.get) == ((SHARED, "main"),)


def test_a_device_phrase_overrides_the_default_and_each_source_has_its_own_ref():
    get = {"wake_word": "Hey Penny", "device_wake_words": {"Den Echo": "Hey Arya", "Hall": ""},
           "wake_word_repositories": [{"repository": SHARED}, {"repository": HOUSE, "ref": "v1"},
                                      {"repository": "not a repository"}, "Keryxion/wakewords"]}.get
    assert wake_phrase(get, "Den Echo") == "Hey Arya"
    assert wake_phrase(get, "Hall") == wake_phrase(get, "Kitchen") == "Hey Penny"
    assert wake_word_repositories(get) == SOURCES
    assert wake_word_repositories({"wake_word_repositories": []}.get) == ()


# ── dictionaries ──

async def test_sources_combine_and_the_first_one_supplies_a_duplicate_phrase_whole(words):
    phrases, failed = await words.listing(SOURCES)
    assert failed == []
    assert phrases == [
        {"phrase": "Hey Sakura", "formats": ["micro_wake_word", "open_wake_word"],
         "description": f"Hey Sakura from {SHARED}"},
        {"phrase": "Hey Arya", "formats": ["micro_wake_word", "open_wake_word"],
         "description": f"Hey Arya from {SHARED}"},
        {"phrase": "Computer", "formats": ["micro_wake_word"], "description": f"Computer from {HOUSE}"}]
    # Reordered, the household entry wins and brings only its own format.
    phrases, _ = await words.listing(SOURCES[::-1])
    assert {"phrase": "Hey Arya", "formats": ["open_wake_word"],
            "description": f"Hey Arya from {HOUSE}"} in phrases


async def test_phrases_differing_by_case_or_spacing_are_separate_and_keep_their_own_models(github, tmp_path):
    github.publish(HOUSE, {"hey  arya": {"onnx": b"house-arya-onnx", "tflite": b"house-arya-tflite"}},
                   ref="v1", commit="d")
    words = WakeWords(tmp_path, open_fetch=github.open)
    phrases, _ = await words.listing(SOURCES)
    assert [item["phrase"] for item in phrases] == ["Hey Sakura", "Hey Arya", "hey  arya"]
    with pytest.raises(WakeWordError, match="unknown_phrase"):
        await words.select("hey arya", SOURCES)  # listed under neither spelling
    shared = await words.select("Hey Arya", SOURCES)
    house = await words.select("hey  arya", SOURCES)
    # The first keeps the plain folder; the second gets one of its own, not the first one's.
    assert shared["open_wake_word"]["url"] == "/v1/audio/wake/hey_arya/hey_arya.onnx"
    folder = house["open_wake_word"]["url"].split("/")[-2]
    assert folder.startswith("hey_arya.") and folder != "hey_arya"
    assert shared["open_wake_word"]["sha256"] == sha256(b"arya-onnx")
    assert house["open_wake_word"]["sha256"] == sha256(b"house-arya-onnx")
    assert (_source(words, "Hey Arya")["phrase"], _source(words, "Hey Arya")["repository"]) == ("Hey Arya", SHARED)
    assert (_source(words, "hey  arya")["phrase"], _source(words, "hey  arya")["repository"]) == ("hey  arya", HOUSE)

    def served(models):
        files = {}
        for model in models.values():
            _, _, _, _, name, filename = model["url"].split("/")
            files[filename] = words.file(name, filename).read_bytes()
            if filename == "manifest.json":
                files["model"] = words.file(name, json.loads(files[filename])["model"]).read_bytes()
        return files

    assert served(shared)["hey_arya.onnx"] == b"arya-onnx" and served(shared)["model"] == b"arya-tflite"
    assert served(house)["hey_arya.onnx"] == b"house-arya-onnx" and served(house)["model"] == b"house-arya-tflite"
    # After a restart, and with GitHub gone, each phrase is still itself.
    github.offline = True
    words = WakeWords(tmp_path, open_fetch=github.open)
    assert words.models("Hey Arya") == shared and words.models("hey  arya") == house
    assert await words.select("hey  arya", SOURCES) == house and await words.select("Hey Arya", SOURCES) == shared
    assert words.models("hey arya") == {} and words.models("HEY ARYA") == {}
    # Selecting one again, changed, leaves the other alone.
    github.offline = False
    github.publish(HOUSE, {"hey  arya": {"onnx": b"house-arya-onnx-v2"}}, ref="v1", commit="e")
    again = await words.select("hey  arya", SOURCES)
    assert again["open_wake_word"]["url"] == house["open_wake_word"]["url"]
    assert again["open_wake_word"]["sha256"] == sha256(b"house-arya-onnx-v2")
    assert words.models("Hey Arya") == shared


async def test_whichever_colliding_phrase_is_cached_first_keeps_the_plain_folder(github, tmp_path):
    github.publish(HOUSE, {"hey  arya": {"onnx": b"house-arya-onnx"}}, ref="v1", commit="d")
    words = WakeWords(tmp_path, open_fetch=github.open)
    house = await words.select("hey  arya", SOURCES)
    shared = await words.select("Hey Arya", SOURCES)
    assert house["open_wake_word"] == {"url": "/v1/audio/wake/hey_arya/hey_arya.onnx",
                                       "sha256": sha256(b"house-arya-onnx")}
    assert shared["open_wake_word"]["url"] != house["open_wake_word"]["url"]
    assert shared["open_wake_word"]["sha256"] == sha256(b"arya-onnx")
    assert words.models("hey  arya") == house and words.models("Hey Arya") == shared


async def test_each_source_is_read_at_its_own_commit(words, github):
    await words.select("Computer", SOURCES)
    await words.select("Hey Arya", SOURCES)
    assert _source(words, "Computer")["commit"] == "b" * 40 and _source(words, "Computer")["repository"] == HOUSE
    assert _source(words, "Computer")["version"] == "2.0.0-beta.1"
    arya = _source(words, "Hey Arya")
    assert (arya["repository"], arya["commit"], arya["version"]) == (SHARED, "a" * 40, "1.0.0")
    assert arya["models"]["open_wake_word"]["sha256"] == sha256(b"arya-onnx")
    assert arya["models"]["micro_wake_word"]["files"][0]["sha256"] == sha256(b"arya-tflite")
    reads = [url for url in github.calls if "raw.githubusercontent.com" in url]
    assert reads and all(f"/{'a' * 40 if SHARED in url else 'b' * 40}/" in url for url in reads)


async def test_an_unchanged_commit_is_not_read_again(words, github):
    await words.listing(SOURCES)
    github.calls.clear()
    await words.listing(SOURCES)
    assert github.calls == [f"https://github.com/{repository}.git/info/refs?service=git-upload-pack"
                            for repository in (SHARED, HOUSE)]


async def test_a_ref_is_a_branch_a_tag_or_a_commit(words, github):
    def line(text):
        return f"{len(text) + 5:04x}{text}\n".encode()

    advertisement = (b"001e# service=git-upload-pack\n0000" + line("1" * 40 + " HEAD\0multi_ack symref=HEAD:refs/heads/main")
                     + line("1" * 40 + " refs/heads/main") + line("2" * 40 + " refs/tags/light")
                     + line("3" * 40 + " refs/tags/v1") + line("4" * 40 + " refs/tags/v1^{}") + b"0000")
    assert wake_words._commit(advertisement, "main") == "1" * 40
    assert wake_words._commit(advertisement, "light") == "2" * 40
    assert wake_words._commit(advertisement, "v1") == "4" * 40  # the commit an annotated tag points at
    with pytest.raises(WakeWordError, match="unknown_ref"):
        wake_words._commit(advertisement, "missing")
    # A full commit needs no lookup and reads the dictionary as it was then.
    github.publish(SHARED, {"Hey Penny": {"onnx": b"penny-onnx"}}, commit="c")
    github.calls.clear()
    phrases, failed = await words.listing(((SHARED, "a" * 40),))
    assert failed == [] and [item["phrase"] for item in phrases] == ["Hey Sakura", "Hey Arya"]
    assert not any("info/refs" in url for url in github.calls)


async def test_a_failed_source_keeps_its_last_good_entries_and_the_others_still_load(words, github):
    await words.listing(SOURCES)
    github.down.add(HOUSE)
    github.publish(SHARED, {"Hey Penny": {"onnx": b"penny-onnx"}}, commit="c")
    phrases, failed = await words.listing(SOURCES)
    assert failed == [HOUSE]
    assert [item["phrase"] for item in phrases] == ["Hey Penny", "Hey Arya", "Computer"]


async def test_a_source_never_reached_is_reported_and_does_not_block_the_rest(words, github):
    github.down.add(SHARED)
    phrases, failed = await words.listing(SOURCES)
    assert failed == [SHARED]
    assert [item["phrase"] for item in phrases] == ["Hey Arya", "Computer"]


async def test_an_unsupported_index_version_is_rejected_and_the_last_good_one_kept(words, github):
    await words.listing(((SHARED, "main"),))
    github.publish(SHARED, {"Hey Penny": {"onnx": b"penny-onnx"}}, commit="c", index_version=2)
    phrases, failed = await words.listing(((SHARED, "main"),))
    assert failed == [SHARED]
    assert [item["phrase"] for item in phrases] == ["Hey Sakura", "Hey Arya"]


async def test_an_entry_without_trained_models_is_not_offered(words, github):
    github.publish(HOUSE, {"Computer": {"tflite": b"computer-tflite"}, "Text Only": {}}, ref="v1", commit="d")
    phrases, failed = await words.listing(((HOUSE, "v1"),))
    assert failed == [] and [item["phrase"] for item in phrases] == ["Computer"]
    with pytest.raises(WakeWordError, match="unknown_phrase"):
        await words.select("Text Only", ((HOUSE, "v1"),))


# ── the model cache ──

async def test_selecting_a_phrase_downloads_verifies_and_caches_its_models(words):
    models = await words.select("Hey Arya", SOURCES)
    folder = words._root / "wake" / "hey_arya"
    manifest = json.loads((folder / "manifest.json").read_text())
    # The model is served under its content hash and the manifest points at that file.
    assert manifest["model"] == sha256(b"arya-tflite") + ".tflite"
    assert (folder / manifest["model"]).read_bytes() == b"arya-tflite"
    assert (folder / "hey_arya.onnx").read_bytes() == b"arya-onnx"
    assert models == {
        "micro_wake_word": {"url": "/v1/audio/wake/hey_arya/manifest.json",
                            "sha256": sha256((folder / "manifest.json").read_bytes())},
        "open_wake_word": {"url": "/v1/audio/wake/hey_arya/hey_arya.onnx", "sha256": sha256(b"arya-onnx")}}
    assert words.models("Hey Arya") == models and words.models("Hey Penny") == {}


async def test_a_changed_model_changes_the_advertised_manifest_hash(words, github):
    before = await words.select("Hey Arya", SOURCES)
    github.publish(SHARED, {"Hey Arya": {"onnx": b"arya-onnx", "tflite": b"arya-tflite-retrained"}}, commit="c")
    after = await words.select("Hey Arya", SOURCES)
    assert after["micro_wake_word"]["sha256"] != before["micro_wake_word"]["sha256"]
    assert after["open_wake_word"] == before["open_wake_word"]
    folder = words._root / "wake" / "hey_arya"
    assert sorted(path.name for path in folder.glob("*.tflite")) == [sha256(b"arya-tflite-retrained") + ".tflite"]


async def test_refreshing_or_reordering_dictionaries_does_not_replace_a_cached_model(words, github):
    installed = await words.select("Hey Arya", SOURCES)
    github.publish(SHARED, {"Hey Arya": {"onnx": b"arya-onnx-v2"}}, commit="c")
    await words.listing(SOURCES)
    await words.listing(SOURCES[::-1])
    assert words.models("Hey Arya") == installed and _source(words, "Hey Arya")["commit"] == "a" * 40
    # The next selection takes the entry then on offer, from the source now first.
    models = await words.select("Hey Arya", SOURCES[::-1])
    assert models == {"open_wake_word": {"url": "/v1/audio/wake/hey_arya/hey_arya.onnx",
                                         "sha256": sha256(b"house-arya-onnx")}}
    assert _source(words, "Hey Arya")["repository"] == HOUSE


@pytest.mark.parametrize("corrupt", ["onnx", "tflite", "manifest"])
async def test_a_bad_hash_prevents_the_selection_and_keeps_the_cached_model(words, github, corrupt):
    installed = await words.select("Hey Arya", SOURCES)
    github.publish(SHARED, {"Hey Arya": {"onnx": b"arya-onnx-v2", "tflite": b"arya-tflite-v2"}}, commit="c")
    suffix = {"onnx": "openwakeword.onnx", "tflite": "microwakeword.tflite", "manifest": "microwakeword.json"}[corrupt]
    (url,) = [url for url in github.assets if "/ccccccc/" in url and url.endswith(suffix)]
    github.assets[url] += b" tampered"
    with pytest.raises(WakeWordError, match="hash_mismatch"):
        await words.select("Hey Arya", SOURCES)
    assert words.models("Hey Arya") == installed and _source(words, "Hey Arya")["commit"] == "a" * 40


async def test_a_missing_model_file_fails_the_selection(words, github):
    (url,) = [url for url in github.assets if url.endswith("hey_arya-openwakeword.onnx") and SHARED in url]
    del github.assets[url]
    with pytest.raises(WakeWordError, match="download_failed"):
        await words.select("Hey Arya", SOURCES)
    assert words.models("Hey Arya") == {}


async def test_cached_models_stay_usable_when_github_is_unreachable(words, github):
    installed = await words.select("Hey Arya", SOURCES)
    github.offline = True
    github.calls.clear()
    assert await words.select("Hey Arya", SOURCES) == installed
    assert not any("releases/download" in url for url in github.calls)  # nothing downloaded again
    # With no dictionaries configured the cache is all there is.
    assert await words.select("Hey Arya", ()) == installed
    with pytest.raises(WakeWordError, match="download_failed"):
        await words.select("Computer", SOURCES)  # listed, but its models were never fetched
    assert words.models("Computer") == {}


async def test_only_files_inside_a_phrase_folder_are_served(words):
    await words.select("Hey Arya", SOURCES)
    (words._root / "secret.txt").write_text("outside")
    (words._root / "wake" / "hey_arya" / "link.onnx").symlink_to(words._root / "secret.txt")
    assert words.file("hey_arya", "hey_arya.onnx").read_bytes() == b"arya-onnx"
    for name, filename in [("hey_arya", "../../secret.txt"), ("hey_arya", ".."), ("..", "secret.txt"),
                           ("hey_arya", "link.onnx"), ("hey_arya/..", "hey_arya"), ("Hey Arya", "manifest.json"),
                           ("hey_arya", "missing.onnx"), ("hey_arya", ""), ("", "manifest.json"),
                           ("hey_arya", "/etc/passwd"), (".hey_arya.new", "hey_arya.onnx"),
                           ("hey_arya.", "hey_arya.onnx"), ("hey_arya..", "wake"), ("hey_arya.0123456789ab", ".."),
                           ("hey_arya.0123456789ab/..", "hey_arya"), ("../wake-dictionaries", "x.json")]:
        assert words.file(name, filename) is None, (name, filename)


# ── GitHub over HTTP: anonymous first, the profile's token only for what refuses ──

GITHUB_HOSTS = {"github.com", "raw.githubusercontent.com", "api.github.com"}


@pytest.fixture
def http(github, monkeypatch):
    fake = FakeHTTP(github)
    monkeypatch.setattr(wake_words, "ClientSession", fake)
    monkeypatch.setenv("GH_TOKEN", fake.token)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return fake


def _with_token(http):
    return [url for url, headers in http.requests if "Authorization" in headers]


async def test_public_dictionaries_and_release_assets_never_see_the_token(http, tmp_path):
    words = WakeWords(tmp_path)
    models = await words.select("Hey Arya", SOURCES)
    assert models["open_wake_word"]["sha256"] == sha256(b"arya-onnx")
    assert (words._root / "wake" / "hey_arya" / (sha256(b"arya-tflite") + ".tflite")).read_bytes() == b"arya-tflite"
    assert _with_token(http) == [] and http.sent_to("api.github.com") == []
    assert len(http.sent_to(FakeHTTP.SIGNED)) == 3  # each download followed its redirect


@pytest.mark.parametrize("variable", ["GH_TOKEN", "GITHUB_TOKEN"])
async def test_a_private_dictionary_and_its_release_assets_use_the_profile_token(
        http, github, tmp_path, monkeypatch, caplog, variable):
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv(variable, http.token)
    github.private.add(HOUSE)
    caplog.set_level("DEBUG")
    words = WakeWords(tmp_path)
    phrases, failed = await words.listing(SOURCES)
    assert failed == [] and "Computer" in [item["phrase"] for item in phrases]
    models = await words.select("Computer", SOURCES)
    folder = words._root / "wake" / "computer"
    assert (folder / (sha256(b"computer-tflite") + ".tflite")).read_bytes() == b"computer-tflite"
    assert models == {"micro_wake_word": {"url": "/v1/audio/wake/computer/manifest.json",
                                          "sha256": sha256((folder / "manifest.json").read_bytes())}}
    # The token went to GitHub's own hosts over HTTPS, and only for the private repository.
    assert _with_token(http) and all(
        url.startswith("https://") and wake_words.urlsplit(url).hostname in GITHUB_HOSTS and HOUSE in url
        for url in _with_token(http))
    # The release was found by tag and each asset downloaded by its id from the API.
    api = [url for url, _ in http.requests if "api.github.com" in url]
    assert f"https://api.github.com/repos/{HOUSE}/releases/tags/bbbbbbb" in api
    assert any(f"/repos/{HOUSE}/releases/assets/" in url for url in api)
    assert all(headers["Authorization"] == f"Bearer {http.token}" for headers in http.sent_to("api.github.com"))
    # The signed download the API redirects to is fetched without credentials, and the
    # asset "url" in the release metadata is never followed.
    signed = http.sent_to(FakeHTTP.SIGNED)
    assert len(signed) == 2 and not any("Authorization" in headers for headers in signed)
    assert http.sent_to("attacker.example") == []
    assert http.token not in caplog.text


async def test_a_private_repository_without_a_token_fails_alone(http, github, tmp_path, monkeypatch):
    monkeypatch.delenv("GH_TOKEN")
    github.private.add(HOUSE)
    words = WakeWords(tmp_path)
    phrases, failed = await words.listing(SOURCES)
    assert failed == [HOUSE] and [item["phrase"] for item in phrases] == ["Hey Sakura", "Hey Arya"]
    assert _with_token(http) == [] and http.sent_to("api.github.com") == []


async def test_a_private_release_asset_is_hash_verified_like_any_other(http, github, tmp_path):
    github.private.add(HOUSE)
    (url,) = [url for url in github.assets if HOUSE in url and url.endswith("computer-microwakeword.tflite")]
    github.assets[url] += b" tampered"
    words = WakeWords(tmp_path)
    with pytest.raises(WakeWordError, match="hash_mismatch"):
        await words.select("Computer", SOURCES)
    assert words.models("Computer") == {}
    del github.assets[url]  # no longer in the release at all
    with pytest.raises(WakeWordError, match="download_failed"):
        await words.select("Computer", SOURCES)
    assert words.models("Computer") == {}


async def test_the_token_is_never_sent_to_another_host_or_over_plain_http(http):
    async with wake_words.github_fetch() as fetch:
        for url in ("https://models.example.com/hey_arya.onnx",
                    "https://github.com.attacker.example/o/r/releases/download/v1/hey_arya.onnx",
                    "https://api.github.com.attacker.example/repos/o/r/releases/tags/v1",
                    "https://raw.githubusercontent.com@attacker.example/o/r/main/index.json",
                    "https://attacker.example/github.com/o/r/releases/download/v1/hey_arya.onnx",
                    "http://github.com/o/r/releases/download/v1/hey_arya.onnx",
                    "http://raw.githubusercontent.com/o/r/main/index.json",
                    "https://github.com:8443/o/r/releases/download/v1/hey_arya.onnx"):
            with pytest.raises(WakeWordError, match="download_failed"):
                await fetch(url)
    assert _with_token(http) == []


async def test_a_request_carrying_the_token_is_not_redirected_with_it(http):
    def refuses_anonymous(location):
        return lambda headers: (302, {"Location": location}, b"") if "Authorization" in headers else (404, {}, b"")

    away = "https://raw.githubusercontent.com/o/r/" + "c" * 40 + "/index.json"
    within = "https://raw.githubusercontent.com/o/r/" + "d" * 40 + "/index.json"
    http.scripted[away] = refuses_anonymous("https://mirror.attacker.example/index.json")
    http.scripted[within] = refuses_anonymous("/o/elsewhere/index.json")  # the same host
    http.scripted["https://mirror.attacker.example/index.json"] = (200, {}, b"moved away")
    http.scripted["https://raw.githubusercontent.com/o/elsewhere/index.json"] = (200, {}, b"moved within")
    async with wake_words.github_fetch() as fetch:
        assert await fetch(away) == b"moved away"
        assert await fetch(within) == b"moved within"
    assert _with_token(http) == [away, within]  # neither redirect target
    assert http.sent_to("mirror.attacker.example") == [{}]


# ── the route ──

@pytest.fixture
def device_env(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": ""}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    return env


async def _connect(client, device_name=DEVICE_NAME):
    ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    await ws.send_str(_start_msg(device_name=device_name))
    ready = await ws.receive_json()
    assert ready["type"] == "ready"
    return ws, ready


@pytest.fixture
async def connected(monkeypatch, device_env, words, github):
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    settings = {"wake_word_repositories": [{"repository": SHARED}, {"repository": HOUSE, "ref": "v1"}]}
    harness = Harness(FakeHermes(API_KEY), settings, wake_words=words)
    async with harness as client:
        ws, ready = await _connect(client)
        (device_env["HERMES_SESSION_CHAT_ID"],) = harness.route.hub._attachments
        call = make_handlers(harness.route.hub)["set_wake_word"]
        try:
            yield SimpleNamespace(ws=ws, ready=ready, client=client, call=call, settings=settings,
                                  hub=harness.route.hub, words=words, github=github, env=device_env)
        finally:
            await ws.close()


async def _switch(connected, phrase, ws=None):
    """Start the tool's switch and return it with the command the device received."""
    task = asyncio.create_task(connected.call({"phrase": phrase}))
    command = await asyncio.wait_for((ws or connected.ws).receive_json(), 2)
    assert command["type"] == "set_wake_word"
    return task, command


async def _result(task):
    return json.loads(await asyncio.wait_for(task, 2))


async def test_a_fresh_device_is_told_hey_sakura_with_nothing_to_download(connected):
    assert connected.ready["wake_word"] == {"phrase": "Hey Sakura", "models": {}}
    assert connected.github.calls == []


async def test_ready_offers_cached_models_and_the_route_serves_them_for_the_api_key(connected):
    await connected.words.select("Hey Arya", SOURCES)
    connected.settings["device_wake_words"] = {"Den Echo": "Hey Arya"}
    ws, ready = await _connect(connected.client, "Den Echo")
    assert ready["wake_word"]["phrase"] == "Hey Arya"
    models = ready["wake_word"]["models"]
    assert set(models) == {"micro_wake_word", "open_wake_word"}
    for model in models.values():
        response = await connected.client.get(model["url"], headers=AUTH)
        assert response.status == 200 and sha256(await response.read()) == model["sha256"]
    # The manifest's model file sits beside it and is fetched the same way.
    manifest = await (await connected.client.get(models["micro_wake_word"]["url"], headers=AUTH)).json(content_type=None)
    response = await connected.client.get("/v1/audio/wake/hey_arya/" + manifest["model"], headers=AUTH)
    assert await response.read() == b"arya-tflite"
    await _end(ws)


async def test_a_phrase_with_a_folder_of_its_own_is_offered_and_served_as_itself(connected):
    connected.github.publish(HOUSE, {"hey  arya": {"onnx": b"house-arya-onnx"}}, ref="v1", commit="d")
    await connected.words.select("Hey Arya", SOURCES)
    await connected.words.select("hey  arya", SOURCES)
    connected.settings["device_wake_words"] = {"Den Echo": "hey  arya", "Hall": "Hey Arya"}
    den, den_ready = await _connect(connected.client, "Den Echo")
    hall, hall_ready = await _connect(connected.client, "Hall")
    assert den_ready["wake_word"]["phrase"] == "hey  arya" and hall_ready["wake_word"]["phrase"] == "Hey Arya"
    for ready, onnx in ((den_ready, b"house-arya-onnx"), (hall_ready, b"arya-onnx")):
        model = ready["wake_word"]["models"]["open_wake_word"]
        response = await connected.client.get(model["url"], headers=AUTH)
        assert response.status == 200 and await response.read() == onnx and sha256(onnx) == model["sha256"]
    await _end(den)
    await _end(hall)


async def test_model_files_are_refused_without_the_api_key_or_outside_the_folder(connected):
    await connected.words.select("Hey Arya", SOURCES)
    url = "/v1/audio/wake/hey_arya/hey_arya.onnx"
    assert (await connected.client.get(url)).status == 401
    assert (await connected.client.get(url, headers={"Authorization": "Bearer wrong"})).status == 401
    assert (await connected.client.get(url, headers={"Authorization": API_KEY})).status == 401
    (connected.words._root / "devices.db").write_text("not a model")
    for path in ("hey_arya/..%2F..%2Fdevices.db", "hey_arya/%2E%2E", "%2E%2E/devices.db", "..%2Fwake/hey_arya",
                 "hey_arya/missing.onnx", "hey_penny/hey_penny.onnx"):
        response = await connected.client.get("/v1/audio/wake/" + path, headers=AUTH)
        assert response.status == 404, path


async def test_the_tool_lists_the_combined_dictionary_and_the_current_phrase(connected):
    connected.github.down.add(HOUSE)
    assert await _result(asyncio.create_task(connected.call({}))) == {
        "wake_word": "Hey Sakura",
        "phrases": [{"phrase": "Hey Sakura", "formats": ["micro_wake_word", "open_wake_word"],
                     "description": f"Hey Sakura from {SHARED}"},
                    {"phrase": "Hey Arya", "formats": ["micro_wake_word", "open_wake_word"],
                     "description": f"Hey Arya from {SHARED}"}],
        "unavailable_dictionaries": [HOUSE]}
    assert connected.words.models("Hey Arya") == {}  # listing installs nothing


async def test_hermes_dispatches_the_tool_to_the_calling_device(connected):
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_tools(SimpleNamespace(register_tool=registry.register), connected.hub)
    listed = json.loads(await asyncio.to_thread(registry.dispatch, "set_wake_word", {}))
    assert listed["wake_word"] == "Hey Sakura" and len(listed["phrases"]) == 3
    task = asyncio.create_task(asyncio.to_thread(registry.dispatch, "set_wake_word", {"phrase": "Hey Arya"}))
    command = await asyncio.wait_for(connected.ws.receive_json(), 2)
    await connected.ws.send_json({"type": "wake_word_set", "phrase": command["phrase"], "status": "ok"})
    assert await _result(task) == {"result": "ok", "phrase": "Hey Arya"}


async def test_a_switch_is_confirmed_only_after_the_device_reports_ok(connected):
    task, command = await _switch(connected, "Hey Arya")
    assert command == {"type": "set_wake_word", "phrase": "Hey Arya", "models": connected.words.models("Hey Arya")}
    assert set(command["models"]) == {"micro_wake_word", "open_wake_word"}
    await asyncio.sleep(.05)
    assert not task.done()  # sent is only requested
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"})
    assert await _result(task) == {"result": "ok", "phrase": "Hey Arya"}
    assert connected.settings["device_wake_words"] == {DEVICE_NAME: "Hey Arya"}
    # The same socket carried the switch; a later connection hears it in ready.
    await connected.ws.close()
    ws, ready = await _connect(connected.client)
    assert ready["wake_word"] == {"phrase": "Hey Arya", "models": command["models"]}
    await _end(ws)


@pytest.mark.parametrize("error", ["model_unavailable", "download_failed", "hash_mismatch", "load_failed"])
async def test_a_device_error_fails_the_switch_and_leaves_the_setting_as_it_was(connected, error, monkeypatch):
    monkeypatch.setattr(wake_words, "POLL_SECONDS", .05)
    connected.settings["device_wake_words"] = {"Hall": "Hey Penny"}
    ws, _ = await _connect(connected.client, "Den Echo")
    connected.env["HERMES_SESSION_CHAT_ID"] = next(
        key for key, item in connected.hub._attachments.items() if item.session.device_name == "Den Echo")
    task, _ = await _switch(connected, "Hey Arya", ws)
    await ws.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "error", "error": error})
    assert await _result(task) == {"result": "failed", "error": error}
    assert connected.settings["device_wake_words"] == {"Hall": "Hey Penny"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.receive_json(), .3)  # the refused phrase is not sent again
    await _end(ws)


async def test_a_missing_or_mismatched_result_does_not_confirm_a_switch(connected, monkeypatch):
    monkeypatch.setattr(wake_words, "SWITCH_TIMEOUT", .2)
    task, _ = await _switch(connected, "Hey Arya")
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Hey Sakura", "status": "ok"})
    await connected.ws.send_json({"type": "wake_word_set", "status": "ok"})
    assert await _result(task) == {"result": "failed", "error": "timeout"}
    assert "device_wake_words" not in connected.settings
    # A late answer to the expired command does not answer the next one.
    monkeypatch.setattr(wake_words, "SWITCH_TIMEOUT", 5)
    task, _ = await _switch(connected, "Computer")
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"})
    await asyncio.sleep(.05)
    assert not task.done()
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Computer", "status": "ok"})
    assert await _result(task) == {"result": "ok", "phrase": "Computer"}


async def test_a_disconnect_does_not_confirm_a_switch(connected):
    task, _ = await _switch(connected, "Hey Arya")
    await connected.ws.close()
    assert await _result(task) == {"result": "failed", "error": "disconnected"}
    assert "device_wake_words" not in connected.settings
    ws, ready = await _connect(connected.client)
    assert ready["wake_word"]["phrase"] == "Hey Sakura"  # the previous phrase, as the tool reported
    await _end(ws)


async def test_switches_on_one_device_run_one_at_a_time(connected):
    first, command = await _switch(connected, "Hey Arya")
    second = asyncio.create_task(connected.call({"phrase": "Computer"}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(connected.ws.receive_json(), .2)  # the second waits for the first
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"})
    assert await _result(first) == {"result": "ok", "phrase": "Hey Arya"}
    command = await asyncio.wait_for(connected.ws.receive_json(), 2)
    assert command["type"] == "set_wake_word" and command["phrase"] == "Computer"
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Computer", "status": "ok"})
    assert await _result(second) == {"result": "ok", "phrase": "Computer"}
    assert connected.settings["device_wake_words"] == {DEVICE_NAME: "Computer"}


async def test_the_tool_changes_only_the_device_that_called_it(connected):
    other, ready = await _connect(connected.client, "Den Echo")
    task, _ = await _switch(connected, "Hey Arya")
    await connected.ws.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"})
    assert (await _result(task))["result"] == "ok"
    assert connected.settings["device_wake_words"] == {DEVICE_NAME: "Hey Arya"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other.receive_json(), .2)
    await _end(other)


@pytest.mark.parametrize("phrase,publish,error", [
    ("Hey Nobody", None, "unknown_phrase"),
    ("Hey Arya", {"onnx": b"arya-onnx-v2", "onnx_sha256": "0" * 64}, "hash_mismatch")])
async def test_a_phrase_without_verified_models_changes_nothing(connected, phrase, publish, error):
    if publish:
        connected.github.publish(SHARED, {phrase: publish}, commit="c")
    assert await _result(asyncio.create_task(connected.call({"phrase": phrase}))) == {
        "result": "failed", "error": error}
    assert "device_wake_words" not in connected.settings
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(connected.ws.receive_json(), .2)  # no command was sent


async def test_a_changed_setting_reaches_a_connected_device_without_a_reconnect(connected, monkeypatch):
    monkeypatch.setattr(wake_words, "POLL_SECONDS", .05)
    den, ready = await _connect(connected.client, "Den Echo")
    hall, _ = await _connect(connected.client, "Hall")
    assert ready["wake_word"]["phrase"] == "Hey Sakura"
    connected.settings["device_wake_words"] = {"Den Echo": "Hey Arya"}
    command = await asyncio.wait_for(den.receive_json(), 2)
    assert command == {"type": "set_wake_word", "phrase": "Hey Arya", "models": connected.words.models("Hey Arya")}
    assert command["models"]
    await den.send_json({"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(den.receive_json(), .3)  # sent once
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(hall.receive_json(), .1)  # and only to that device
    # The default changing reaches a device that has no phrase of its own.
    connected.settings["wake_word"] = "Computer"
    command = await asyncio.wait_for(hall.receive_json(), 2)
    assert command["phrase"] == "Computer" and set(command["models"]) == {"micro_wake_word"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(den.receive_json(), .2)
    await _end(den)
    await _end(hall)


async def test_a_phrase_configured_while_the_device_was_away_is_fetched_when_it_connects(connected):
    connected.settings["device_wake_words"] = {"Den Echo": "Hey Arya"}
    ws, ready = await _connect(connected.client, "Den Echo")
    assert ready["wake_word"] == {"phrase": "Hey Arya", "models": {}}  # ready never waits on GitHub
    command = await asyncio.wait_for(ws.receive_json(), 2)
    assert command["type"] == "set_wake_word" and command["phrase"] == "Hey Arya" and command["models"]
    await _end(ws)


async def test_the_tool_needs_an_attached_voice_device(device_env):
    call = make_handlers(ConversationHub())["set_wake_word"]
    assert json.loads(await call({"phrase": "Hey Arya"})) == {"result": "not_attached"}
    assert json.loads(await call({})) == {"result": "not_attached"}
    device_env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(await call({"phrase": "Hey Arya"})) == {"result": "not_a_device"}

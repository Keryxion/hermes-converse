"""The Runs API client against a scripted fake server: deltas are spoken once, interim and
replayed events never are, blocking prompts are answered, and a stop (or an abandoned turn)
always reaches the server."""

import asyncio

import pytest
from aiohttp import ClientSession, ClientTimeout
from aiohttp.test_utils import TestClient, TestServer

from hermes_converse.agent_client import RunsClient
from hermes_converse.auth import offered_key_protocol, verify_api_key

from tests.support.fake_api import FakeRunsAPI

API_KEY = "-".join(("fixture", "runs", "api", "key", "0123456789"))


async def _client_for(server, api_key=API_KEY, **kw):
    http = ClientSession(timeout=ClientTimeout(total=None, connect=5, sock_read=30))
    return RunsClient(str(server.make_url("")), api_key, http=http, **kw), http


@pytest.mark.asyncio
async def test_run_turn_streams_deltas_and_returns_the_final_output():
    fake = FakeRunsAPI(API_KEY, deltas=("Turn it ", "on."), interim="Turn it on.")
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server)
        async with http:
            spoken = []
            result = await client.run_turn(text="turn it on", session_id="voice_1", on_delta=spoken.append,
                                           instructions="be brief", model="m", provider="p",
                                           idempotency_key="converse-voice_1-abc")
    assert spoken == ["Turn it ", "on."]  # the interim event was never spoken
    assert result.reply == "Turn it on." and result.error is None and result.status == "completed"
    run = fake.runs[result.run_id]
    assert run["body"] == {"input": "turn it on", "session_id": "voice_1", "instructions": "be brief",
                           "model": "m", "provider": "p"}
    assert run["idempotency_key"] == "converse-voice_1-abc"
    assert client.current_run_id is None


@pytest.mark.asyncio
async def test_reconnect_resumes_after_the_last_seen_event_without_replaying():
    fake = FakeRunsAPI(API_KEY, deltas=("one ", "two ", "three."), drop_after=2)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server)
        async with http:
            spoken = []
            result = await client.run_turn(text="count", session_id="s", on_delta=spoken.append)
    assert spoken == ["one ", "two ", "three."]
    assert result.status == "completed"
    assert [last for _, last in fake.subscriptions] == [None, "1"]  # resumed with Last-Event-ID


@pytest.mark.asyncio
async def test_stop_interrupts_a_hung_run_and_settles():
    fake = FakeRunsAPI(API_KEY, deltas=("Sure, ",), hang=True)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server, settle_timeout=5.0)
        async with http:
            spoken = []
            turn = asyncio.ensure_future(client.run_turn(text="x", session_id="s", on_delta=spoken.append))
            for _ in range(100):
                if spoken:
                    break
                await asyncio.sleep(0.02)
            assert spoken == ["Sure, "]
            await client.stop()
            result = await asyncio.wait_for(turn, 5)
    assert fake.stop_calls == [result.run_id]
    assert result.status == "cancelled" and result.error is None  # a requested stop is not an error


@pytest.mark.asyncio
async def test_abandoned_turn_posts_stop_before_propagating_the_cancel():
    fake = FakeRunsAPI(API_KEY, deltas=("Sure, ",), hang=True)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server)
        async with http:
            spoken = []
            turn = asyncio.ensure_future(client.run_turn(text="x", session_id="s", on_delta=spoken.append))
            for _ in range(100):
                if spoken:
                    break
                await asyncio.sleep(0.02)
            turn.cancel()  # the driver's deadline / the socket closed
            with pytest.raises(asyncio.CancelledError):
                await turn
            assert fake.stop_calls == [next(iter(fake.runs))]


@pytest.mark.asyncio
async def test_blocking_prompts_are_answered_with_the_configured_choice():
    fake = FakeRunsAPI(API_KEY, deltas=("ok.",), pre_events=(
        ("approval.request", {"request_id": "ap-1", "execution_generation": 3, "command": "rm -rf build"}),
        ("clarify.request", {"request_id": "cl-1", "execution_generation": 3})))
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server, approval_choice="once")
        async with http:
            result = await client.run_turn(text="x", session_id="s", on_delta=lambda d: None)
    assert result.status == "completed"
    assert fake.approvals == [{"request_id": "ap-1", "execution_generation": 3, "choice": "once"}]
    assert fake.clarifies == [{"request_id": "cl-1", "execution_generation": 3, "answer": {}}]


@pytest.mark.asyncio
async def test_refused_run_is_reported_not_raised():
    fake = FakeRunsAPI(API_KEY)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server, api_key="wrong-key-0123456789")
        async with http:
            result = await client.run_turn(text="x", session_id="s", on_delta=lambda d: None)
    assert result.status == "failed" and "refused the turn (401)" in result.error
    assert fake.runs == {}


@pytest.mark.asyncio
async def test_verify_api_key_asks_the_server():
    fake = FakeRunsAPI(API_KEY)
    async with TestClient(TestServer(fake.app())) as tc:
        async with ClientSession() as http:
            base = str(tc.server.make_url(""))
            assert await verify_api_key(base, API_KEY, http=http) is True
            assert await verify_api_key(base, "nope", http=http) is False
            assert await verify_api_key(base, "", http=http) is False
            assert await verify_api_key("http://127.0.0.1:1", API_KEY, http=http) is False  # unreachable: closed


def test_offered_key_protocol_parsing():
    assert offered_key_protocol("") is None
    assert offered_key_protocol("hermes-voice-v1") is None
    assert offered_key_protocol("hermes-voice-v1, hermes-key.abc") == "abc"
    assert offered_key_protocol("hermes-key.a, hermes-key.b") == ""  # ambiguous: reject
    assert offered_key_protocol("hermes-key.") == ""

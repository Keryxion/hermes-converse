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



@pytest.mark.asyncio
async def test_cancel_during_admission_waits_for_id_and_stops_accepted_run():
    fake = FakeRunsAPI(API_KEY, hang=True)
    accepted = asyncio.Event()
    release = asyncio.Event()
    create = fake.create_run

    async def delayed_response(request):
        response = await create(request)
        accepted.set()
        await release.wait()
        return response

    fake.create_run = delayed_response
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server)
        async with http:
            turn = asyncio.create_task(client.run_turn(text='go', session_id='s', on_delta=lambda _: None))
            await asyncio.wait_for(accepted.wait(), 2)
            turn.cancel()
            await asyncio.sleep(0)
            turn.cancel()  # socket close while barge-in cleanup is already waiting
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 2)
            run_id = next(iter(fake.runs))
            assert fake.stop_calls == [run_id]
            assert fake.runs[run_id]['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_next_request_waits_for_previous_run_to_settle():
    fake = FakeRunsAPI(API_KEY)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server, settle_timeout=5.0)
        async with http:
            fake.runs['old'] = {'status': 'running', 'output': '', 'stopped': asyncio.Event()}
            client.unsettled_run_ids.add('old')

            async def _settle_old():
                await asyncio.sleep(0.1)
                fake.runs['old']['status'] = 'cancelled'

            asyncio.ensure_future(_settle_old())
            result = await client.run_turn(text='and make it warm white', session_id='s', on_delta=lambda _: None)
            assert not result.error and len(fake.runs) == 2  # admitted only once 'old' actually settled
            assert client.unsettled_run_ids == set()


@pytest.mark.asyncio
async def test_a_run_that_never_settles_does_not_poison_future_turns():
    fake = FakeRunsAPI(API_KEY)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server, settle_timeout=0)
        async with http:
            client.unsettled_run_ids.add('stuck')  # never reaches a terminal status within settle_timeout
            result = await client.run_turn(text='and make it warm white', session_id='s', on_delta=lambda _: None)
            assert client.unsettled_run_ids == set()  # the stuck run is dropped, not left poisoning the client
            assert not result.error and len(fake.runs) == 1  # this turn still gets to run


@pytest.mark.asyncio
async def test_tool_count_survives_sse_reconnect_and_resets_for_next_turn():
    fake = FakeRunsAPI(API_KEY, pre_events=(
        ('tool.started', {'tool_call_id': 'a', 'tool': 'read_file'}),
        ('tool.completed', {'tool_call_id': 'a', 'tool': 'read_file'}),
        ('tool.started', {'tool_call_id': 'b', 'tool': 'read_file'}),
        ('tool.completed', {'tool_call_id': 'b', 'error': True}),
    ), drop_after=1)
    async with TestClient(TestServer(fake.app())) as tc:
        client, http = await _client_for(tc.server)
        async with http:
            result = await client.run_turn(text='go', session_id='s', on_delta=lambda _: None)
            assert result.error is None
            assert client.tool_calls == 2
            assert len(fake.subscriptions) >= 2
            fake.pre_events = ()
            fake.drop_after = None
            result = await client.run_turn(text='hello', session_id='s', on_delta=lambda _: None)
            assert result.error is None
            assert client.tool_calls == 0


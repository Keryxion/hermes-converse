"""Wake elections are advisory and consume only client-reported wake signals."""
import asyncio

import pytest

from hermes_converse.arbitrate import (
    Arbitrator, DeviceAnnounce, LocalBus, POLICY_FIRST_TO_ENDPOINT, WakeClaim,
)
from hermes_converse.route import _wake_response
from hermes_converse.settings import load_settings


async def _arbitrator(**kwargs):
    arb = Arbitrator(wake_window=0.02, **kwargs)
    await arb.register('d1')
    await arb.register('d2')
    return arb


async def _pair(arb, first=None, second=None):
    return await asyncio.gather(
        arb.claim_wake('d1', claim_id='client-1', wake_word='Hey Arya', **(first or {})),
        arb.claim_wake('d2', claim_id='client-2', wake_word='Hey Arya', **(second or {})),
    )


@pytest.mark.parametrize('first,second,winner', [
    ({'confidence': 0.6, 'volume': 1}, {'confidence': 0.9, 'volume': 0}, 'd2'),
    ({'confidence': 0.9, 'volume': 0.2}, {'confidence': 0.9, 'volume': 0.8}, 'd2'),
    ({'volume': 0.5, 'heard_at': 500}, {'volume': 0.5, 'heard_at': 200}, 'd2'),
    ({}, {}, 'd1'),
    ({'heard_at': 100}, {'confidence': 0}, 'd2'),
    ({'heard_at': 100}, {'volume': 0}, 'd2'),
    ({}, {'heard_at': 100}, 'd2'),
])
@pytest.mark.asyncio
async def test_rank_and_echo_client_ids(first, second, winner):
    results = await _pair(await _arbitrator(), first, second)
    assert sum(r[0] for r in results) == 1
    assert [r[1] for r in results] == [winner, winner]
    assert [r[2] for r in results] == ['client-1', 'client-2']


@pytest.mark.asyncio
async def test_exact_phrases_elect_independently():
    arb = await _arbitrator()
    results = await asyncio.gather(
        arb.claim_wake('d1', claim_id='1', wake_word='Arya'),
        arb.claim_wake('d2', claim_id='2', wake_word='Hey Arya'),
    )
    assert all(r[0] for r in results)


@pytest.mark.asyncio
async def test_later_activation_needs_no_release():
    arb = await _arbitrator()
    assert (await arb.claim_wake('d1', claim_id='1', wake_word='Arya'))[0]
    assert (await arb.claim_wake('d2', claim_id='2', wake_word='Arya'))[0]
    assert arb._wake_open == {}


@pytest.mark.asyncio
async def test_cancelled_claim_does_not_win():
    arb = await _arbitrator()
    first = asyncio.create_task(arb.claim_wake('d1', claim_id='1', wake_word='Arya', confidence=1))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await arb.claim_wake('d2', claim_id='2', wake_word='Arya', confidence=0))[0]


@pytest.mark.asyncio
async def test_disconnected_claim_does_not_win():
    arb = await _arbitrator()
    first = asyncio.create_task(arb.claim_wake('d1', claim_id='1', wake_word='Arya', confidence=1))
    await asyncio.sleep(0)
    await arb.unregister('d1')
    second = await arb.claim_wake('d2', claim_id='2', wake_word='Arya')
    assert second[0]
    assert not (await first)[0]


@pytest.mark.asyncio
async def test_multiple_claims_from_one_socket_still_choose_one_claim():
    arb = await _arbitrator()
    results = await asyncio.gather(*[
        arb.claim_wake('d1', claim_id=str(n), wake_word='Arya') for n in range(2)
    ])
    assert [r[0] for r in results] == [True, False]


@pytest.mark.asyncio
async def test_legacy_policy_grants_without_transcript_election():
    arb = await _arbitrator(policy=POLICY_FIRST_TO_ENDPOINT)
    assert all(r[0] for r in await _pair(arb))
    assert arb._wake_open == {}


@pytest.mark.asyncio
async def test_disabled_echoes_id_and_exact_phrase():
    reply = await _wake_response({'claim_id': 'from-client', 'wake_word': 'Hey Arya'},
                                 arbitrator=None, device_id='d1')
    assert reply == {'type': 'wake_grant', 'claim_id': 'from-client', 'wake_word': 'Hey Arya'}


@pytest.mark.parametrize('frame', [
    {}, {'claim_id': 3, 'wake_word': 'Arya'}, {'claim_id': '', 'wake_word': 'Arya'},
    {'claim_id': '1', 'wake_word': []}, {'claim_id': '1', 'wake_word': ' '},
])
@pytest.mark.asyncio
async def test_invalid_required_wake_fields(frame):
    assert (await _wake_response(frame, arbitrator=None, device_id='d1'))['error'] == 'bad_wake'


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 1.1, True, '0.9', {}, 10**400])
@pytest.mark.asyncio
async def test_invalid_optional_signals_rank_as_missing(bad):
    arb = await _arbitrator()
    results = await asyncio.gather(
        _wake_response({'claim_id': 'bad', 'wake_word': 'Arya', 'confidence': bad, 'volume': bad},
                       arbitrator=arb, device_id='d1'),
        _wake_response({'claim_id': 'good', 'wake_word': 'Arya', 'confidence': 0, 'volume': 0},
                       arbitrator=arb, device_id='d2'),
    )
    assert [r['type'] for r in results] == ['wake_deny', 'wake_grant']


@pytest.mark.asyncio
async def test_bus_fanout_unsubscribe_and_registration():
    bus = LocalBus()
    q1, q2 = await bus.subscribe(), await bus.subscribe()
    arb = Arbitrator(bus=bus, wake_window=0)
    dev = await arb.register('d1')
    assert dev is await arb.register('d1')
    assert isinstance(await q1.get(), DeviceAnnounce)
    await bus.unsubscribe(q2)
    while not q2.empty():
        q2.get_nowait()
    await arb.claim_wake('d1', claim_id='client-id', wake_word='Arya')
    assert q2.empty()
    await q1.get()  # repeated registration
    assert isinstance(await q1.get(), WakeClaim)
    await arb.unregister('d1')
    assert arb.devices == {}


@pytest.mark.parametrize('raw', ['unknown', 'proximity', None])
def test_default_policy_is_ranked_wake_selection(raw):
    assert load_settings({'arbitrate_policy': raw}.get).arbitrate_policy == 'wake_signal'


@pytest.mark.parametrize('raw', [float('nan'), float('inf'), -1, 'invalid'])
def test_invalid_window_uses_default(raw):
    assert load_settings({'arbitrate_wake_window_seconds': raw}.get).arbitrate_wake_window_seconds == 0.75


def test_default_window_is_shared_by_settings_and_arbitrator():
    from hermes_converse.settings import Settings

    assert Arbitrator().wake_window == Settings().arbitrate_wake_window_seconds == 0.75
    assert load_settings({}.get).arbitrate_wake_window_seconds == 0.75
    assert load_settings({'arbitrate_wake_window_seconds': 0.25}.get).arbitrate_wake_window_seconds == 0.25


@pytest.mark.asyncio
async def test_logs_correlate_claims_elections_and_decisions_without_phrase(caplog):
    import logging

    caplog.set_level(logging.INFO, logger='hermes_converse.arbitrate')
    arb = await _arbitrator()
    await _pair(arb)
    await arb.claim_wake('d1', claim_id='later', wake_word='Hey Arya')
    messages = [r.getMessage() for r in caplog.records]
    opened = [m for m in messages if 'wake election opened' in m]
    assert len(opened) == 2
    election_ids = [m.split('election_id=')[1].split()[0] for m in opened]
    assert election_ids[0] != election_ids[1]
    for event in ('wake claim received', 'wake decision'):
        entries = [m for m in messages if event in m]
        assert len(entries) == 3
        assert sum(f'election_id={election_ids[0]}' in m for m in entries) == 2
        assert sum(f'election_id={election_ids[1]}' in m for m in entries) == 1
    assert sum('wake election closed' in m for m in messages) == 2
    assert any('received_unix_ms=' in m and 'election_age_ms=' in m for m in messages)
    assert any('decision=wake_deny' in m and "claim_id='client-2'" in m for m in messages)
    assert any('decision=wake_grant' in m and 'wait_ms=' in m for m in messages)
    assert 'Hey Arya' not in caplog.text

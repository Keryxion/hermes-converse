"""Local converse load test: the real route and gateway adapter over a fake Hermes in a server
process; N live connections stream real-time silence while a fresh connection is timed to `ready`
at each step, and the server's thread count is read. Silence never trips the VAD, so no turn runs.

    HERMES_AGENT_ROOT=<hermes checkout> python -m tests.load_converse [STEPS] [HOLD]

STEPS defaults to 1,4,8,16,32 live connections and HOLD to 10 seconds at each step. Linux only:
the thread count comes from /proc.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time

import aiohttp

BLOCK = bytes(960)  # 30 ms of 16 kHz pcm16 silence
PROBES = 9


def serve() -> None:
    import tests.conftest  # noqa: F401  (the plugin package and an isolated HERMES_HOME)
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_converse import speech
    from tests.support.fake_hermes import FakeHermes
    from tests.test_route import API_KEY, Harness

    platform_registry.register(PlatformEntry(
        name="converse", label="Converse", adapter_factory=lambda config: None, check_fn=lambda: True,
        trusted_inbound=True, source="builtin"))
    speech.streaming_provider = lambda cfg: None
    speech.load_tts_config = dict
    speech.tts_text_cap = lambda cfg: 4000
    speech.stt_model_override = lambda: None

    async def run() -> None:
        async with Harness(FakeHermes(API_KEY)) as client:
            print(json.dumps({"url": str(client.make_url("/v1/audio/converse")), "key": API_KEY}), flush=True)
            await asyncio.Event().wait()

    asyncio.run(run())


def _threads(pid: int) -> int:
    with open(f"/proc/{pid}/status") as status:
        return int(next(line for line in status if line.startswith("Threads:")).split()[1])


def _cpu_seconds(path: str) -> float:
    with open(f"{path}/stat") as stat:
        fields = stat.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")  # utime + stime


def _thread_cpu(pid: int) -> dict[str, float]:
    """CPU seconds by thread, keyed ``tid name``."""
    seconds = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        path = f"/proc/{pid}/task/{tid}"
        try:
            with open(f"{path}/comm") as comm:
                seconds[f"{tid} {comm.read().strip()}"] = _cpu_seconds(path)
        except OSError:
            pass  # the thread ended
    return seconds


async def _connect(http, url: str, key: str, name: str):
    started = time.monotonic()
    ws = await http.ws_connect(url)
    await ws.send_str(json.dumps({"type": "start", "key": key, "assistant_name": "Arya", "device_name": name}))
    while True:
        msg = await asyncio.wait_for(ws.receive(), 30)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError(f"{name}: {msg.type} {ws.close_code}")
        if json.loads(msg.data).get("type") == "ready":
            return ws, (time.monotonic() - started) * 1000


async def _stream(ws, lag: dict) -> None:
    async def drain():
        async for _ in ws:
            pass
    reader = asyncio.ensure_future(drain())
    due = time.monotonic()
    try:
        while not ws.closed:
            await ws.send_bytes(BLOCK)
            due += 0.03
            lag["ms"] = max(lag["ms"], (time.monotonic() - due) * 1000)
            await asyncio.sleep(max(0.0, due - time.monotonic()))
    finally:
        reader.cancel()


async def load(url: str, key: str, pid: int, steps: list[int], hold: float) -> None:
    held, tasks, lag = [], [], {"ms": 0.0}
    async with aiohttp.ClientSession() as http:
        try:
            for live in steps:
                while len(held) < live:
                    ws, _ = await _connect(http, url, key, f"Load {len(held) + 1}")
                    held.append(ws)
                    tasks.append(asyncio.ensure_future(_stream(ws, lag)))
                cpu, threads = _cpu_seconds(f"/proc/{pid}"), _thread_cpu(pid)
                await asyncio.sleep(hold)
                cpu = (_cpu_seconds(f"/proc/{pid}") - cpu) / hold
                busiest = sorted(((spent - threads.get(name, 0.0)) / hold, name.split(" ", 1)[1])
                                 for name, spent in _thread_cpu(pid).items())[-3:]
                probes = []
                for _ in range(PROBES):
                    ws, ms = await _connect(http, url, key, "Load Probe")
                    probes.append(ms)
                    await ws.close()
                print(json.dumps({"live": live, "server_threads": _threads(pid), "server_cpu": f"{cpu:.0%}",
                                  "busiest_threads": [f"{name} {share:.0%}" for share, name in reversed(busiest)],
                                  "ready_ms_median": round(statistics.median(probes)),
                                  "ready_ms_max": round(max(probes)), "dropped": sum(ws.closed for ws in held),
                                  "max_send_lag_ms": round(lag["ms"])}), flush=True)
                lag["ms"] = 0.0
        finally:
            for task in tasks:
                task.cancel()
            for ws in held:
                await ws.close()


def main() -> None:
    if sys.argv[1:2] == ["serve"]:
        serve()
        return
    steps = [int(n) for n in (sys.argv[1] if len(sys.argv) > 1 else "1,4,8,16,32").split(",")]
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    server = subprocess.Popen([sys.executable, "-m", "tests.load_converse", "serve"], stdout=subprocess.PIPE, text=True)
    try:
        started = json.loads(server.stdout.readline())
        print(json.dumps({"live": 0, "server_threads": _threads(server.pid)}), flush=True)
        asyncio.run(load(started["url"], started["key"], server.pid, steps, hold))
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()

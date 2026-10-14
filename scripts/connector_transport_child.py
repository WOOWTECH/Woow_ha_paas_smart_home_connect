"""Owned loopback WebSocket process; no HA/CF credentials or remote traffic."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent

async def main():
    # Manager passes a synthetic token; intentionally never inspect or persist it.
    async def reply(ws):
        async for message in ws:
            request = json.loads(message)
            await ws.send(json.dumps({'id': request['id'], 'pid': os.getpid(), 'echo': 'owned-read-only-probe'}))
    done = asyncio.get_running_loop().create_future()
    if (ROOT / 'ignore-term').exists():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: done.done() or done.set_result(None))
    if (ROOT / 'fail-start').exists():
        raise SystemExit(42)
    async with serve(reply, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        with (ROOT / 'children.jsonl').open('a') as f:
            f.write(json.dumps({'pid': os.getpid(), 'port': port}) + '\n')
        await done

if __name__ == '__main__':
    asyncio.run(main())

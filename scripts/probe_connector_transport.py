"""Real candidate manager + two owned local WS children; no provider claims."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

SCRIPTS = Path(__file__).resolve().parent
SOURCE = SCRIPTS.parent / 'cloudflared_manager.py'
OUT: Path
checks = []
processes = []
_CLEANUP_WAIT_TIMEOUT = 3.0

class Bus:
    def __init__(self): self.listeners = []
    def async_listen_once(self, event, callback):
        pair = (event, callback); self.listeners.append(pair)
        def remove():
            if pair in self.listeners: self.listeners.remove(pair)
        return remove


def check(name, passed, **extra):
    row = {'check': name, 'passed': bool(passed), **extra}
    checks.append(row)
    (OUT / 'checks.json').write_text(json.dumps(checks, indent=2)+'\n')
    print(json.dumps(row), flush=True)
    if not passed: raise AssertionError(name)


def load_manager():
    for name in ['homeassistant','homeassistant.core','homeassistant.const',
                 'homeassistant.helpers','homeassistant.helpers.aiohttp_client']:
        sys.modules[name] = types.ModuleType(name)
    sys.modules['homeassistant.core'].HomeAssistant = object
    sys.modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP = 'homeassistant_stop'
    sys.modules['homeassistant.helpers.aiohttp_client'].async_get_clientsession = lambda _: None
    spec = importlib.util.spec_from_file_location('shc_candidate_manager', SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_manager(module, path):
    path.mkdir()
    shutil.copyfile(SCRIPTS/'connector_transport_child.py', path/'socket_child.py')
    wrapper = path/'cloudflared'
    wrapper.write_text('#!'+sys.executable+'\n'+'import runpy\nrunpy.run_path('+repr(str(path/'socket_child.py'))+',run_name="__main__")\n')
    wrapper.chmod(0o700)
    return module.CloudflaredManager(types.SimpleNamespace(bus=Bus(), is_stopping=False), str(path))


def latest(path):
    return json.loads((path/'children.jsonl').read_text().splitlines()[-1])


async def read(ws, n):
    await ws.send(json.dumps({'id':n}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert reply['id'] == n and reply['echo'] == 'owned-read-only-probe'
    return reply


async def main():
    if (OUT/'result.json').exists(): raise RuntimeError('existing_result_do_not_overwrite')
    module=load_manager(); managers=[]; sockets=[]; failure=None; temporary=None
    try:
        with tempfile.TemporaryDirectory(prefix='shc-real-ws-reset-') as directory:
            temporary=directory; root=Path(directory)
            for role in ['target','control']:
                manager=make_manager(module,root/role);managers.append(manager)
                check(role+'_started',await manager.start_tunnel('synthetic-local-only'))
                processes.append(manager._process)
                entry=latest(root/role)
                ws=await connect('ws://127.0.0.1:'+str(entry['port']),open_timeout=3);sockets.append(ws)
                value=await read(ws,1);check(role+'_baseline_real_ws',value['pid']==entry['pid'])
            target,control=managers; old=target._process; started=time.monotonic()
            result=await target.reset_tunnel()
            processes.append(target._process)
            check('candidate_reset_receipt',result.cutoff_verified and result.restarted,cutoff_verified=result.cutoff_verified,restarted=result.restarted)
            check('old_child_exit_verified',old.returncode is not None,returncode=old.returncode)
            await asyncio.wait_for(sockets[0].wait_closed(),5)
            try:
                await read(sockets[0],2)
            except ConnectionClosed as e:
                check('old_socket_actual_transport_closed',True,error_type=type(e).__name__,elapsed_seconds=round(time.monotonic()-started,4))
            else:
                check('old_socket_actual_transport_closed',False)
            value=await read(sockets[1],2)
            check('same_control_socket_remains_usable',value['pid']==control._process.pid)
            entry=latest(root/'target');ws=await connect('ws://127.0.0.1:'+str(entry['port']),open_timeout=3);sockets.append(ws)
            value=await read(ws,3)
            check('replacement_accepts_fresh_socket',value['pid']==entry['pid'] and value['pid']!=old.pid)
            # Actual OS SIGTERM-resistant child exercises the real five-second KILL path.
            (root/'target'/'ignore-term').touch()
            preparation=await target.reset_tunnel();check('term_resistant_child_started',preparation.cutoff_verified and preparation.restarted)
            processes.append(target._process);stubborn=target._process
            entry=latest(root/'target');ws=await connect('ws://127.0.0.1:'+str(entry['port']),open_timeout=3);sockets.append(ws)
            await read(ws,4);(root/'target'/'ignore-term').unlink()
            result=await target.reset_tunnel();processes.append(target._process)
            check('real_SIGKILL_reset_receipt',result.cutoff_verified and result.restarted and stubborn.returncode==-9,returncode=stubborn.returncode)
            await asyncio.wait_for(ws.wait_closed(),5)
            try:await read(ws,5)
            except ConnectionClosed:check('SIGKILL_old_socket_closed',True)
            else:check('SIGKILL_old_socket_closed',False)
            await read(sockets[1],5);check('control_survives_target_SIGKILL',True)
            # Startup failure after verified cutoff must be a distinct outcome.
            (root/'target'/'fail-start').touch();old=target._process
            result=await target.reset_tunnel()
            check('restart_failure_not_reported_success',result.cutoff_verified and not result.restarted)
            check('restart_failure_prior_child_exited',old.returncode is not None)
            await read(sockets[1],6);check('control_survives_target_restart_failure',True)
            check('target_stopped_after_failed_restart',await target.stop_tunnel())
            for ws in sockets:await ws.close()
            for i,manager in enumerate(managers):check('cleanup_stop_'+str(i),await manager.stop_tunnel())
            check('all_owned_child_handles_exited',all(p is None or p.returncode is not None for p in processes))
            check('all_shutdown_hooks_removed',all(not m.hass.bus.listeners for m in managers))
    except BaseException as error:
        failure=type(error).__name__
    finally:
        for ws in sockets:
            try:await ws.close()
            except Exception:pass
        cleanup=True
        for manager in managers:
            try:cleanup=await manager.stop_tunnel() and cleanup
            except Exception:cleanup=False
        # Tracked process handles only; no PID scanning or unrelated process termination.
        cleanup_failure_types = []
        for process in processes:
            if process is not None and process.returncode is None:
                cleanup = False
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), _CLEANUP_WAIT_TIMEOUT)
                except Exception as error:
                    # Safe type metadata only. One failed owned handle must not
                    # abandon the remaining handles or prevent the final report.
                    cleanup_failure_types.append(type(error).__name__)
        result={'candidate_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'scope':'real candidate manager, actual loopback WebSocket/process lifecycle; only HA wiring stubbed','real_Cloudflare':False,'automatic_policy_revocation':False,'checks':checks,'failure_type':failure,'cleanup':cleanup,'cleanup_failure_types':cleanup_failure_types,'runtime_removed':temporary is not None and not Path(temporary).exists(),'passed':failure is None and cleanup and bool(checks) and all(x['passed'] for x in checks)}
        (OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'passed':result['passed'],'failure_type':failure,'checks':len(checks),'cleanup':cleanup}),flush=True)
        return 0 if result['passed'] else 1

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    OUT = args.output.resolve()
    OUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    raise SystemExit(asyncio.run(main()))

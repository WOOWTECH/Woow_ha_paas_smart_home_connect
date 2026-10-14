# Serialized local connector reset

`await manager.reset_tunnel()` returns an immutable `TunnelResetResult` with
`cutoff_verified: bool` and `restarted: bool`. It takes no arguments and reuses
the currently desired token only in memory. The receipt contains no credentials,
process arguments, or provider identifiers.

| cutoff_verified | restarted | Meaning |
| --- | --- | --- |
| true | true | Prior owned child exit observed; replacement passed the existing local startup check. |
| true | false | Prior exit verified, but restart failed or was prevented by shutdown/no desired token. |
| false | false | Prior exit/cleanup could not be verified; ownership retained and replacement blocked. |

`restarted` is a local process outcome at receipt time, not Cloudflare readiness.
`cutoff_verified` concerns only the prior child owned by this manager. With no
owned child it reports the vacuous local stopped state. This cannot establish
remote connector identity, replica cutoff, policy propagation, global revocation,
or end-to-end WebSocket revocation. No automatic consumer is wired in this slice.
A future caller must decide whether interrupting this connector is acceptable;
all connections carried by that child may be interrupted.

The existing lifecycle lock spans stop, observed exit, and replacement startup.
Manual start/stop, watchdog recovery, concurrent resets, and shutdown therefore
cannot install overlapping replacements. Successful reset resumes the watchdog.
A failed or cancelled reset clears restart intent and disables recovery; it does
not retry replacement in the background. An explicit subsequent start can recover
a verified stopped manager. If cleanup is unverified, `start_tunnel()` returns
false without changing restart intent; first retry `stop_tunnel()` to verify
cleanup. Public stop retries require the same observed-exit verification as
reset, including TERM/KILL signal lookup races; an unverified retry keeps manual
start blocked. PR25's existing final SIGKILL-timeout process retention remains in use.

Cancellation while waiting for the lock changes no lifecycle state. Once reset
owns the lock, cancellation raises `asyncio.CancelledError` with no receipt,
even if the old child has already exited. It waits for a pending subprocess spawn
to return ownership, then performs bounded TERM/KILL cleanup under the lock.
Repeated cancellation cannot abandon that cleanup. If cleanup times out, the
manager retains the child handle and blocks replacement. Spawn handoff itself
has no artificial deadline: releasing the lock before the native spawn returns
would risk losing child ownership. HA shutdown prevents replacement before spawn
and rechecks after startup; if shutdown begins during startup, replacement cleanup
is attempted and `restarted` is false.

Verification uses synthetic tokens and owned local OS children; only HA wiring
is stubbed in the portable suite. The inherited-pipe timeout cases use a separately
owned/reaped helper and real native subprocess waits. Signal dispatch/spawn-return
instrumentation exercises cancellation and signal lookup races at OS boundaries.

```sh
python scripts/test_watchdog.py
```

The canonical runner isolates pytest collection from the integration package and
runs both existing watchdog and new reset regressions. To select reset tests:

```sh
candidate="$PWD/cloudflared_manager.py"
scratch=$(mktemp -d)
cp watchdog_tests/test_cloudflared_watchdog.py "$scratch/"
(cd "$scratch" && SHC_MANAGER_SOURCE="$candidate" python -m pytest -q test_cloudflared_watchdog.py -k reset)
rm -r "$scratch"
```

Native HA unload verification and the parent's owned WebSocket transport proof
are separate checks. Local process evidence must not be promoted to global
revocation or production acceptance.


A second portable proof runs the candidate manager against two real loopback
WebSocket child processes. It verifies graceful cutoff/EOF, a fresh replacement
connection, TERM-resistant/SIGKILL cutoff, unchanged control connection, failed
replacement receipt, and cleanup. It uses the pinned watchdog requirements and
is also run by the watchdog CI job. The output directory must not already exist.

```sh
python scripts/probe_connector_transport.py --output /tmp/shc-transport-proof-new
```

This is synthetic local transport evidence: the executable is an owned Python
WebSocket server, not cloudflared, and HA event wiring is stubbed. No actual
provider, HA instance, policy, devices, credentials, or production traffic is
used. The proof does not claim automatic policy-triggered revocation.

The proof fallback waits at most three seconds per still-running owned handle.
A signal lookup error or wait timeout marks cleanup failed and records only the
exception type in `cleanup_failure_types`. Cleanup continues with every remaining
owned handle and writes `result.json` with a failed outcome; no PID scans occur.
A focused simulated failure-seam regression checks this deadline and final-report
behavior separately from the real 21-check transport oracle.

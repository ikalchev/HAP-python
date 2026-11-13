# Setter/getter callback interface refactor — design

Status: agreed design, not yet implemented. This document is the contract for
the refactor: the new interface, how it behaves compared to the current one,
the migration path for existing integrators, and the alternative that was
considered and parked.

Related: issue [#75](https://github.com/ikalchev/HAP-python/issues/75)
(allow characteristics to indicate set value failure).

## Motivation (short)

The current callback contract grew ad hoc and shows the strain:

- Camera/HDS/WebRTC setters need per-connection context and get it through a
  magic-parameter hack (`sender_client_addr`, detected by literal parameter
  name in `pyhap/characteristic.py`).
- Callbacks cannot report a HAP error status (issue #75): every failure is
  `-70402`, so controllers cannot learn that a device is busy or refused.
- Getters are sync-only and run on the event loop; a slow getter stalls the
  whole server, and a raising getter fails the entire `/accessories` response.
- Async-ness is inconsistent: characteristic setters may be coroutine
  functions (result-detected), service/accessory setters may not, getters may
  not.
- Client identity is the `(ip, port)` tuple, which is only unique while a
  connection lives. Several recent fixes (#544, #557) patch instances of the
  address-reuse bug class instead of removing it.

## New interface

### Contexts

Frozen dataclasses, passed to every delegate call. New input data is added as
new fields; signatures never change.

```python
# pyhap/context.py
@dataclass(frozen=True)
class Context:
    client_addr: tuple[str, int] | None   # writer's/reader's address
    client_uuid: UUID | None              # paired controller identity, None
                                          # before pair-verify completes
    connection: HAPConnectionHandle       # stable per-connection identity
    deadline: float                       # monotonic; the driver answers
                                          # -70408 after it passes

@dataclass(frozen=True)
class ReadContext(Context):
    characteristic: Characteristic        # the char being read

@dataclass(frozen=True)
class WriteContext(ReadContext):
    characteristic: Characteristic | None  # None for grouped (service/
                                           # accessory-level) calls
    is_admin: bool
```

### Connection handle

```python
# pyhap/connection.py
class HAPConnectionHandle:
    """Opaque per-connection identity. Exposes client_addr only.

    Equality and hashing are by (client_addr, generation), so a handle is an
    exact identity even while driver internals are still address-keyed.
    """
```

Phased migration, **to be documented in this class's docstring and in
`docs/source/advanced/async.rst` when implemented**:

1. Phase 1 (this refactor): the handle appears in contexts and is used for
   camera session ownership. Driver internals (`topics`,
   `prepared_writes`, `_write_responses`, `_pending_connection_writes`,
   `http_server.connections`) remain address-keyed; existing guards
   (`_ConnectionWrites.alive`, dropping late responses) stay in place.
2. Phase 2 (later release): re-key the internals above by the handle and
   delete the address-boundary guards.

### Errors

```python
# pyhap/errors.py
class HapStatusError(Exception):
    """Raised by a delegate to answer a HAP status code for its operation."""
    def __init__(self, status: int) -> None: ...
```

Curated statuses a delegate may raise (HAP codes, see `pyhap/const.py`):

| Status | Meaning | Notes |
|---|---|---|
| `-70402` SERVICE_COMMUNICATION_FAILURE | default | Any other exception maps to this (unchanged from today) |
| `-70403` RESOURCE_BUSY | device refuses, busy | the common "no" answer |
| `-70407` OUT_OF_RESOURCE | e.g. no free streaming session | |
| `-70409` RESOURCE_DOES_NOT_EXIST | e.g. unknown session id | |
| `-70410` INVALID_VALUE_IN_REQUEST | passed format checks, semantically wrong | |
| `-70411` INSUFFICIENT_AUTHORIZATION | app-level refusal for this controller | |

Driver-reserved codes (`-70401` privileges, `-70404` read-only, `-70405`
write-only, `-70406` notification-unsupported, `-70408` timed-out) are enforced
by the driver before delegate code runs or owned by its watchdog.
Constructing a `HapStatusError` with a reserved or unknown code raises
`ValueError` immediately, so a bad status fails at its source instead of
silently degrading a request.

### Delegates

```python
# pyhap/delegates.py
class CharacteristicDelegate:
    """Noop defaults; override what you need.

    Every method is a coroutine function BY DESIGN. The driver awaits every
    call; there is no sync/async detection on this path. A method that is
    overridden but not async fails registration with a TypeError.

    One instance may be shared by many characteristics: the context carries
    ``characteristic`` and identifies the target.
    """

    async def on_get(self, context: ReadContext) -> Any: ...
    async def on_set(self, value: Any, context: WriteContext) -> Any: ...
    async def on_subscribe(self, context: ReadContext) -> None: ...
    async def on_unsubscribe(self, context: ReadContext) -> None: ...

class ServiceDelegate:
    async def on_set(
        self, values: Mapping[Characteristic, Any], context: WriteContext
    ) -> None: ...
```

- `on_set` return values keep today's write-response / `read_back_response`
  semantics: used as the write response for `wr` characteristics and the
  per-connection read-back value.
- Registration: `char.delegate = ...`, `service.delegate = ...`, plus
  `configure_char(..., delegate=...)`. Registration time is when all
  validation happens (override detection, async-ness check) — the read/write
  paths never introspect anything.
- "Not overridden" and "overridden, returns None" are distinct: an
  unoverridden `on_get` means "use the stored value"; an overridden one
  returning `None` means the value is null (e.g. Programmable Switch Event).
- A noop base class saves boilerplate; subclass and override.

### Accessory lifecycle methods

Overridable methods on `Accessory` (users already subclass it for
`run`/`stop`):

```python
async def client_connected(self, client_addr) -> None: ...    # TCP accepted
async def client_verified(self, client_addr, client_uuid) -> None: ...  # pair-verify done
def client_disconnected(self, client_addr) -> None: ...       # existing, unchanged
```

`client_disconnected` stays sync for compatibility; the two new methods are
async by design, like delegates. Exceptions in any of them are isolated and
logged.

## Write pipeline (new style)

For `PUT /characteristics` writing characteristics A, B of one service:

1. Validation, privilege checks, timed-write checks — unchanged.
2. Store the requested values; **notify is deferred** (today, sync setters
   notify before grouped hooks run).
3. **Gate**: `AccessoryDelegate.on_set` (if any), then per-service
   `ServiceDelegate.on_set`, awaited with `context.deadline`. A curated
   `HapStatusError` vetoes the request: previous values are restored, nothing
   is notified, no per-characteristic delegate runs, the raised status is
   reported for the affected characteristics. Any other exception → `-70402`
   for the group, treated like a veto.
4. Per-characteristic `on_set` in request order; async ones awaited
   concurrently under the same deadline. A char answering after its deadline
   → `-70408` for that char (cancellation as today).
5. Notify subscribers of characteristics that succeeded; immediate-notify
   characteristics behave as today.
6. Response carries per-characteristic statuses and any write responses.

Legacy grouped callbacks cannot veto (unchanged from today); the gate is the
new-style replacement for them.

## Read pipeline (new style)

Same async contract as writes — this is the point of "getter and setter have
the same async-ness":

- `GET /characteristics` and `GET /accessories` await `on_get` per readable
  characteristic under `context.deadline` (via the existing `response.task`
  mechanism, as `POST /resource` already does). Timeout → `-70408` for that
  characteristic.
- Cancellation on connection close mirrors the write path.
- `Characteristic.get_value()` keeps its sync meaning for the legacy
  `getter_callback` only; the new-style driver read path calls delegates
  directly.
- The `to_HAP` value cache already skips getter-backed characteristics and
  stays as-is.

## Legacy facade (backwards compatibility)

`setter_callback` / `getter_callback` on `Characteristic`, `Service` and
`Accessory` become properties:

- Assigning wraps the callable into an internal `_LegacyDelegate` that
  adapts it to the new interface. Reading returns the original callable, so
  code that compares or reassigns (including tests and the camera stack's
  own setup) sees no change.
- Normalization happens at assignment time (callbacks are assigned after
  construction in the wild, sometimes after driver start — e.g.
  `pyhap/camera.py` recording setup). The pipeline code itself only ever
  sees delegates.
- The legacy pipeline keeps today's semantics byte-for-byte:
  - `f(value)` and `f(value, sender_client_addr)` signatures, detected by
    parameter name as today;
  - sync callables run inline on the event loop (no timeout — unchanged);
  - a characteristic's legacy setter may be a coroutine function, detected by
    result as today, awaited with the write deadline;
  - service/accessory-level legacy setters are sync-only; a returned
    coroutine is closed with a warning (unchanged);
  - the service-level legacy setter still receives `{display_name: value}`;
  - every exception maps to `-70402`;
  - today's ordering: per-char sync parts (with notify) → accessory setter →
    service setters → await per-char coroutines.
- If both a legacy callback and a delegate are registered, the delegate wins
  and a warning is logged.

## Behavior changes vs. the current design

### Unchanged (guaranteed)

- Wire protocol, pairing, HAP status codes for driver-level errors, `ev`
  subscription mechanics, notify fan-out, per-connection write-response /
  read-back privacy, prepared and timed writes, camera TLV formats.
- `set_value()` never invokes setters or delegates (it publishes local
  changes only, as today).
- `client_update_value()` / `client_update_finished()` public API keeps
  working for direct calls (used by tests).
- All legacy callback signatures and their threading contract (sync callables
  run on the event loop).
- The author-facing camera methods (`start_stream`, `stop_stream`,
  `reconfigure_stream`, `get_snapshot`/`async_get_snapshot`,
  `handle_recording_stream`, `talkback_audio_received`) — these are not
  characteristic callbacks: pyhap's camera internals invoke them with their
  own `session_info` signatures, and embedders override them with matching
  signatures (e.g. Home Assistant's `type_cameras.py`). pyhap's internal
  camera *wiring* — the setters that call them — does migrate to delegates
  and contexts (phase 2), see "Deliberate changes" below.

### New behavior (only when delegates/lifecycle hooks are used)

| Area | Current | New |
|---|---|---|
| Async-ness | Getter: sync only. Setter: sync, or coroutine function inferred from the returned object. Grouped setters: sync only. | Every delegate method is async by design, validated at registration, always awaited. Uniform for getter, setter, grouped and subscribe hooks. |
| Callback input | `f(value)`, or `f(value, sender_client_addr)` by literal parameter name. | `await on_set(value, context)` with a frozen context: `client_addr`, `client_uuid`, `connection`, `deadline`, `is_admin`. Extensible without signature changes. |
| Error reporting | Any exception → `-70402`. | Curated `HapStatusError` statuses (table above); other exceptions still `-70402`. |
| Grouped hooks | Run after per-char sync parts, cannot veto, sync only. | Run first as a gate; can veto via `HapStatusError` (values restored, nothing applied); async, so they may do IO before deciding. |
| Notify timing | Sync setters notify before grouped hooks run. | New-style: notify deferred until per-char `on_set` succeeds; a veto notifies nobody. Legacy path unchanged. |
| Getter IO | Impossible (sync, blocks the loop; no timeout). | Async getter with a deadline; per-characteristic isolation and `-70408` on timeout. |
| Read of `/accessories` with a failing getter | Whole request fails (500 / `-70402`) — one bad getter can break pairing. | Per-characteristic trapping (see "applies to legacy" below). |
| Hooks | `client_disconnected` only. | `client_connected`, `client_verified`, per-char `on_subscribe` / `on_unsubscribe`. |
| Identity | `(ip, port)` tuple. | Stable handle, equal by `(addr, generation)`. |

### Deliberate changes that also affect legacy users

1. **`/accessories` per-characteristic error isolation.** A raising
   `getter_callback` (or `on_get`) answers `-70402` for its characteristic
   instead of failing the whole request. This is a behavior change on the
   error path only — existing accessories that do not raise are unaffected,
   and the old behavior was a robustness bug (a single bad getter breaks
   pairing).
2. **`HapStatusError` works for legacy setters too.** A legacy
   `setter_callback` that raises a curated `HapStatusError` is mapped to that
   status instead of `-70402`. This closes issue #75 for existing code with
   no signature change.
3. **Camera internals** migrate to delegates and contexts. No external
   protocol change, but: session ownership (camera streams, multi-tier,
   WebRTC) is keyed by the handle, which ends the address-reuse leak class
   (e.g. #557) structurally; `sender_client_addr` parameter-name detection
   and WebRTC's self-computed `_deadline()` disappear from the internals —
   both were implementation details, but subclass authors who copied the
   pattern should switch to delegates.

### Explicitly not changed

- Sync legacy setters still block the event loop if they are slow. The
  deadline protects *awaiting*, not sync CPU-bound work; running sync setters
  in the executor would silently change their threading contract and break
  embedders (e.g. Home Assistant runs the driver on its own loop). The
  documented answer for slow work is: make it async.

## Migration notes for integrators

- **Nothing is required.** Every existing callback keeps working with
  unchanged semantics. The legacy attributes are not deprecated on a
  timeline; they are documented as "legacy" alongside the new interface.
- **Home Assistant** (pins the exact version, runs the driver on its own
  loop): today its setters are sync fire-and-forget that resync state on
  failure because they cannot answer HomeKit. With delegates it can
  `await hass.services.async_call(...)` in `on_set` and raise `HapStatusError`
  on failure, giving controllers a correct error instead of an optimistic
  flip-back; grouped `on_set` (gate) replaces the display-name dict for
  thermostat/light multi-characteristic writes.
- **Camera authors**: nothing required. The methods they override
  (`start_stream` etc.) keep their signatures — pyhap's internal camera
  setters migrate to delegates and contexts, but the author-facing methods
  they call are untouched. Adopting delegates directly is optional.

## Alternative considered: driver-level listener registry (parked)

Drafted and set aside — it composes with, rather than replaces, the chosen
design:

```python
# pyhap/hooks.py — DRAFT, NOT CHOSEN
class HAPListener:
    """Observer of connection lifecycle events.

    Observers only: no return values, cannot veto, exceptions isolated per
    listener. Connection-level only — characteristic events stay on
    CharacteristicDelegate.
    """
    def connection_opened(self, connection: HAPConnectionHandle) -> None: ...
    def connection_verified(
        self, connection: HAPConnectionHandle, client_uuid: UUID
    ) -> None: ...
    def connection_closed(self, connection: HAPConnectionHandle) -> None: ...

# AccessoryDriver
driver.add_listener(listener)        # multiple; broadcast in registration order
driver.remove_listener(listener)     # both thread-safe
```

Semantics drafted for it:

- Observer-only by contract: with N listeners there is no coherent veto, so
  the registry can never host the gate. Owner hooks (delegates / Accessory
  methods) run first and decide; listeners then observe.
- Dispatch order: owner first, then listeners in registration order; each
  listener failure logged, never affects the request.

Comparison:

| | Delegates / Accessory methods (chosen) | Driver registry (draft) |
|---|---|---|
| Cardinality | one owner per hook point | N observers, broadcast |
| Veto / return values | yes (gate, write responses) | no, by design |
| Scope | per characteristic / service / accessory | process-wide |
| Discovery | explicit, local | implicit, global coupling |
| Fits | device logic, refusals, cameras | embedder telemetry, bridging app frameworks |

Verdict: purely additive later; no embedder has demanded it. Kept here so the
lifecycle-event naming stays aligned (`client_connected`, `client_verified`,
`client_disconnected` ↔ `connection_opened`, `connection_verified`,
`connection_closed`).

## Implementation phases

Each phase ships green tests and doc updates.

1. **`HapStatusError` + curated mapping + `/accessories` per-char isolation.**
   Small; benefits legacy setters immediately; closes #75.
2. **Contexts, handle, delegates, legacy facade, gate write pipeline.**
   Camera/multi-tier/WebRTC internals migrate; `sender_client_addr` detection
   and WebRTC `_deadline()` deleted.
3. **Lifecycle methods and subscribe hooks.**
4. **Async read path** (uniform async-ness completed).
5. **Handle-keyed internals** (phase 2 of the connection migration), guarded
   by the documented plan in the handle's docstring.

## Documentation obligations (per AGENTS.md)

- New `docs/source/api/` pages for `pyhap.context`, `pyhap.errors`,
  `pyhap.delegates`, `pyhap.connection`; listed in `docs/source/index.rst`
  and the `api/index.rst` glob.
- `docs/source/advanced/async.rst`: the callback contract table (who runs
  where, who may block, deadlines, veto semantics) and the connection-handle
  migration plan.
- `docs/source/intro/overview.rst`: interaction diagram updates for the
  write/read pipeline.
- `CHANGELOG.md` entries per release.

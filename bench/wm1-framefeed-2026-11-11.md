# WM v1 / Stage WM-A — lease-free settled frame feed

Built 2026-11-11 under ArK's approval of the council-reconciled design in
`docs/WM-V1-ARCHITECTURE.md` and
`hidden_files/COUNCIL-WMV1-RECONCILED.md`. Only the frame-feed stage is built.
Genealogy, controller, presenter, takeover and promotion are out of scope.

## Design as built

`wm/worker_framefeed.py` adds a worker-owned AF_UNIX stream listener at
`SD/driver/frames-feed.sock`. The socket is bound under umask 0177, chmodded
0600, and accepts only the worker's UID via SO_PEERCRED, like `act.sock`.
`worker.json` advertises `frames_feed_socket`. The existing action listener
and driver protocol remain separate.

The normal worker loop calls `FrameFeed.publish()` after its action tick.
Publication requires existing pixel settlement (no dirty tiles, outstanding
capture, failed capture or uninitialized tile hashes), committed metadata,
and the worker's existing `covered()` predicate. It NEVER calls
`settle_pixels()`, GetImage, QueryPointer, XFixes, reconciliation, a provider,
or any other X operation. It adds no X connection, capture timer or refresh
request. The existing worker/observe/watchdog paths determine settlement
cadence. A stalled capture leaves the previous snapshot available.

Publication copies the settled native pixel buffer into an immutable private
snapshot, along with window geometry and cached pointer metadata. Retaining
the live Xvfb mmap would allow an unprocessed repaint to leak into a query;
the private copy also protects against partially updated GetImage shadow
buffers. Only a new pixel settlement serial replaces that buffer; metadata
and pointer revisions can reuse its pixels. This retains the existing
capture's consistency limits; it does not create an atomic X-server capture
primitive. Feed requests read this snapshot only, and encode PNGs lazily.

Session frames cover the whole native desktop. Window frames crop the
mapped window's geometry, clipped to the desktop, from the same settled
desktop snapshot. These are visible desktop crops with native occlusion,
not offscreen/unoccluded application surfaces. Native resolution, scale 1,
and desktop coordinates are returned. A missing snapshot, a window absent
from the latest settled snapshot, or an empty crop returns `no_frame_yet`.
Queries never initiate a capture to make that result disappear.

Feed connections are `FeedClient` objects in `FrameFeed.clients`, never
`Worker.peers`, driver lease records, pending observes or action queues.
Connecting, querying and disconnecting cannot acquire, renew, release or
retire a lease. Driver frame issuance, quota accounting, observation pins,
in-progress receipt pins and durable receipt retention are never called.
Shutdown closes feed clients/listener and unlinks the feed socket. Verified
session teardown's existing scratch cleanup removes feed artifacts while
preserving `driver/frames` evidence.

## Wire shape

The transport matches the worker's existing framing: four-byte unsigned
big-endian JSON byte length, then UTF-8 JSON-RPC 2.0. One request in flight
per connection; wait for its response before sending another. Pipelining
is unsupported. Only `frames.get` exists. No attach or lease token is needed.

Whole desktop request (omit `since_rev` on first query):

```json
{"jsonrpc":"2.0","id":1,"method":"frames.get","params":{"scope":{"kind":"session"},"generation":"<worker-generation>","since_rev":41}}
```

`scope` defaults to `{"kind":"session"}`. For one window use
`{"kind":"window","window":"s1/g01234567/w1"}`. `generation` is optional;
if supplied it must match. `since_rev` is optional/null or a positive
integer. Keep last-seen revisions separately for each scope and reset them
on worker generation change. Revisions are settled worker/session revisions,
so an unrelated state or pointer change may also change a window reply.

Changed session result:

```json
{
  "jsonrpc":"2.0","id":1,
  "result":{
    "session":"s1","generation":"<worker-generation>","rev":42,
    "status":"frame","changed":true,
    "frame":{
      "frame":"f:<sha256-of-PNG-bytes>",
      "path":"<SD>/driver/feed-frames/<sha256-of-PNG-bytes>.png",
      "rev":42,"rect":{"x":0,"y":0,"w":1920,"h":1080},
      "scale":1,"space":"desktop"
    },
    "cursor":{"position":{"x":120,"y":80},"image":null,"hotspot":null}
  }
}
```

The returned file is the frame payload: as with driver observe, this local
API returns a readable PNG path rather than base64 image bytes. Read/copy
it promptly. Paths are ephemeral, may be evicted between reply and open,
and are never action-coordinate capabilities or receipt evidence. On an
evicted path, retry without `since_rev` to request a fresh issuance from the
latest retained snapshot. A file already opened remains readable on Unix
after eviction. `f:<sha>` names PNG content; geometry and revision belong
to the returned issuance, not to the content ID alone.

Unchanged result (no PNG path, frame object, image bytes or encoding work):

```json
{"jsonrpc":"2.0","id":2,"result":{"session":"s1","generation":"<worker-generation>","rev":42,"status":"unchanged","changed":false}}
```

Unavailable result:

```json
{"jsonrpc":"2.0","id":3,"result":{"session":"s1","generation":"<worker-generation>","rev":null,"status":"no_frame_yet","changed":false}}
```

`rev` may be the retained session revision when only the requested window
is unavailable. All methods besides `frames.get`, including `window.act`,
`session.attach`, writes and unknown methods, return JSON-RPC -32601 with
`error.data.reason="read_only"`. Malformed JSON returns -32700/`parse_error`;
invalid queries return -32602/`invalid_params`; a supplied old generation
returns -32602/`stale_generation`. Feed file I/O failures are feed-local
`feed_unavailable` errors; they do not raise driver `provider_degraded` or
`frame_quota`. There is no push stream, blocking subscription, server-side
polling loop or on-demand capture. Querying faster repeats the latest
settlement (or returns `unchanged` with its last-seen revision).

## Caps and cursor gap

- Served-frame FIFO: **32 issuances and 64 MiB of distinct PNG files**,
  whichever binds first, in `SD/driver/feed-frames` (0700; files 0600).
  Session and window requests share this feed-only cap. Re-reading an entry
  does not reorder it. Identical PNGs share a file with reference counts;
  eviction removes it only after its final retained issuance. No pins exist.
  A single PNG exceeding the byte cap returns `frame_too_large`.
- The feed never uses the driver's `Frames` instance or
  `SD/driver/frames`. Old feed PNG/temp files are discarded on worker
  startup; neither issuance history nor snapshots survive restart.
- One immutable native snapshot, capped at **64 MiB** of packed 32-bpp
  pixels (1920×1080 uses 8,294,400 bytes), plus current window metadata.
  A larger desktop yields `no_frame_yet` instead of allocating an unbounded
  snapshot. Replacement/PNG encoding can temporarily hold additional
  buffers; the snapshot cap is not a total worker RSS limit.
- **8 feed clients**, **8 KiB request bodies**, one bounded request/reply
  per connection. Replies contain metadata and paths, not PNG bytes.
  Reads pause until the outgoing reply drains. Slow clients do not enqueue
  capture work, future frames or driver waits.

Cursor position is copied from `StateCache.pointer`, populated by the
worker's existing QueryPointer and motion handling. A position can be null
before that existing sampling completes. **Cursor image and hotspot are
not built in WM-A.** The current XFixes surface only negotiates its version;
there is no cursor-image cache or notification subscription to reuse.
GetCursorImage would add X traffic. Replies explicitly mark image/hotspot
null, and add no cursor polling. A presenter needing the exact cursor shape
still needs a later explicitly approved worker surface.

## Headless evidence

`wm/tests/test_worker_wm_a_framefeed.py` adds 18 tests, including:

- A feed fetch beside a held lease and active real `Engine` action with
  the driver's PNG quota fully occupied. Driver frame accounting, lease,
  observation pins, receipt pins and retention state remain identical;
  the action finishes successfully and peer retirement ignores the feed.
- Independent PNG decoding checks native desktop/window pixels and
  content IDs. Dirty mmap updates cannot alter an already published
  snapshot. Only the existing Pixels path schedules a deferred GetImage;
  feed queries before/after it never add another request.
- Unchanged results contain no frame payload and perform no PNG encoding;
  no-frame-yet, pending/failed capture, uncovered metadata and pointer-only
  revision cases are covered.
- Write/unknown-method refusal, invalid queries, generation mismatch,
  fragmented frames, output backpressure, oversized requests, connection
  limits, credential failure and cleanup are exercised.
- Count/byte FIFO eviction, shared-content file lifetime and feed-local
  disk failures leave the driver store untouched.

Socket binding and SO_PEERCRED syscalls are denied by this build sandbox.
Tests follow W5's established approach: real AF_UNIX socketpair descriptors
and real selector/file I/O, with listener binding and credentials injected.
Permission-setting code and the foreign-UID refusal branch are exercised;
kernel enforcement remains a live check. No display was opened.

Reproduce from any working directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 /home/ark/arkdrive/wm/tests/run_wm_a_headless.py
PYTHONDONTWRITEBYTECODE=1 python3 /home/ark/arkdrive/wm/tests/run_wm_a_headless.py full
```

The runner confines temporary directories to `wm/tests/.tmp` and discovers
the same `test_worker_w*.py` full-suite set as memory step 5 and its fix.
Targeted result: **18 tests, OK**, 0.172 s; log
`wm/tests/.tmp/wm-a-framefeed-targeted.log`.

Full-suite result: **1,082 tests in 132.917 s; 1,081 passed, 1 skipped**,
exit 0, `OK (skipped=1)`. Log:
`wm/tests/.tmp/wm-a-framefeed-full-suite.log`. This includes memory step 5
and its fix-round tests. The existing environment-dependent skip remains.

## Raj's live checks — NOT RUN in the sandbox

Run on a dedicated session display in **:91–:120**, never :0. These checks
qualify this stage only; they do not require a presenter or takeover path.

1. Start a normal worker and verify `worker.json.frames_feed_socket`, socket
   ownership/mode 0600 and directory/file modes. Exercise a real owner-UID
   client; confirm a different UID cannot connect. Confirm shutdown removes
   the socket and restart cannot serve a previous generation's snapshot.
2. Keep the normal driver attached and executing the fixture workload.
   Query session and window frames concurrently, carrying each scope's
   last-seen revision. Decode files and verify geometry, pointer coordinates,
   frame IDs and frame age while the driver moves/acts. Feed connection
   churn must not detach or extend the driver lease or alter peer retirement.
3. Compare matched driver-only and driver-plus-feed runs: per-session CPU,
   RSS, driver observe latency (p50/p95), frame age and count of errors.
   Required: **zero `provider_degraded` / `frame_quota` errors against the
   driver**; record the measured CPU and latency delta. Exercise the feed
   FIFO under sustained scene changes while driver receipt evidence is pinned.
4. Trace/count X requests with the same existing watchdog/driver workload.
   A burst of feed queries must add **zero feed-originated GetImage,
   QueryPointer or GetCursorImage requests**. Record normal capture cadence;
   there is no feed capture timer. Repeat during a long deferred capture:
   old settled frames remain available and no new capture is triggered.
5. Repeat a last-seen revision: require `unchanged` and no frame/path payload.
   Query a not-yet-settled window: require `no_frame_yet`. Send `window.act`,
   `session.attach`, a write verb and an unknown method to the feed; require
   `read_only`, zero injected input and an unaffected driver lease/action.
6. Issue more than 32 distinct frames and enough large frames to hit the
   64 MiB cap. Verify oldest-first eviction, bounded feed files, unchanged
   driver files/pins and no driver quota error. Hold a client without reading
   its reply, churn clients through the eight-client cap, then disconnect:
   no driver peer/lease retention and no growing queue of feed work.

Report live outcomes and measurement artifacts separately. Cursor image /
hotspot remain a recorded gap even if position checks pass.

## Raj verifier addendum (Nov 11, 2026)

Suite: 1,082 tests OK verified by Raj on ArK2 (commit 02585c8).

Live gate (~/wm_a_gate.py on ArK2; slate -> xvfb session -> worker ->
fx_log; a w2_client driver holds the lease and types while a separate
feed client reads frames-feed.sock): **PASS 14/14**.

- Feed socket present under SD/driver with 0600 perms.
- Session-scope frames.get returns a frame (rev 16) with a readable
  PNG in the feed's OWN feed-frames/ directory; re-query at the same
  rev returns `unchanged`; window-scope get returns a frame.
- window.act over the feed is refused: -32601, data.reason read_only.
- After each of the driver's 3 verified text acts (alpha/bravo/
  charlie, all success), the feed advanced: changed=true, new frame
  id, rev 24/32/40. The driver was never disturbed — no
  provider_degraded, no frame_quota pressure; driver frame counters
  unmoved while the feed ring held its own 5 frames.
- The reconciled council test this stage owed — feed beside an
  acting driver with zero quota errors against the driver — passes.

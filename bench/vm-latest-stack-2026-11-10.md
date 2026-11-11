# Latest stack (W6 close) on the generic x86 Debian VM — Nov 10, 2026

Question (ArK): has the current stack been checked on the VM? The earlier VM
validation (bench/vm-gui-apps-2026-11-10.md) ran W4-era code. This run syncs
the W6-closed tree (commit b545c99; wm/ only, sha256-verified tarball) into
the generic VM (~/vm-generic on ArK2, Debian 13.7.0 x86_64, KVM) and re-runs
the checks. Ops note: the ArK2 tunnel dropped transfers above ~100 KB that
evening (scp and single-stream ssh both broke); the 672 KB tarball went over
in 96 KB chunks and verified by hash on both hops.

## Results

- **Headless suite: 681/682** (one failure — a real defect, below).
- **sessiond smoke: PASS** — xvfb session start 587 ms total, list, run
  inside the display, clean stop.
- **GUI apps: 5/5 PASS** (vm_gui_test.py against the W6 code) — xterm, GNOME
  Text Editor, Thunar (folder created, filesystem oracle), GNOME Calculator
  (pixel evidence), Firefox ESR.
- **xorg profile: works after completing the guest's package set.** The
  minimal netinst image lacked xserver-xorg-core, xserver-xorg-video-dummy,
  xserver-xorg-input-evdev and xinput (session start failed with
  "Xorg: not found", then "'xinput'"); after installing them the xorg
  session starts (1.75 s) and runs.
- **AT-SPI dependency catch:** the guest also lacked python3-pyatspi and
  gir1.2-atspi-2.0 — the provider child died at import ("Namespace Atspi not
  available" / "No module named 'pyatspi'") and the channel sat in
  provider_restarting with the session otherwise healthy. After installing
  python3-pyatspi, gir1.2-atspi-2.0 and at-spi2-core the provider is healthy.
  These belong on the Phase 5 image dependency list alongside
  libglib2.0-bin (found by the first VM smoke).
- **evdev channel in the guest (wm-w6-verify.py a0a2, run inside the VM):**
  - A2-E2E **PASS** — a type act routed evdev, key sources = the session
    keyboard slave only.
  - A2-LATE **PASS** — fixture-first start, keyboard promoted to tested by
    the first verified key act.
  - A2 **partial** — evdev up, grab true, keyboard tested, isolation probes
    busy/busy, second reader 0 events; but the **XTEST keyboard leg of the
    startup self-test could not be verified in the guest** (xtest keyboard
    "down", pointer ok; observed twice). The worker marked the channel down
    honestly and evdev carried all input. Not seen on ArK2 hardware; a
    QEMU/dummy-Xorg environment difference, recorded, not worked around.

## Finding: frame eviction order depends on filesystem timestamps (real defect)

`test_oldest_evicted_receipt_reference_protected` (W3 FrameQuotaTests) fails
deterministically in the guest and passes on ArK2. Root cause, read from
wm/worker_pixels.py `Frames.evict()`: candidates sort by
`(st_mtime_ns, name)`. Three frames written back-to-back can share an
mtime_ns on a coarse-clock filesystem (the guest's tmpfs under KVM does);
the tie then breaks by content-hash filename, i.e. effectively at random,
and eviction can delete a NEWER unprotected frame while an older one stays —
the quota and the receipt-protection rules still hold, but "oldest first"
does not. On ArK2's filesystem the timestamps differ per write, so the test
passes there. Fix owed: order eviction by the store's own insertion
sequence (the `issued` bookkeeping), with mtime only as the restart/restore
fallback. Small, evidence-clear; queued as a fix round after W7 (no repo
contention with the running build).

## Verdict

The W6-closed stack runs on a clean generic Debian 13 x86 install: suite,
session manager, both session profiles, the accessibility tree (once its
packages are present), and the physical evdev input channel all verified in
the VM. One real defect found (above) and one honest environment difference
(XTEST keyboard self-test in the guest).

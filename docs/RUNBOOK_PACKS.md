# Runbook packs

The executor ships domain-blind: no application, website, or task is
baked into it. Everything task-specific travels in a **runbook pack**:
a single JSON file the caller supplies per task family.

```
CU_RUNBOOK_PACK=/path/to/pack.json python3 src/cu_executor.py "<task>"
```

Today packs apply to the **VNC surface** (remote-desktop work is where
worked plans pay off most). A pack is optional — with no pack, the
executor uses generic behavior: raise the viewer, engage the remote
desktop with a centre click, generic prompts.

## Format

```json
{
  "name": "my-pack",
  "description": "What task family this is for (documentation only).",
  "match": ["schematic", "myapp"],
  "briefing": "Execute these steps in order:\n1. observe ...\n2. ...",
  "bootstrap_click_text": "the MyApp command line, the text entry at the bottom of the main window",
  "done_hints": {
    "mouse": "click with text \"the MyApp command line\"",
    "kbd": "type_verify the openDocument line from the plan, then keys [\"Return\"]"
  },
  "viewer_match": "TigerVNC"
}
```

| Field | Meaning |
|---|---|
| `match` | Case-insensitive substrings; the pack applies only if one appears in the task text. Absent/empty = applies to every run on the surface. |
| `briefing` | A worked plan appended to the task before the model's first call. Number the steps; name the exact windows, menu items, and typed commands; say what *not* to do (e.g. "the Terminal is a plain shell — never type app commands into it"). |
| `bootstrap_click_text` | Vision-grounded description of where the executor's deterministic pre-loop click should land to engage the app (e.g. its command line). Omitted = viewer-centre click. |
| `done_hints.mouse` / `.kbd` | The literal next calls named in done-guard rejections when a run tries to finish without engaging that input modality. |
| `viewer_match` | Window-title fragment for the viewer the bootstrap should raise (default `TigerVNC`, or `CU_VNC_VIEWER_MATCH`). |

## Writing a good briefing

The pattern that works, learned the hard way:

1. **A session map.** List the windows on the remote desktop, where
   they are, and which ones are traps (a shell that looks like the
   app's command window is the classic one).
2. **Exact commands.** If the app has a command line, give the literal
   command text for each step, with placeholders filled in.
3. **Verification steps.** After each action, an `observe` and the
   exact window title or text that proves it worked.
4. **Topology, once, plainly.** The viewer is a live picture of a
   remote computer; everything inside its rectangle is that machine
   and is directly clickable; there is no other screen. Weak models
   get this wrong without being told.
5. **A short completion.** "Report the two editor titles exactly as
   observed" beats a paragraph — long conclusions overflow output
   limits and are discarded.

A complete worked example with fictional names ships at
`runbooks/eda-viewer.example.json` (an EDA-style app in a remote
session: command window, library/cell documents, read-only opens).

## Rules

- Packs are **data, not code**: JSON only, no executable content.
- Packs may contain site-specific names (your libraries, your apps) —
  that's their job. Keep packs with the site, not in shared repos you
  don't control; the executor itself must stay clean without them.
- A pack changes *guidance*, never *guarantees*: deadlines, receipts,
  done discipline, and the kill switch behave identically with or
  without one.

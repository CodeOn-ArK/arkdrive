"""Thunar facts and adapter-side verified batches; no worker mechanisms."""
import json
from pathlib import Path
import re

NAME = "thunar"
CLASSES = ("Thunar",)
PLAYBOOK = (
    "Files are exposed in the tree only in Details view (Ctrl+2); icon view shows no file items. "
    "Rows are table cells with empty names; the file name is in a child name cell. "
    "F2 with the name cell selected opens the rename dialog; commit with Return. "
    "The dialog is modal for the whole session. At about 27+ entries, rows below the viewport "
    "are virtualized and absent from the tree until scrolled into view. "
    'Inside a copy→paste interval, pin text steps via:"keys": auto text over 200 characters '
    "goes paste-first and overwrites the session clipboard. "
    "Thunar-specific "
    'Create-folder example (replace <file-manager> and <folder-list-ref> with the observed window handle and container ref): '
    '{"steps":[{"id":"1","target":"<file-manager>","intent":{"kind":"key","keys":["Control_L","Shift_L","n"]},"expect":{"kind":"dialog_present","match":{"title_re":"[Cc]reate.*[Ff]older"},"bind":{"dialog":"dialog","entry":{"role":"text"}}}},'
    '{"id":"2","target":{"from_step":"1","name":"entry"},"intent":{"kind":"text","text":"Reports","replace":true}},'
    '{"id":"3","target":{"from_step":"1","name":"dialog"},"intent":{"kind":"key","keys":["Return"]},"expect":{"kind":"item_present","container":"<folder-list-ref>","name":"Reports"}}]}. '
    'Step 1 verifies and binds the create dialog and its entry. Step 2 types into that bound entry; the worker supplies the strong whole-field text_equals expectation. Step 3 presses Return in the bound dialog and verifies the new item in the original folder listing. Bindings refer to verified earlier steps; do not invent refs for a dialog that has not appeared.'
)


def _shown(adapter, name, args):
    return json.loads(adapter.call(name, args)['content'][0]['text'])


def _observe(adapter):
    """A complete fresh tree page set, without scrolling virtualized rows."""
    result = _shown(adapter, 'observe', dict(full=True, scope=dict(kind='session'),
                                           channels=['state', 'tree']))
    if not result.get('settled') or result.get('resync_required'):
        return None, dict(status='observe_required')
    state = result.get('state', {})
    focus = state.get('focus')
    window = next((w for w in state.get('windows', []) if w['handle'] == focus), {})
    if window.get('class', '').casefold() not in [c.casefold() for c in CLASSES]:
        return None, dict(status='inactive_pack')
    if state.get('modal'):
        return None, dict(status='blocked', detail='session modal is present')
    tree = result.get('tree', {})
    if tree.get('window') != focus or tree.get('dirty'):
        return None, dict(status='listing_unavailable')
    lines = list(tree.get('lines', []))
    cursor, seen = tree.get('cursor'), set()
    while cursor:
        if cursor in seen:
            return None, dict(status='listing_unavailable', detail='repeated tree cursor')
        seen.add(cursor)
        page = _shown(adapter, 'observe', dict(cursor=cursor, channels=['state', 'tree'],
                      scope=dict(kind='window', window=focus)))
        more = page.get('tree', {})
        if (not page.get('settled') or page.get('resync_required') or
                page.get('rev') != result.get('rev') or more.get('rev') != tree.get('rev') or
                more.get('window') != focus or more.get('dirty') or
                page.get('state', {}).get('focus', focus) != focus or
                page.get('state', {}).get('modal')):
            return None, dict(status='listing_unavailable', detail='state changed during tree paging')
        lines.extend(more.get('lines', []))
        cursor = more.get('cursor')
    if tree.get('truncated') and not tree.get('cursor'):
        return None, dict(status='listing_unavailable', detail='tree continuation missing')
    return (focus, _listing(lines)), None


def _listing(lines):
    """Resolve the first named cell under each unnamed row in a tree_table.

    Use the JSON name verbatim (including escapes); metadata cells and other
    app chrome are excluded. Parentage is the lines/1 indentation contract.
    """
    stack, tables, items = [], [], []
    for line in lines:
        match = re.match(r'^( *)(n[0-9]+) ([a-z_]+) (".*)', line)
        if not match:
            continue
        indent, ref, role, tail = match.groups()
        try:
            name, _ = json.JSONDecoder().raw_decode(tail)
        except ValueError:
            continue
        depth = len(indent)
        while stack and stack[-1]['depth'] >= depth:
            stack.pop()
        parent = stack[-1] if stack else None
        table = next((n for n in reversed(stack) if n['role'] == 'tree_table'), None)
        node = dict(depth=depth, ref=ref, role=role, name=name)
        if role == 'tree_table':
            tables.append(ref)
        if (table and role == 'table_cell' and name and parent and
                parent['role'] == 'table_cell' and not parent['name'] and
                not parent.get('name_cell')):
            parent['name_cell'] = ref
            items.append(dict(name=name, ref=ref, row=parent['ref'], container=table['ref']))
        stack.append(node)
    return dict(tables=tables, items=items)


def _verified(result, ids):
    return (result.get('status') == 'success' and
            [s.get('id') for s in result.get('steps', [])] == ids and
            all(s.get('effect', {}).get('status') == 'verified' for s in result['steps']))


def _outcome(result):
    # Unknown/contradicted remains that status even after auto-reconciliation.
    # No receipt, action journal, or evidence escapes the skill projection.
    value = dict(status='unverified' if result.get('status') == 'success' else result.get('status', 'unknown'))
    if result.get('reason'):
        value['detail'] = str(result['reason'])[:240]
    return value


def _ready_listing(adapter):
    listing, error = _observe(adapter)
    if error:
        return None, error
    window, data = listing
    if not data['tables'] or not data['items']:
        result = _shown(adapter, 'act', dict(steps=[dict(id='details', target=window,
            intent=dict(kind='key', keys=['Control_L', '2']), timeout_ms=2000,
            expect=dict(kind='node_present', window=window,
                        selector=dict(provider='atspi', role='tree_table')))]))
        if not _verified(result, ['details']):
            return None, _outcome(result)
        listing, error = _observe(adapter)
        if error:
            return None, error
        window, data = listing
    if not data['tables']:
        return None, dict(status='listing_unavailable')
    return (window, data), None


def _valid(args, required):
    if not isinstance(args, dict) or set(args) - set(required) - {'dir'} or set(required) - set(args):
        return False
    for field in required:
        name = args[field]
        if (not isinstance(name, str) or not name or len(name) > 2000 or
                name in ('.', '..') or '/' in name or '\0' in name):
            return False
    return 'dir' not in args or (isinstance(args['dir'], str) and bool(args['dir']) and '\0' not in args['dir'])


def _oracle(adapter, args, name, old=None):
    directory = Path(args['dir']) if 'dir' in args else adapter.sd / 'home'
    try:
        return (directory / name).exists() and (old is None or not (directory / old).exists())
    except (OSError, ValueError):
        return False


def create_folder(adapter, args):
    if not _valid(args, ('name',)):
        return dict(status='invalid_arguments')
    listing, error = _ready_listing(adapter)
    if error:
        return error
    window, data = listing
    name = args['name']
    if any(item['name'] == name for item in data['items']):
        return dict(status='collision')
    steps = [dict(id='1', target=window, timeout_ms=2000,
                  intent=dict(kind='key', keys=['Control_L', 'Shift_L', 'n']),
                  expect=dict(kind='dialog_present', parent=window,
                              match=dict(title_re='[Cc]reate.*[Ff]older'),
                              bind=dict(dialog='dialog', entry=dict(role='text')))),
             dict(id='2', target=dict(from_step='1', name='entry'), timeout_ms=3500,
                  intent=dict(kind='text', text=name, replace=True)),
             dict(id='3', target=dict(from_step='1', name='dialog'), timeout_ms=3500,
                  intent=dict(kind='key', keys=['Return']),
                  expect=dict(kind='item_present', container=data['tables'][0], name=name))]
    result = _shown(adapter, 'act', dict(steps=steps))
    if not _verified(result, ['1', '2', '3']):
        return _outcome(result)
    return dict(status='success', verified=_oracle(adapter, args, name))


def rename(adapter, args):
    if not _valid(args, ('old', 'new')):
        return dict(status='invalid_arguments')
    listing, error = _observe(adapter)
    if error:
        return error
    window, data = listing
    matches = [item for item in data['items'] if item['name'] == args['old']]
    if not matches:
        return dict(status='not_found', candidates=[i['name'] for i in data['items'][:5]])
    if len(matches) != 1:
        return dict(status='ambiguous')
    if any(item['name'] == args['new'] for item in data['items']):
        return dict(status='collision')
    item = matches[0]
    steps = [dict(id='1', target=dict(window=window, ref=item['row']), timeout_ms=2000,
                  require=dict(selected_ref=item['ref']), intent=dict(kind='key', keys=['F2']),
                  expect=dict(kind='dialog_present', parent=window, role='rename',
                              bind=dict(dialog='dialog', entry=dict(role='text')))),
             dict(id='2', target=dict(from_step='1', name='entry'), timeout_ms=3500,
                  intent=dict(kind='text', text=args['new'], replace=True)),
             dict(id='3', target=dict(from_step='1', name='dialog'), timeout_ms=3500,
                  intent=dict(kind='key', keys=['Return']),
                  expect=dict(kind='all', of=[
                      dict(kind='dialog_absent', handle=dict(from_step='1', name='dialog')),
                      dict(kind='node_present', window=window, selector=dict(provider='atspi',
                           role='table_cell', name=args['new']))]))]
    result = _shown(adapter, 'act', dict(steps=steps))
    if not _verified(result, ['1', '2', '3']):
        return _outcome(result)
    return dict(status='success', verified=_oracle(adapter, args, args['new'], args['old']))


def _params(required):
    return dict(type='object', properties={key: dict(type='string') for key in (*required, 'dir')},
                required=list(required), additionalProperties=False)


SKILLS = {
    'thunar.create_folder': dict(params=_params(('name',)),
        summary='Create a folder after fresh listing/collision checks; verify on the filesystem.', run=create_folder),
    'thunar.rename': dict(params=_params(('old', 'new')),
        summary='Rename a unique visible entry through a bound modal dialog; verify on the filesystem.', run=rename),
}

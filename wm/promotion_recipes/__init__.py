"""Closed, deterministic promotion recipes. No caller supplied launch commands."""
from . import text_editor, thunar

RECIPES = {text_editor.APP_CLASS: text_editor, thunar.APP_CLASS: thunar}


class PromotionError(Exception):
    pass


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ('title', 'name', 'value', 'text', 'description', 'uri', 'path'):
                yield from strings(item)
            elif isinstance(item, (dict, list)):
                yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def editor_title_path(title, home):
    """GNOME Text Editor's saved-file title, using the owning worker's HOME."""
    from pathlib import Path
    import re
    match = re.fullmatch(r'([^\0/\n]+) \(([^\0\n]+)\) - Text Editor', title)
    if match is None:
        return None
    name, parent = match.groups()
    if parent == '~':
        parent = str(home)
    elif parent.startswith('~/'):
        parent = str(Path(home) / parent[2:])
    if not parent.startswith('/'):
        return None
    return Path(parent) / name


def saved_path(window, tree, home, directory=False):
    from pathlib import Path
    from urllib.parse import unquote, urlparse
    import re
    evidence = list(strings([window, tree]))
    if unsaved(window, tree):
        raise PromotionError('unsaved_state')
    candidates = set()
    for item in evidence:
        if not directory:
            path = editor_title_path(item, home)
            if path is not None and path.is_file():
                candidates.add(str(path))
        # Titles commonly use “filename — /directory — Text Editor”. Only
        # paths actually exposed by the window/tree are considered.
        for part in [item, *re.split(r'\s+[—–]\s+|\n', item)]:
            part = part.strip()
            if part.startswith('file://'):
                uri = urlparse(part)
                if uri.netloc not in ('', 'localhost'):
                    continue
                part = unquote(uri.path)
            if part.startswith('~/'):
                part = str(Path(home) / part[2:])
            if part.startswith('/') and '\0' not in part:
                path = Path(part)
                if (path.is_dir() if directory else path.is_file()):
                    candidates.add(str(path))
                elif not directory and path.is_dir():
                    candidates.add(str(path))
    if not directory:
        for item in evidence:
            name = re.split(r'\s+[—–]\s+', item)[0].strip()
            if '/' not in name and name:
                for parent in list(candidates):
                    path = Path(parent) / name
                    if path.is_file():
                        candidates.add(str(path))
        candidates = {p for p in candidates if Path(p).is_file()}
    candidates = {p for p in candidates if visible(window, None, p, home)}
    if len(candidates) != 1:
        raise PromotionError('document_path_unavailable')
    path = Path(candidates.pop())
    if '..' in path.parts or not path.is_relative_to(Path(home)):
        raise PromotionError('document_outside_home')
    return str(path)


def unsaved(window, tree):
    import re
    indicators = r'(^\s*\*|\*\s*$|[•●]|\b(unsaved|untitled|new document)\b|\(modified\))'
    if re.search(indicators, window.get('title', ''), re.I):
        return True
    def flagged(node):
        if isinstance(node, dict):
            if any(node.get(k) is True for k in ('unsaved', 'modified', 'dirty')):
                return True
            if any(s in ('unsaved', 'modified', 'dirty') for s in node.get('states', [])):
                return True
            if node.get('role') in ('frame', 'window', 'document', 'page_tab', 'tab', 'label'):
                if re.search(indicators, node.get('name', ''), re.I):
                    return True
            return any(flagged(v) for v in node.values() if isinstance(v, (list, dict)))
        if isinstance(node, list):
            return any(flagged(v) for v in node)
        return False
    return flagged(tree)


def visible(window, tree, path, home=None):
    from pathlib import Path
    import re
    name = Path(path).name
    for text in strings([window, tree]):
        title_path = editor_title_path(text, Path.home() if home is None else home)
        if title_path is not None:
            if title_path == Path(path):
                return True
            continue
        if name == text or name in re.split(r'\s+[—–-]\s+|\n', text) or path in text:
            return True
    return False

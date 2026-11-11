"""Discover trusted adapter knowledge packs. Stdlib only, no desktop imports.

A module exports NAME, CLASSES, PLAYBOOK and SKILLS. Each skill has a closed
JSON-schema params dict, a one-line summary and run(adapter, args). Registry
failures are diagnostics, never an adapter startup failure. Sorted discovery
also makes overlapping class matches deterministic (first pack wins).
"""
import importlib
import json
import pkgutil

AUTHORITY_FIELDS = frozenset(('risk', 'allow_stale_pixels', 'lease', 'base_rev',
                              'control_epoch', 'action_id', 'generation', 'session'))


def check_schema(value):
    if isinstance(value, dict):
        if AUTHORITY_FIELDS.intersection(value.get('properties', {})):
            raise ValueError('authority field in pack schema')
        for child in value.values():
            check_schema(child)
    elif isinstance(value, list):
        for child in value:
            check_schema(child)


class Registry:
    def __init__(self, package=__name__):
        self.packs, self.skills, self.diagnostics = [], {}, []
        try:
            root = importlib.import_module(package)
            names = sorted(module.name for module in pkgutil.iter_modules(root.__path__)
                           if not module.name.startswith('_') and not module.ispkg)
        except (Exception, SystemExit) as exc:
            self.diagnostics.append(dict(module=package, error=type(exc).__name__))
            return
        for name in names:
            module_name = package + '.' + name
            try:
                pack = importlib.import_module(module_name)
                if (not isinstance(pack.NAME, str) or not pack.NAME or
                        not isinstance(pack.CLASSES, tuple) or not pack.CLASSES or
                        not all(isinstance(c, str) and c for c in pack.CLASSES) or
                        not isinstance(pack.PLAYBOOK, str) or not isinstance(pack.SKILLS, dict)):
                    raise ValueError('invalid pack contract')
                if any(p.NAME == pack.NAME for p in self.packs):
                    raise ValueError('duplicate pack name')
                for skill_name, skill in pack.SKILLS.items():
                    if (not isinstance(skill_name, str) or not skill_name or skill_name in self.skills or
                            not isinstance(skill, dict) or not callable(skill.get('run')) or
                            not isinstance(skill.get('summary'), str) or not skill['summary'] or
                            any(c in skill['summary'] for c in '\r\n') or
                            not isinstance(skill.get('params'), dict) or
                            skill['params'].get('type') != 'object' or
                            skill['params'].get('additionalProperties') is not False):
                        raise ValueError('invalid skill contract')
                    params = skill['params']
                    properties, required = params.get('properties', {}), params.get('required', [])
                    if (not isinstance(properties, dict) or
                            not all(isinstance(n, str) for n in properties) or
                            not isinstance(required, list) or
                            not all(isinstance(n, str) and n in properties for n in required)):
                        raise ValueError('invalid params schema')
                    json.dumps(params, allow_nan=False)
                    check_schema(params)
                # Publish atomically: an invalid skill invalidates only its pack.
                self.packs.append(pack)
                self.skills.update((n, (pack, s)) for n, s in pack.SKILLS.items())
            except (Exception, SystemExit) as exc:
                self.diagnostics.append(dict(module=module_name, error=type(exc).__name__))

    def for_class(self, wm_class):
        if not isinstance(wm_class, str):
            return None
        return next((p for p in self.packs if wm_class.casefold() in
                     (c.casefold() for c in p.CLASSES)), None)

    def description(self):
        lines = ['Run a named skill for the focused app. Skills use fresh observations and '
                 'the same lease, revision and reconciliation as act.']
        for name, (_, skill) in self.skills.items():
            params = skill['params']
            names = [n + ('' if n in params.get('required', []) else '?')
                     for n in params.get('properties', {})]
            lines.append(name + '(' + ', '.join(names) + '): ' + skill['summary'])
        return '\n'.join(lines)


REGISTRY = Registry()

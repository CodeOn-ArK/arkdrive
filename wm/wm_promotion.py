"""Durable save/export → spawn → reopen → verify → retire controller operation.

External inputs are armed once. An armed but unacknowledged input is observed,
never replayed. The checkpoint survives both success and failure.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

from promotion_recipes import RECIPES, PromotionError
from wm_registry import atomic_json, durable_mkdir, sync_directory


def destination_parent(path):
    """Walk destination directories with openat; never traverse a symlink."""
    path = Path(path).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if part == '..':
                raise PromotionError('invalid_destination')
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_tree(source, destination, limits):
    """Copy without following links, including ancestor links; bounded snapshot."""
    source, destination = Path(source), Path(destination)
    fd = os.open(source.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in source.parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    entries, total = [], [0, 0]

    def visit(descriptor, target, relative):
        info = os.fstat(descriptor)
        total[1] += 1
        if total[1] > limits['artifact_count']:
            raise PromotionError('artifact_quota')
        if stat.S_ISDIR(info.st_mode):
            parent = destination_parent(target)
            try:
                os.mkdir(target.name, mode=0o700, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
            entries.append(dict(path=relative, kind='directory'))
            for name in sorted(os.listdir(descriptor)):
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    visit(child, target / name, relative + '/' + name)
                finally:
                    os.close(child)
            sync_directory(target)
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            parent = destination_parent(target)
            try:
                out = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=parent)
            finally:
                os.close(parent)
            with os.fdopen(out, 'wb') as stream:
                while True:
                    block = os.read(descriptor, min(65536, limits['artifact_bytes'] - total[0] + 1))
                    if not block:
                        break
                    total[0] += len(block)
                    if total[0] > limits['artifact_bytes']:
                        raise PromotionError('artifact_quota')
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            entries.append(dict(path=relative, kind='file', sha256=digest.hexdigest()))
            sync_directory(target.parent)
        else:
            raise PromotionError('artifact_not_regular')
    try:
        visit(leaf, destination, destination.name)
    finally:
        os.close(leaf)
    return entries


def validate_files(root, manifest):
    for item in manifest['files']:
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise PromotionError('invalid_checkpoint')
        path = root / 'documents' / relative
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
            raise PromotionError('invalid_checkpoint')
        if item['kind'] == 'directory':
            if not path.is_dir():
                raise PromotionError('invalid_checkpoint')
        elif hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise PromotionError('invalid_checkpoint')


class Backend:
    """Session-scoped worker sensing and argv-only app/clipboard operations."""
    def __init__(self, controller):
        self.c = controller

    def observe(self, record):
        from wm_controller import local_call
        response = local_call(Path(record['session_dir']) / 'driver/act.sock', 'promotion.observe', {}, timeout=3)
        result = response.get('result', {})
        if result.get('incarnation') != record['incarnation'] or not result.get('settled'):
            raise PromotionError('source_not_settled')
        return result

    def settled(self, record):
        deadline = time.monotonic() + 5
        while True:
            try:
                return self.observe(record)
            except PromotionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def run(self, record, argv, **kwargs):
        from wm_controller import sessiond
        if type(record.get('display')) is not int or not 91 <= record['display'] <= 120:
            raise PromotionError('unsafe_display')
        result = subprocess.run(argv, env=sessiond.session_env(record), timeout=kwargs.pop('timeout', 5),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        if result.returncode:
            raise PromotionError('app_command_failed')
        return result.stdout

    def start_worker(self, record):
        from wm_controller import WM
        raw = self.run(record, [sys.executable, str(WM / 'ark-worker'), 'start',
                               '--session', record['session']], timeout=25)
        result = json.loads(raw)
        if result.get('session') != record['session'] or result.get('state') != 'ready':
            raise PromotionError('child_worker_failed')

    def clipboard_read(self, record):
        data = self.run(record, ['xclip', '-selection', 'clipboard', '-out'])
        if len(data) > 65536:
            raise PromotionError('clipboard_quota')
        return data.decode('utf-8')

    def clipboard_set(self, record, text):
        self.run(record, ['xclip', '-selection', 'clipboard', '-in'], input=text.encode('utf-8'))

    def reopen(self, record, manifest, root):
        from wm_controller import sessiond
        if type(record.get('display')) is not int or not 91 <= record['display'] <= 120:
            raise PromotionError('unsafe_display')
        with (Path(record['session_dir']) / 'logs/promotion.log').open('ab') as stream:
            process = subprocess.Popen(manifest['launch_argv'], cwd=root / manifest['cwd'],
                                       env=sessiond.session_env(record), stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=stream, start_new_session=True)
            self.c.app_processes.append(process)

    def retire(self, record, window):
        # WM_DELETE_WINDOW targets precisely the observed window, never the app
        # PID (a process can own unrelated windows/documents).
        self.run(record, ['wmctrl', '-ic', hex(window['xid'])])


class Promotion:
    def __init__(self, controller, intent):
        self.c, self.intent = controller, intent
        self.p = intent['params']
        self.id = intent['promotion_id']
        self.bundle = controller.state / 'exchange/promotions' / self.id
        self.backend = controller.promotion_backend
        self.recipe = RECIPES[self.p['app_class']]
        self.steps = {}
        for row in controller.log.rows:
            if row.get('promotion_id') == self.id and row['type'] == 'promotion.step':
                self.steps[row['step']] = row

    def mark(self, step, status, **fields):
        row = self.c.log.append('promotion.step', promotion_id=self.id, step=step, status=status, **fields)
        self.steps[step] = row
        self.c.crash('promotion.' + step + '.' + status)
        return row

    def step(self, name, action, reconcile=None):
        if name != 'ungate' and self.c.kill_file.exists():
            raise PromotionError('global_kill')
        prior = self.steps.get(name)
        if prior and prior['status'] == 'done':
            return prior.get('value')
        if prior:
            if reconcile is None:
                raise PromotionError(name + '_uncertain')
            value = reconcile()
        else:
            self.mark(name, 'armed')
            value = action()
            self.c.crash('promotion.' + name + '.effect')
        self.mark(name, 'done', value=value)
        return value

    def record(self, incarnation):
        from wm_controller import sessiond
        with sessiond.locked_registry() as records:
            return copy.deepcopy(self.c.record(records, incarnation))

    def window(self, observation):
        return next((w for w in observation['windows'] if
                     w['handle'] == self.p['window'] and w.get('mapped') and
                     w['class'] == self.p['app_class']), None)

    def source_ready(self):
        from wm_controller import sessiond
        with sessiond.locked_registry() as records:
            source = self.c.record(records, self.p['source_incarnation'])
            with self.c.parent_gate(source):
                return copy.deepcopy(source)

    def gate(self):
        from wm_controller import sessiond, Refused
        with sessiond.locked_registry() as records:
            source = self.c.record(records, self.p['source_incarnation'])
            with self.c.parent_gate(source):
                path = Path(source['session_dir']) / 'promotion-gate.json'
                if path.exists() and json.loads(path.read_text())['promotion_id'] != self.id:
                    raise Refused('promotion_busy')
                if not path.exists():
                    before = self.backend.settled(source)
                    window = self.window(before)
                    if window is None:
                        raise PromotionError('source_window_missing')
                    atomic_json(path, dict(promotion_id=self.id, window=self.p['window'],
                                           family=window.get('family'), xid=window['xid']))
        observation = self.backend.settled(source)
        window = self.window(observation)
        if window is None:
            raise PromotionError('source_window_missing')
        # Repeat refusal after the gate: preflight and gate can race an edit.
        self.recipe.export(window, observation.get('trees', {}).get(window['handle']), source['home'])
        return dict(observation, windows=[window],
                    trees={window['handle']: observation.get('trees', {}).get(window['handle'])})

    def ungate(self):
        try:
            source = self.record(self.p['source_incarnation'])
        except Exception as exc:
            from wm_controller import Refused
            if isinstance(exc, Refused) and exc.reason == 'unknown_incarnation':
                return
            raise
        path = Path(source['session_dir']) / 'promotion-gate.json'
        if path.exists() and json.loads(path.read_text())['promotion_id'] == self.id:
            path.unlink()
            sync_directory(path.parent)

    def checkpoint(self):
        try:
            return self.read_checkpoint()
        except (KeyError, TypeError, ValueError) as exc:
            raise PromotionError('invalid_checkpoint') from exc

    def read_checkpoint(self):
        path = self.bundle / 'manifest.json'
        manifest = json.loads(path.read_text())
        document = 'documents/' + Path(manifest['source_path']).name
        if (manifest['version'] != 1 or manifest['promotion_id'] != self.id or
                manifest['app_class'] != self.p['app_class'] or
                manifest['source_incarnation'] != self.p['source_incarnation'] or
                manifest['source_window']['handle'] != self.p['window'] or
                manifest['cwd'] != '.' or manifest['document_paths'] != [document] or
                manifest['launch_argv'] != [self.recipe.COMMAND, document] or
                ('clipboard_payload' in manifest) != self.p.get('transfer_clipboard', False)):
            raise PromotionError('invalid_checkpoint')
        validate_files(self.bundle, manifest)
        return manifest

    def export(self, observation):
        if (self.bundle / 'manifest.json').exists():
            return self.checkpoint()
        source = self.source_ready()
        window = self.window(observation)
        path = self.recipe.export(window, observation.get('trees', {}).get(window['handle']), source['home'])
        durable_mkdir(self.bundle)
        documents = self.bundle / 'documents'
        # Partial snapshots are not reused as good checkpoints.
        if documents.exists():
            raise PromotionError('incomplete_checkpoint')
        files = copy_tree(path, documents / Path(path).name, self.c.cfg)
        relative = 'documents/' + Path(path).name
        manifest = dict(version=1, promotion_id=self.id, app_class=self.p['app_class'],
                        source_incarnation=source['incarnation'], source_window=window,
                        source_path=path, launch_argv=[self.recipe.COMMAND, relative], cwd='.',
                        document_paths=[relative], files=files)
        if self.p.get('transfer_clipboard', False):
            manifest['clipboard_payload'] = self.backend.clipboard_read(source)
        atomic_json(self.bundle / 'manifest.json', manifest)
        return manifest

    def spawn(self):
        source = self.source_ready()
        result = self.c.submit('spawn', dict(request_id='promotion-spawn:' + self.id,
                              parent_incarnation=source['incarnation'], profile=source['profile'],
                              task_text='Reopen promotion ' + self.id, origin='parent-agent',
                              promotion_id=self.id, reservation=self.intent['reservation']))
        if not result['ok']:
            raise PromotionError(result['reason'])
        return result['child_incarnation']

    def install(self, child, manifest):
        root = Path(child['home']) / 'Promotions' / self.id
        if root.exists():
            validate_files(root, manifest)
            return str(root)
        temporary = root.with_name(root.name + '.partial')
        parent = destination_parent(root)
        try:
            try:
                os.stat(temporary.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                shutil.rmtree(temporary.name, dir_fd=parent)
            os.mkdir(temporary.name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
            copy_tree(self.bundle / manifest['document_paths'][0],
                      temporary / manifest['document_paths'][0], self.c.cfg)
            validate_files(temporary, manifest)
            os.rename(temporary.name, root.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        return str(root)

    def verify(self, child, manifest, root):
        validate_files(root, manifest)  # filesystem oracle in the child's HOME
        observation = self.backend.settled(child)
        document = str(root / manifest['document_paths'][0])
        return any(self.recipe.verify(w, observation.get('trees', {}).get(w['handle']), document)
                   for w in observation['windows'] if w.get('mapped'))

    def verified(self, child, manifest, root):
        deadline = time.monotonic() + 5
        while True:
            if self.verify(child, manifest, root):
                return True
            if time.monotonic() >= deadline:
                raise PromotionError('verify_failed')
            time.sleep(.05)

    def reconcile_clipboard(self, child, manifest):
        if self.backend.clipboard_read(child) != manifest['clipboard_payload']:
            raise PromotionError('clipboard_uncertain')

    def retire(self, source, manifest, reconcile=False):
        observation = self.backend.settled(source)
        window = self.window(observation)
        if window is None:
            if self.source_present(observation, manifest['source_window']):
                raise PromotionError('source_identity_changed')
            return True
        if reconcile:
            raise PromotionError('retire_uncertain')
        if any(window.get(k) != manifest['source_window'].get(k)
               for k in ('handle', 'xid', 'family', 'class', 'title', 'app_build')):
            raise PromotionError('source_changed')
        path = self.recipe.export(window, observation.get('trees', {}).get(window['handle']), source['home'])
        if path != manifest['source_path']:
            raise PromotionError('source_changed')
        self.backend.retire(source, window)
        self.c.crash('promotion.retire.dispatched')
        deadline = time.monotonic() + 5
        while True:
            sample = self.backend.settled(source)
            if not self.source_present(sample, manifest['source_window']):
                return True
            if self.window(sample) is None:
                raise PromotionError('source_identity_changed')
            if time.monotonic() >= deadline:
                raise PromotionError('retire_failed')
            time.sleep(.05)

    def source_present(self, observation, previous):
        # A worker restart gives an existing native window a new handle. Its
        # old handle disappearing alone is never proof the app was closed.
        return any(w.get('class') == self.p['app_class'] and
                   (w['handle'] == self.p['window'] or w.get('xid') == previous['xid'] or
                    previous.get('family') and w.get('family') == previous['family'])
                   for w in observation['windows'])

    def cleanup(self):
        from wm_controller import sessiond
        # Include a spawn whose result never made it into our step row.
        spawn = self.c.intents.get('promotion-spawn:' + self.id)
        if spawn:
            self.mark('reap', 'armed')
            result, code = sessiond.stop_sessions(None, incarnation=spawn['child_incarnation'])
            if code:
                raise OSError(result['error'])
            if spawn['request_id'] not in self.c.completed:
                self.c.complete(spawn, dict(ok=False, reason='promotion_failed'))
            self.mark('reap', 'done')
        self.step('ungate', self.ungate, self.ungate)

    def run(self):
        from wm_controller import Refused
        failure = self.steps.get('failure')
        if failure:
            self.cleanup()
            return dict(ok=False, promotion_id=self.id, reason=failure['reason'])
        try:
            if self.steps.get('retire', {}).get('status') == 'done':
                return self.finish(self.steps['spawn']['value'])
            observation = self.step('quiesce', self.gate, self.gate)
            manifest = self.step('export', lambda: self.export(observation), self.checkpoint)
            if self.checkpoint() != manifest:
                raise PromotionError('invalid_checkpoint')
            child_id = self.step('spawn', self.spawn, self.spawn)
            child = self.record(child_id)
            self.step('worker', lambda: self.backend.start_worker(child),
                      lambda: self.backend.settled(child))
            root = Path(self.step('transfer', lambda: self.install(child, manifest),
                                  lambda: self.install(child, manifest)))
            if 'clipboard_payload' in manifest:
                self.step('clipboard', lambda: self.backend.clipboard_set(child, manifest['clipboard_payload']),
                          lambda: self.reconcile_clipboard(child, manifest))
            self.step('reopen', lambda: self.backend.reopen(child, manifest, root),
                      lambda: self.verified(child, manifest, root))
            self.step('verify', lambda: self.verified(child, manifest, root),
                      lambda: self.verified(child, manifest, root))
            source = self.source_ready()
            # Revalidate the child even when recovering an already verified step.
            self.verified(child, manifest, root)
            self.step('retire', lambda: self.retire(source, manifest),
                      lambda: self.retire(source, manifest, reconcile=True))
            return self.finish(child_id)
        except (PromotionError, Refused, OSError, ValueError, subprocess.SubprocessError) as exc:
            if self.c.log.failed:
                raise
            retirement = self.steps.get('retire')
            if retirement:
                if retirement['status'] == 'done':
                    raise  # Retirement committed: keep child; retry finalization.
                # A close can take effect even if its acknowledgement was lost.
                # Keep both sides until sensing can determine which survived.
                try:
                    source = self.record(self.p['source_incarnation'])
                    previous = self.steps['export']['value']['source_window']
                    absent = not self.source_present(self.backend.settled(source), previous)
                except (PromotionError, Refused, OSError):
                    raise OSError('retire_outcome_unavailable') from exc
                if absent:
                    self.mark('retire', 'done', value=True)
                    return self.finish(self.steps['spawn']['value'])
            reason = getattr(exc, 'reason', str(exc))
            self.mark('failure', 'done', reason=reason)
            self.cleanup()
            return dict(ok=False, promotion_id=self.id, reason=reason)

    def finish(self, child_id):
        self.step('ungate', self.ungate, self.ungate)
        return dict(ok=True, promotion_id=self.id, child_incarnation=child_id,
                    checkpoint=str(self.bundle / 'manifest.json'))

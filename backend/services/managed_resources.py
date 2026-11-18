"""Single-process admission/storage guards, independent of model billing.

Only managed visitor mode opts in. Filesystem bytes (including old versions) are
the source of truth; reservations are temporary and never a monetary ledger.
"""
import json
import os
import shutil
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from flask import current_app, g, has_request_context

DEFAULTS = dict(request_window_seconds=60, visitor_requests=6, global_requests=20,
                service_storage_bytes=10 * 2**30, storage_free_bytes=5 * 2**30)
_storage_lock = threading.RLock()
_rate_lock = threading.Lock()
_pending = {}
_created = ContextVar('pc_created_outputs', default=None)


class ResourceLimit(ValueError):
    """A local refusal: no implicit retry and no fee settlement."""


def active():
    from services.visitor_access import enabled
    return enabled()


def request_allowed():
    """Called once after replay detection, never from per-page provider calls."""
    from services.visitor_access import identity, limits
    cap = limits()
    now = time.monotonic()
    with _rate_lock:
        events = current_app.extensions.setdefault('pc_request_frequency', deque())
        while events and events[0][0] <= now - cap['request_window_seconds']:
            events.popleft()
        if (len(events) >= cap['global_requests'] or
                sum(who == identity() for _, who in events) >= cap['visitor_requests']):
            return False
        events.append((now, identity()))
    return True


def _root():
    return Path(current_app.config['UPLOAD_FOLDER']).resolve()


def _owners(root):
    path = root.parent / 'managed-file-owners.json'
    return json.loads(path.read_text(encoding='utf8')) if path.exists() else {}


def _remember(root, path, who):
    """Standalone uploads may not yet have a committed ORM row. Keep attribution.

    The index is not a size counter. Missing files contribute zero after deletion
    or a crash; project/legacy ownership is also obtained from the existing ORM.
    """
    owners = _owners(root)
    owners[path.relative_to(root).as_posix()] = who
    dest = root.parent / 'managed-file-owners.json'
    tmp = dest.with_suffix('.tmp')
    try:
        with tmp.open('w', encoding='utf8') as out:
            json.dump(owners, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def usage():
    """Unique regular files: uploads, images, versions, exports, task copies.

    Backups/logs outside UPLOAD_FOLDER are excluded, but disk free includes them.
    Orphans in this dedicated managed root count globally, never silently vanish.
    Caller holds _storage_lock when making admission decisions.
    """
    from services.visitor_access import identity
    from models import Project, UserTemplate, Material, ReferenceFile
    root = _root()
    roots = [root / p.id for p in Project.query.all()]
    roots += [root / 'user-templates' / t.id for t in UserTemplate.query.all()]
    own = {root / m.relative_path for m in Material.query.all()}
    own |= {root / f.file_path for f in ReferenceFile.query.all()}
    own |= {root / name for name, who in _owners(root).items() if who == identity()}
    temps = {r['temp'] for r in _pending.values() if r['root'] == root}
    seen, total, visitor = set(), 0, 0
    for path in root.rglob('*'):
        if path in temps or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += stat.st_size
        if path in own or any(path.is_relative_to(folder) for folder in roots):
            visitor += stat.st_size
    return visitor, total


def _check(extra, *, omit=None):
    from services.visitor_access import identity, limits
    cap, root = limits(), _root()
    visitor, total = usage()
    pending = [v for k, v in _pending.items() if k != omit and v['root'] == root]
    reserved = sum(v['size'] for v in pending)
    own_reserved = sum(v['size'] for v in pending if v['who'] == identity())
    # Keep the old destination until atomic replacement: conservative peak usage.
    admin = current_app.extensions.get('pc_admin')
    floor = max(cap['storage_free_bytes'], int(admin.base['resources']['disk_gib'] * 2**30) if admin else 0)
    if (visitor + own_reserved + extra > cap['storage_bytes'] or
            total + reserved + extra > cap['service_storage_bytes'] or
            shutil.disk_usage(root).free - reserved - extra < floor):
        raise ResourceLimit('存储空间不足，请减少文件或联系管理员；已有作品仍可下载。')


@contextmanager
def image_capacity():
    """Before a model image request reserve 32 MiB until its save completes."""
    if not active():
        yield
        return
    from services.visitor_access import identity
    token = uuid.uuid4().hex
    try:
        with _storage_lock:
            _check(32 * 2**20)
            _pending[token] = dict(root=_root(), who=identity(), size=32 * 2**20, temp=None)
        yield
    finally:
        with _storage_lock:
            _pending.pop(token, None)


@contextmanager
def new_files_transaction():
    """Rollback newly published files if the associated ORM save fails."""
    files = []
    mark = _created.set(files)
    try:
        yield
    except BaseException:
        for path in reversed(files):
            path.unlink(missing_ok=True)
        raise
    finally:
        _created.reset(mark)


@contextmanager
def _output(destination, estimate):
    """Reserve before writing; publish only a complete, quota-checked temp file.

    Preserve the filename suffix for Pillow/export libraries. On every unwind,
    including BaseException, remove only this attempt's unpublished temporary.
    """
    if not active():
        path = Path(destination)
        temp = path.with_name(f'.pc-{uuid.uuid4().hex}{path.suffix}')
        try:
            yield temp
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return
    from services.visitor_access import identity
    root = _root()
    path = Path(destination).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ResourceLimit('文件保存位置无效。')
    path = path.resolve()
    if not identity() or not path.is_relative_to(root):
        raise ResourceLimit('文件保存位置或访客身份无效。')
    token = uuid.uuid4().hex
    temp = path.with_name(f'.pc-{token}{path.suffix}')
    try:
        with _storage_lock:
            _check(estimate)
            _pending[token] = dict(root=root, who=identity(), size=estimate, temp=temp)
        yield temp
        with _storage_lock:
            size = temp.stat().st_size
            # Physical temp bytes already use disk; _check remains deliberately
            # conservative about the free-space floor at commit.
            _check(size, omit=token)
            _remember(root, path, identity())
            existed = path.exists()
            os.replace(temp, path)
            if not existed and _created.get() is not None:
                _created.get().append(path)
    except ResourceLimit as exc:
        if has_request_context():
            g.pc_resource_error = str(exc)
        raise
    finally:
        with _storage_lock:
            try:
                temp.unlink(missing_ok=True)
            finally:
                _pending.pop(token, None)


@contextmanager
def output(destination, estimate):
    from services.visitor_access import heavy_work
    try:
        with heavy_work(), _output(destination, estimate) as temp:
            yield temp
    except ResourceLimit as exc:
        if has_request_context():
            g.pc_resource_error = str(exc)
        raise


def save_upload(file, destination, validate=None):
    from services.visitor_access import limits
    estimate = limits()['upload_bytes'] if active() else 0
    with output(destination, estimate) as temp:
        file.save(str(temp))
        if validate:
            validate(temp)


def save_image(image, destination, image_format=None, **options):
    # Raw pixels plus encoder overhead; check actual output before publishing.
    estimate = image.width * image.height * max(4, len(image.getbands())) * 2 + 65536
    with output(destination, estimate) as temp:
        image.save(str(temp), format=image_format, **options)


def copy_file(source, destination):
    with output(destination, Path(source).stat().st_size) as temp:
        shutil.copyfile(source, temp)

"""Standalone Windows Python 3.13 GUI installer. Only the standard library is needed."""
from contextlib import closing, contextmanager, nullcontext, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
REPO = 'Mihail9990/Simylator'
CONTRACT = {'format': 1, 'backend': 'paper_mt5_quotes_v1', 'state_schema': 1, 'ledger_contract': 'deal_ledger_v1', 'scope_contract': 'cycle_scope_v1', 'python': '3.13'}
REQUIREMENTS = ['MetaTrader5==5.0.6231; sys_platform == "win32"', 'requests>=2.31,<3']
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
SHA = re.compile('[0-9a-f]{40}')

class SetupError(Exception):
    pass

def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        raw = f.read(65537)
    if len(raw) > 65536:
        raise SetupError('Слишком большой файл настроек.')
    return json.loads(raw)

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                f.write(b'0')
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SetupError('Другой сервис или установщик уже работает.') from None
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SetupError('Другой сервис или установщик уже работает.') from None
        acquired = True
        yield
    finally:
        if acquired and os.name == 'nt':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()

def download(url, limit=MAX_DOWNLOAD):
    req = urllib.request.Request(url, headers={'User-Agent': 'TradFi-Windows-Installer', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        from urllib.parse import urlsplit
        end = urlsplit(r.url)
        if end.scheme != 'https' or end.hostname not in {'api.github.com', 'github.com', 'codeload.github.com'}:
            raise SetupError('Неожиданный источник загрузки.')
        if int(r.headers.get('Content-Length', '0')) > limit:
            raise SetupError('Архив слишком большой.')
        data = bytearray()
        deadline = time.monotonic() + 120
        while True:
            chunk = r.read(min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit or time.monotonic() > deadline:
                raise SetupError('Превышен лимит загрузки.')

def latest_sha(fetch=download):
    data = json.loads(fetch(f'https://api.github.com/repos/{REPO}/git/ref/heads/main', 65536))
    if not isinstance(data, dict) or data.get('ref') != 'refs/heads/main' or (not isinstance(data.get('object'), dict)) or (data['object'].get('type') != 'commit'):
        raise SetupError('GitHub не вернул ссылку на ветку main.')
    sha = data['object'].get('sha', '')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise SetupError('GitHub не вернул корректный SHA ветки main.')
    return sha

def extract_archive(blob, destination, sha):
    """Validate the entire central directory before writing any member."""
    if len(blob) > MAX_DOWNLOAD:
        raise SetupError('Архив слишком большой.')
    root = f'Simylator-{sha}'
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        infos = z.infolist()
        total = 0
        seen = set()
        planned = []
        if len(infos) > 3000:
            raise SetupError('Слишком много файлов в архиве.')
        for info in infos:
            raw = info.filename
            if '\\' in raw or '\x00' in raw:
                raise SetupError('Недопустимый путь в архиве.')
            parts = raw.rstrip('/').split('/')
            if not parts or parts[0] != root or any((p in ('', '.', '..') for p in parts)):
                raise SetupError('Недопустимый путь в архиве.')
            for p in parts:
                if ':' in p or p.endswith((' ', '.')) or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1, 10)], *[f'LPT{i}' for i in range(1, 10)]}:
                    raise SetupError('Недопустимое имя Windows в архиве.')
            mode = info.external_attr >> 16 & 65535
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                raise SetupError('Ссылки и специальные файлы запрещены.')
            key = '/'.join(parts).casefold()
            if key in seen:
                raise SetupError('Повторяющийся путь в архиве.')
            seen.add(key)
            total += info.file_size
            if total > MAX_EXPANDED or info.file_size > 8 * 1024 * 1024 or info.file_size > max(info.compress_size, 1) * 200:
                raise SetupError('Превышены лимиты распаковки.')
            if len(parts) > 1:
                planned.append((info, Path(destination).joinpath(*parts[1:])))
        for info, path in planned:
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, path.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 65536)

def validate_release(code):
    if not (code / 'managed_release.json').is_file():
        raise SetupError('Эта версия main ещё не поддерживает установщик. Дождитесь публикации совместимой версии.')
    if read_json(code / 'managed_release.json') != CONTRACT:
        raise SetupError('Версия несовместима с этим установщиком. Скачайте новый установщик.')
    lines = [x.strip() for x in (code / 'requirements-tradfi-mt5.txt').read_text().splitlines() if x.strip() and (not x.lstrip().startswith('#'))]
    if lines != REQUIREMENTS:
        raise SetupError('Зависимости изменились: требуется проверенный новый установщик.')
    for name in ('tradfi_main.py', 'tradfi/runtime.py', 'tradfi/managed.py', 'tradfi_paper_config.example.json'):
        if not (code / name).is_file():
            raise SetupError('Архив неполный.')

def validate_instance(instance):
    if instance != 'paper':
        raise SetupError('Неизвестный экземпляр сервиса.')
    return instance

def binding_path(root, instance='paper'):
    validate_instance(instance)
    return Path(root) / 'data' / 'paper-state-binding.json'

def state_path(root, config):
    p = Path(config.get('state_file', 'state.sqlite3'))
    return p.resolve() if p.is_absolute() else (root / 'data' / p).resolve()

def inspect_state(root, config, instance='paper'):
    p = state_path(root, config)
    binding = binding_path(root, instance)
    if binding.exists():
        if read_json(binding) != {'path': str(p)} or not p.is_file():
            raise SetupError('Путь сохранённой SQLite изменён или файл потерян. Автосброс запрещён.')
    if not p.exists():
        return
    try:
        db = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError()
            row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if row is None:
                raise ValueError()
            s = json.loads(row[0])
            if s.get('schema') != 1 or s.get('backend') != 'paper_mt5_quotes_v1':
                raise ValueError()
            if instance == 'paper':
                paper = json.loads(db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
                if not isinstance(paper.get('identity'), str) or not paper['identity'].startswith('PAPER-'):
                    raise ValueError()
                if not all((isinstance(paper.get(key), list) for key in ('orders', 'positions', 'deals', 'order_history'))):
                    raise ValueError()
                if not isinstance(paper.get('paused_gap'), bool):
                    raise ValueError()
                if s.get('ledger_contract') is None:
                    if s.get('seen_deals'):
                        raise ValueError()
                    return
                if s.get('ledger_contract') != 'deal_ledger_v1':
                    raise ValueError()
            tickets = [r[0] for r in db.execute('SELECT ticket FROM consumed_deals')]
            seen = s.get('seen_deals', [])
            if len(seen) != len(set(seen)) or len(tickets) != len(set(tickets)) or set(tickets) != set(seen):
                raise ValueError()
        finally:
            db.close()
    except Exception:
        raise SetupError('Состояние активно, неизвестно или несовместимо. Обновление/rollback заблокировано; SQLite сохранена.') from None

def state_lock(root, config):
    p = state_path(root, config)
    return lock(str(p) + '.lock') if p.exists() else nullcontext()

class Manager:

    def __init__(self, root, fetch=download, runner=subprocess.run, report=print):
        self.root = Path(root).resolve()
        self.fetch = fetch
        self.runner = runner
        self.report = report

    def pointer(self):
        path = self.root / 'current.json'
        value = read_json(path) if path.exists() else {'current': None, 'previous': None}
        if set(value) != {'current', 'previous'} or any((x is not None and (not isinstance(x, str) or not SHA.fullmatch(x)) for x in value.values())):
            raise SetupError('Повреждён указатель версии; автоматический сброс запрещён.')
        return value

    def version(self, sha):
        return self.root / 'releases' / sha

    def prepare_python(self, release):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(('PIP_', 'PYTHON', 'TRADFI_')):
                env.pop(k)
        env['PIP_CONFIG_FILE'] = os.devnull
        flags = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {}

        def run(args):
            result = self.runner(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, **flags)
            if result.returncode:
                raise SetupError('Не удалось установить зависимости. Предыдущая версия сохранена.')
        run([sys.executable, '-I', '-m', 'venv', str(release / 'venv')])
        python = release / 'venv' / 'Scripts' / 'python.exe'
        run([str(python), '-I', '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--disable-pip-version-check', '--no-input', '-r', str(release / 'code' / 'requirements-tradfi-mt5.txt')])
        run([str(python), '-I', '-c', 'import MetaTrader5, requests'])

    def install(self):
        with self.maintenance_guard():
            old = self.pointer()
            if old['current'] and (not self.config_path.is_file()):
                raise SetupError('Конфигурация потеряна.')
            self.inspect_states()
            self.backup()
            sha = latest_sha(self.fetch)
            self.report(f'{REPO}\nmain → {sha}')
            if sha == old['current']:
                self.current_release()
                self.report('Уже установлена актуальная версия.')
                return
            release = self.version(sha)
            if release.exists():
                validate_release(release / 'code')
                if read_json(release / 'ready.json') != {'sha': sha, 'contract': CONTRACT}:
                    raise SetupError('Папка версии неполная. Проверьте её вручную; ничего не удалено.')
                self.verify_prepared(release)
                self.initialize(release / 'code')
                atomic_json(self.root / 'current.json', {'current': sha, 'previous': old['current']})
                self.report('Переключено на уже установленную проверенную версию.')
                return
            self.root.joinpath('releases').mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.download-', dir=self.root / 'releases'))
            created = False
            try:
                blob = self.fetch(f'https://github.com/{REPO}/archive/{sha}.zip')
                extract_archive(blob, stage / 'code', sha)
                validate_release(stage / 'code')
                stage.rename(release)
                created = True
                self.prepare_python(release)
                self.verify_prepared(release)
                self.initialize(release / 'code')
                atomic_json(release / 'ready.json', {'sha': sha, 'contract': CONTRACT})
                atomic_json(self.root / 'current.json', {'current': sha, 'previous': old['current']})
            except BaseException:
                if created:
                    try:
                        referenced = sha in self.pointer().values()
                    except Exception:
                        referenced = True
                    if not referenced:
                        failed = self.root / 'failed'
                        failed.mkdir(exist_ok=True)
                        release.rename(failed / (sha + '-' + uuid.uuid4().hex))
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            self.report('Установка завершена. Настройки и база — в папке data. Торговля не включалась.')

    def verify_prepared(self, release):
        pass

    def current_release(self):
        sha = self.pointer()['current']
        if not sha:
            raise SetupError('Сначала установите код.')
        release = self.version(sha)
        if read_json(release / 'ready.json') != {'sha': sha, 'contract': CONTRACT}:
            raise SetupError('Версия не подготовлена.')
        validate_release(release / 'code')
        return release

    def request_stop(self, session, instance='paper'):
        validate_instance(instance)
        if not isinstance(session, str) or not re.fullmatch('[0-9a-f]{32}', session):
            raise SetupError('Некорректная сессия запуска.')
        path = self.root / 'data' / ('stop-' + instance + '-' + session + '.request')
        path.touch(exist_ok=True)
        return path

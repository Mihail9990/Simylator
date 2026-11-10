"""Standalone Windows Python 3.13 GUI installer. Only the standard library is needed."""
from contextlib import closing, contextmanager, nullcontext, ExitStack
from decimal import Decimal
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

def _state_json(payload):
    """Reject ambiguous/non-finite persisted JSON without changing the database."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate state key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Non-finite state number')
    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('State must be an object')
    return value


def _inspect_paper_history(state, paper):
    """PAPER accounts exits in seen_deals, not the LIVE consumed_deals table.

    Read the persisted exchange only. Do not construct PaperSession: its startup
    can save state, reconcile receipts and latch the restart pause. Exchange
    history may be ahead of the controller after an interrupted tick, so seen
    exits must be a subset, not an exact copy, of the complete exchange history.
    """
    def identity(value):
        return isinstance(value, str) and bool(value)

    def ids(rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not identity(row.get('id')) for row in rows):
            raise ValueError('Invalid paper records')
        result = {row['id']: row for row in rows}
        if len(result) != len(rows):
            raise ValueError('Duplicate paper identity')
        return result

    def amount(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError('Invalid paper amount')
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('Non-finite paper amount')
        return number

    if not identity(paper.get('identity')) or not paper['identity'].startswith('PAPER-'):
        raise ValueError('Invalid paper account')
    if not isinstance(paper.get('paused_gap'), bool):
        raise ValueError('Invalid paper pause')
    orders = ids(paper.get('orders'))
    positions = ids(paper.get('positions'))
    deals = ids(paper.get('deals'))
    history = ids(paper.get('order_history'))
    if orders.keys() & history.keys():
        raise ValueError('Duplicate paper order')
    results = paper.get('results')
    if not isinstance(results, dict) or any(not identity(cid) or not isinstance(result, dict)
            or result.get('status') not in {'ACCEPTED', 'REJECTED', 'DONE'} for cid, result in results.items()):
        raise ValueError('Unknown paper result')
    if amount(paper['commission_per_lot_side']) < 0:
        raise ValueError('Invalid paper costs')
    if paper['cursor'] is not None and (type(paper['cursor']) is not int or paper['cursor'] < 0):
        raise ValueError('Invalid paper cursor')
    if not isinstance(paper['cursor_keys'], list) or any(not identity(key) for key in paper['cursor_keys']):
        raise ValueError('Invalid paper cursor keys')
    seen = state.get('seen_deals')
    if not isinstance(seen, list) or any(not identity(ticket) for ticket in seen) or len(seen) != len(set(seen)):
        raise ValueError('Invalid consumed paper exits')
    commands = state.get('commands')
    if not isinstance(commands, dict):
        raise ValueError('Invalid paper commands')
    statuses = {'READY', 'UNKNOWN', 'ACCEPTED', 'PENDING', 'DONE', 'REJECTED', 'CANCELLED', 'FILLED', 'OBSOLETE'}
    for cid, command in commands.items():
        if (not identity(cid) or not isinstance(command, dict) or command.get('id') != cid
                or command.get('kind') not in {'STOP', 'MARKET', 'CANCEL', 'MODIFY', 'CLOSE'}
                or command.get('status') not in statuses):
            raise ValueError('Unknown paper command')
        if command['status'] == 'UNKNOWN' and cid not in results:
            raise ValueError('Unresolved paper command outcome')
    phases = {'IDLE', 'WAIT_START', 'WAIT_CANDLE', 'COOLDOWN', 'PAUSED', 'PAUSED_CYCLE',
              'SAFE_PAUSE', 'MANUAL', 'INITIAL_WAIT', 'INITIAL_PENDING', 'INITIAL_POSITION',
              'RECOVERY_WAIT', 'RECOVERY_CLEANUP', 'RECOVERY_PENDING', 'RECOVERY_POSITION',
              'V3_WAIT_ENTRY', 'V3_WAIT_CANDLE'}
    receipts = state.get('control_receipts', {})
    if not isinstance(receipts, dict) or any(not isinstance(row, dict) or row.get('status') not in {'APPLIED', 'REJECTED'}
                                           for row in receipts.values()):
        raise ValueError('Unresolved paper control action')
    if state.get('migration_block') or state.get('recovery_mode') not in {None, 'V3'}:
        raise ValueError('Unresolved strategy migration')
    if amount(state.get('basket_carry', '0')) < 0:
        raise ValueError('Invalid carried recovery')
    if (not isinstance(state.get('stop'), bool) or state.get('phase') not in phases
            or 'cycle' not in state or (state['cycle'] is not None and not isinstance(state['cycle'], dict))
            or not isinstance(state.get('completed'), list)
            or any(not isinstance(row, dict) or row.get('currency') != 'UST'
                   for row in [state, *state['completed'], *([state['cycle']] if state['cycle'] is not None else [])])):
        raise ValueError('Incomplete paper controller')
    cycle = state['cycle']
    if cycle is not None:
        if (cycle.get('contract') != 'ticket_basket_v1' or cycle.get('recovery_mode') != 'V3'
                or not identity(cycle.get('id')) or amount(cycle['recovery']) < 0):
            raise ValueError('Unknown or incompatible paper cycle')
        amount(cycle['realized'])
    if 'account' not in state:
        raise ValueError('Missing paper account')
    if state['account'] != paper['identity']:
        if state['account'] is not None or commands or deals or orders or positions or history or cycle is not None or state['completed'] or seen:
            raise ValueError('Paper account mismatch')
    entries, exits = {}, {}
    for deal in deals.values():
        if (deal.get('entry') not in {'IN', 'OUT'} or not identity(deal.get('position_id'))
                or deal.get('side') not in {'BUY', 'SELL'} or amount(deal['volume']) <= 0
                or amount(deal['price']) <= 0):
            raise ValueError('Invalid paper execution')
        for key in ('profit', 'commission', 'swap', 'fee'):
            amount(deal[key])
        group = entries if deal['entry'] == 'IN' else exits
        if deal['position_id'] in group:
            raise ValueError('Duplicate paper fill or close')
        group[deal['position_id']] = deal
    if not set(seen) <= {deal['id'] for deal in exits.values()} or not exits.keys() <= entries.keys():
        raise ValueError('Missing paper execution history')
    if positions.keys() != entries.keys() - exits.keys():
        raise ValueError('Missing or unknown paper positions')
    for pid, entry in entries.items():
        order = history.get(entry.get('order_id'))
        if (not order or order.get('status') != 'FILLED' or order.get('command_id') != entry.get('command_id')
                or order['command_id'] not in commands):
            raise ValueError('Missing paper entry order')
        if entry['side'] != order.get('side') or amount(entry['volume']) != amount(order['size']):
            raise ValueError('Paper fill differs from entry order')
        current = positions.get(pid)
        close = exits.get(pid)
        if current is not None:
            if (current.get('owned') is not True or current.get('side') != entry['side']
                    or amount(current['size']) != amount(entry['volume']) or amount(current['entry']) != amount(entry['price'])):
                raise ValueError('Paper position mismatch')
            amount(current['sl']); amount(current['tp'])
        if close is not None and (close['side'] != entry['side'] or amount(close['volume']) != amount(entry['volume'])):
            raise ValueError('Paper close mismatch')
    filled_orders = {entry.get('order_id') for entry in entries.values()}
    if len(filled_orders) != len(entries):
        raise ValueError('Duplicate paper order fill')
    order_commands = set()
    for oid, order in {**orders, **history}.items():
        if (order.get('owned') is not True or order.get('command_id') not in commands
                or order.get('side') not in {'BUY', 'SELL'} or amount(order['size']) <= 0
                or order.get('status') not in ({'PENDING'} if oid in orders else {'FILLED', 'CANCELLED'})):
            raise ValueError('Unknown paper order')
        cid = order['command_id']
        command = commands[cid]
        if (cid in order_commands or command['kind'] not in {'STOP', 'MARKET'}
                or order['side'] != command.get('side') or amount(order['size']) != amount(command['size'])):
            raise ValueError('Paper order command mismatch')
        order_commands.add(cid)
        for key in ('price', 'sl', 'tp'):
            amount(order[key])
        if (order['status'] == 'FILLED') != (oid in filled_orders):
            raise ValueError('Missing paper fill history')
    for cid, result in results.items():
        command = commands.get(cid)
        if command is None:
            raise ValueError('Paper result without controller command')
        if result['status'] == 'ACCEPTED' and command['kind'] in {'STOP', 'MARKET'}:
            order = orders.get(result.get('order_id')) or history.get(result.get('order_id'))
            if order is None or order['command_id'] != cid:
                raise ValueError('Accepted paper order missing')


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
            s = _state_json(row[0])
            if type(s.get('schema')) is not int or s['schema'] != 1 or s.get('backend') != 'paper_mt5_quotes_v1':
                raise ValueError()
            # The release envelope retains its historical ledger_contract field
            # for installer compatibility. A PAPER controller snapshot never
            # uses that LIVE ledger; a claimed ledger is not a supported upgrade.
            if s.get('ledger_contract') is not None:
                raise ValueError('Unexpected controller ledger contract')
            paper = _state_json(db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
            _inspect_paper_history(s, paper)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'consumed_deals' in tables and db.execute('SELECT 1 FROM consumed_deals LIMIT 1').fetchone():
                raise ValueError('Unexpected LIVE ledger in PAPER state')
        finally:
            db.close()
    except Exception:
        raise SetupError('Не удалось подтвердить целостность и совместимость состояния PAPER. Обновление/rollback заблокировано; SQLite сохранена. Не удаляйте базу: нужна проверка истории или неизвестных исходов команд.') from None

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

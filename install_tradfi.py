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
BRANCH = 'sl-overlap-recovery'
VARIANT = 'sl_overlap_recovery_v1'
CONTRACT = {'format': 1, 'backend': 'paper_mt5_quotes_v1', 'state_schema': 1, 'ledger_contract': 'deal_ledger_v1', 'scope_contract': 'cycle_scope_v1', 'python': '3.13', 'variant': VARIANT}
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
    data = json.loads(fetch(f'https://api.github.com/repos/{REPO}/git/ref/heads/{BRANCH}', 65536))
    if not isinstance(data, dict) or data.get('ref') != f'refs/heads/{BRANCH}' or (not isinstance(data.get('object'), dict)) or (data['object'].get('type') != 'commit'):
        raise SetupError(f'GitHub не вернул ссылку на ветку {BRANCH}.')
    sha = data['object'].get('sha', '')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise SetupError(f'GitHub не вернул корректный SHA ветки {BRANCH}.')
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
        raise SetupError('Эта версия ещё не поддерживает установщик SL OVERLAP. Дождитесь публикации совместимой версии.')
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
            # Updating swaps code only, never reconciles or approves trading
            # history. UNKNOWN receipts, recovery, cycles and PAPER result
            # extensions belong to the runtime, not installer eligibility.
            # Keep the complete exchange opaque and backed up unchanged.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'consumed_deals' in tables and db.execute('SELECT 1 FROM consumed_deals LIMIT 1').fetchone():
                raise ValueError('Unexpected LIVE ledger in PAPER state')
        finally:
            db.close()
    except Exception:
        raise SetupError('Не удалось проверить файл SQLite или формат PAPER. Данные сохранены; обновление не выполнено. История сделок и исходы команд установщиком не проверяются.') from None

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
            self.report(f'{REPO}\n{BRANCH} → {sha}')
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


def _windows_path(value):
    """Compare native Windows paths without relying on host OS in offline tests."""
    import ntpath
    if not isinstance(value, str) or not ntpath.isabs(value):
        return None
    return ntpath.normcase(ntpath.normpath(value))


def owned_paper_process(root, process):
    """Strict command identity, not executable name or descendant-tree guessing.

    Windows venv redirectors may execute the base python.exe. Both processes
    retain the exact venv argv[0], service script and own config arguments.
    The observed executable is subsequently rechecked on a held process handle.
    """
    import ntpath
    root = _windows_path(str(root))
    if root is None or not isinstance(process, dict):return False
    args=process.get('argv');pid=process.get('pid');created=process.get('created')
    if type(pid) is not int or pid<=0 or type(created) is not int or created<=0:return False
    if not isinstance(args,list) or len(args)!=7 or not all(isinstance(a,str) for a in args):return False
    if args[2:5]!=['--mode','paper','--service'] or args[5]!='--config':return False
    script=_windows_path(args[1]);exe=_windows_path(process.get('exe'))
    if script is None or exe is None or ntpath.basename(exe) not in ('python.exe','pythonw.exe'):return False
    prefix=ntpath.join(root,'releases')+'\\'
    if not script.startswith(prefix):return False
    relative=script[len(prefix):].split('\\')
    if len(relative)!=3 or not SHA.fullmatch(relative[0]) or relative[1:]!=['code','tradfi_main.py']:return False
    expected=ntpath.join(root,'releases',relative[0],'venv','Scripts','python.exe')
    return _windows_path(args[0])==_windows_path(expected) and _windows_path(args[6])==ntpath.join(root,'data','service.json')



def same_cim_process(row,identity):
    # CIM exposes microseconds, FILETIME exposes 100 ns. Compare the common
    # precision once at discovery, then retain exact native FILETIME forever.
    return bool(identity and identity['created']//10==int(row['created'])//10 and _windows_path(identity['exe'])==_windows_path(row['exe']))

def windows_processes():
    """Read process metadata only; no broad termination commands are executed."""
    if os.name!='nt':raise SetupError('Остановка процессов поддерживается только в Windows.')
    import ctypes
    from ctypes import wintypes
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes=[wintypes.HLOCAL];kernel.LocalFree.restype=wintypes.HLOCAL
    command="""$ErrorActionPreference='Stop'; $p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | ForEach-Object { @{pid=[int]$_.ProcessId;parent=[int]$_.ParentProcessId;exe=$_.ExecutablePath;cmd=$_.CommandLine;created=([string]$_.CreationDate.ToFileTimeUtc())} }); ConvertTo-Json -InputObject $p -Compress"""
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise SetupError('Не удалось проверить владельцев процессов PAPER. Процессы не завершались.')
    try:
        rows=json.loads(result.stdout)
        if not isinstance(rows,list):raise ValueError()
        processes=[]
        for row in rows:
            if not row.get('cmd') or not row.get('exe'):continue # Access denied is not ownership evidence.
            count=ctypes.c_int();pointer=shell.CommandLineToArgvW(row['cmd'],ctypes.byref(count))
            if not pointer:raise ValueError()
            try:args=[pointer[i] for i in range(count.value)]
            finally:kernel.LocalFree(pointer)
            try:identity=process_identity(row['pid'])
            except (SetupError,OSError):continue # Process exited or is inaccessible; never infer ownership.
            if not same_cim_process(row,identity):continue
            processes.append(dict(identity,parent=row['parent'],argv=args))
        return processes
    except (ValueError,TypeError,KeyError):raise SetupError('Не удалось разобрать список процессов; остановка не выполнена.') from None


def terminate_verified_process(process):
    """Terminate only the held, revalidated process, never a reused PID/tree."""
    if os.name!='nt':raise SetupError('Остановка процессов поддерживается только в Windows.')
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle.restype=wintypes.BOOL
    api.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)];api.GetProcessTimes.restype=wintypes.BOOL
    api.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)];api.QueryFullProcessImageNameW.restype=wintypes.BOOL
    api.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];api.TerminateProcess.restype=wintypes.BOOL
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.WaitForSingleObject.restype=wintypes.DWORD
    handle=api.OpenProcess(0x1000|0x0001|0x00100000,False,process['pid'])
    if not handle:
        if ctypes.get_last_error()==87:return False # Already gone.
        raise SetupError('Нет доступа к собственному процессу PAPER; другие процессы не затронуты.')
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        size=wintypes.DWORD(32768);name=ctypes.create_unicode_buffer(size.value)
        if not api.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]) or not api.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):
            raise SetupError('Не удалось повторно проверить процесс; он не завершался.')
        created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        if created!=process['created'] or _windows_path(name.value)!=_windows_path(process['exe']):return False
        if api.WaitForSingleObject(handle,0)==0:return False
        if not api.TerminateProcess(handle,1) or api.WaitForSingleObject(handle,5000)!=0:
            raise SetupError('Не подтверждено завершение собственного процесса PAPER.')
        return True
    finally:api.CloseHandle(handle)



def process_identity(pid):
    """Obtain stable Windows identity without executing or terminating anything."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle.restype=wintypes.BOOL
    api.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)];api.GetProcessTimes.restype=wintypes.BOOL
    api.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)];api.QueryFullProcessImageNameW.restype=wintypes.BOOL
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:raise SetupError('Не удалось записать владельца процесса PAPER.')
    try:
        times=[wintypes.FILETIME() for _ in range(4)];size=wintypes.DWORD(32768);name=ctypes.create_unicode_buffer(size.value)
        if not api.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]) or not api.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):
            raise SetupError('Не удалось определить идентичность процесса PAPER.')
        return dict(pid=pid,created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime,exe=name.value)
    finally:api.CloseHandle(handle)


def register_process_ownership(root,pid,kind,session=None):
    if kind not in ('service','quote_worker'):raise SetupError('Неизвестный тип процесса PAPER.')
    identity=process_identity(pid)
    if identity is None:return
    root=Path(root).resolve()
    if session is not None and not re.fullmatch('[0-9a-f]{32}',session):raise SetupError('Некорректная сессия запуска.')
    atomic_json(root/'data'/'processes'/f"{pid}-{identity['created']}.json",dict(identity,format=1,root=str(root),kind=kind,session=session))


def _spawn_worker(process):
    args=process.get('argv',[])
    if not isinstance(args,list) or len(args)<4 or args[-1]!='--multiprocessing-fork' or args[-3]!='-c':return False
    if any(flag not in ('-B','-E','-I','-s','-S','-O','-OO','-u') for flag in args[1:-3]):return False
    return bool(re.fullmatch(r'from multiprocessing.spawn import spawn_main; spawn_main\(parent_pid=\d+, pipe_handle=\d+\)',args[-2]))


def owned_processes(root,processes,records=()):
    """Root service commands plus tightly identified capture workers only."""
    owned={p['pid']:p for p in processes if owned_paper_process(root,p)}
    for p in processes:
        if not _spawn_worker(p) or type(p.get('pid')) is not int or p['pid']<=0 or type(p.get('created')) is not int or p['created']<=0:continue
        import ntpath
        prefix=ntpath.join(_windows_path(str(root)) or '', 'releases')+'\\'
        argv0=_windows_path(p['argv'][0]) or ''
        rel=argv0[len(prefix):].split('\\') if argv0.startswith(prefix) else []
        if len(rel)==4 and SHA.fullmatch(rel[0]) and rel[1:]==['venv','scripts','python.exe'] and ntpath.basename(_windows_path(p.get('exe')) or '') in ('python.exe','pythonw.exe'):
            owned[p['pid']]=p
        for r in records:
            if r.get('kind')=='quote_worker' and r.get('root')==str(root) and all(r.get(k)==p.get(k) for k in ('pid','created')) and _windows_path(r.get('exe'))==_windows_path(p.get('exe')):
                owned[p['pid']]=p
    # A venv redirector and its base Python process may add one lineage level.
    for _ in range(3):
        for p in processes:
            parent=owned.get(p.get('parent'))
            if parent and _spawn_worker(p) and type(p.get('created')) is int and p['created']>=parent['created'] and _windows_path(p.get('exe'))==_windows_path(parent.get('exe')):
                owned[p['pid']]=p
    return list(owned.values())

def stop_owned_paper_processes(root, *, sessions=(), grace_seconds=3, enumerate_processes=None, terminate=None, sleep=time.sleep):
    """Stop service processes belonging to this installation, including old windows.

    Installer/UI and dependency installation processes are deliberately excluded.
    MT5 and another root cannot match the exact script + config command identity.
    Caller must hold its operation lock to prevent a simultaneous new launch.
    """
    enumerate_processes=enumerate_processes or windows_processes
    terminate=terminate or terminate_verified_process
    root=Path(root)
    if read_json(root/'managed-install.json')!=dict(format=2,role='paper',layout='sl_overlap_roots_v1') or read_json(root/'application.json')!=dict(format=1,role='paper'):
        raise SetupError('Этот каталог не является установкой PAPER SL OVERLAP.')
    for session in sessions:
        if not isinstance(session,str) or not re.fullmatch('[0-9a-f]{32}',session):raise SetupError('Некорректная сессия остановки.')
    records=[]
    for path in (root/'data'/'processes').glob('*.json'):
        try:
            record=read_json(path)
            if record.get('format')==1 and record.get('root')==str(root):records.append(record)
        except (ValueError,OSError,AttributeError):continue
    sessions=set(sessions)|{r['session'] for r in records if isinstance(r.get('session'),str) and re.fullmatch('[0-9a-f]{32}',r['session'])}
    initial=owned_processes(root,enumerate_processes(),records)
    for session in sessions:(root/'data'/('stop-paper-'+session+'.request')).touch(exist_ok=True)
    if initial and grace_seconds>0:sleep(grace_seconds)
    stopped=0
    # Re-enumerate after the grace period; termination revalidates a held handle.
    candidates={p['pid']:p for p in initial}
    candidates.update({p['pid']:p for p in owned_processes(root,enumerate_processes(),records)})
    for process in reversed(list(candidates.values())):stopped+=bool(terminate(process))
    observed=enumerate_processes()
    remaining=[p['pid'] for p in observed if p in owned_processes(root,observed,records) or any(p.get('pid')==old.get('pid') and p.get('created')==old.get('created') for old in candidates.values())]
    if remaining:raise SetupError('Остались собственные процессы PAPER: '+', '.join(map(str,remaining))+'. Обновление пока не запускайте.')
    return dict(found=len(initial),forced=stopped,remaining=[])

"""Windows HTTPS setup. No trading imports; mutations require a reviewed plan.
The inventory and command adapter are injectable so tests never control real services.
"""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import ssl
import stat
import struct
import queue
import threading
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import zipfile
VERSION = '2.11.7'
ARCHIVE_SHA256 = '0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06'
DOWNLOAD = f'https://github.com/caddyserver/caddy/releases/download/v{VERSION}/caddy_{VERSION}_windows_amd64.zip'

class HttpsError(Exception):
    pass

class CommandError(HttpsError):

    def __init__(self, command, returncode):
        self.command = command if command.lower() in ('sc.exe', 'reg.exe', 'icacls.exe', 'powershell.exe', 'caddy.exe') else 'command'
        self.returncode = returncode if isinstance(returncode, int) else None
        super().__init__('Команда Windows не завершена: ' + self.command + '.')

class AttemptError(HttpsError):
    pass
STAGES = {'local_archive', 'begin', 'admin', 'preflight', 'download', 'checksum', 'unpack', 'recheck', 'directory', 'acl', 'backup', 'binary', 'caddyfile', 'adapt', 'old_service', 'config', 'service_create', 'service_start', 'complete', 'recovery'}
MESSAGES = {'ADMIN_REQUIRED': 'Для изменения служб нужен запуск установщика от имени администратора того же пользователя Windows.', 'CHECKSUM': 'SHA256 архива Caddy не совпал. Файл не исполнялся; не отключайте проверку.', 'HTTP_ERROR': 'Сервер загрузки вернул ошибку HTTP; проверьте доступ к GitHub с VPS.', 'TLS_VERIFY': 'Не удалось проверить TLS сертификат загрузки. Проверку TLS не отключать.', 'NETWORK': 'Сетевая загрузка не выполнена. Проверьте доступ к GitHub; причина на VPS требует отчёта.', 'TIMEOUT': 'Операция превысила тайм-аут. Состояние службы необходимо сверить до повторения.', 'FILE_PERMISSION': 'Windows отказала в доступе к файлу. Проверьте пользователя, права и защиту Windows.', 'FILE_MISSING': 'Не найден требуемый файл или исполняемая программа.', 'ZIP_INVALID': 'Архив не содержит ожидаемый корректный caddy.exe.', 'COMMAND_FAILED': 'Команда Windows/Caddy завершилась с ошибкой. Код возврата указан отдельно.', 'GUARD_REJECTED': 'Проверка безопасности отклонила действие: повторите диагностику и проверьте план/настройки.', 'OS_ERROR': 'Системная файловая/процессная операция не выполнена.', 'UNKNOWN': 'Неопознанная ошибка; сырой текст скрыт для защиты секретов.'}

def safe_failure(exc, stage):
    row = {'stage': stage if stage in STAGES else 'begin'}
    if isinstance(exc, urllib.error.HTTPError):
        code = 'HTTP_ERROR'
        row['http_status'] = int(exc.code)
    elif isinstance(exc, ssl.SSLCertVerificationError) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError)):
        code = 'TLS_VERIFY'
    elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)):
        code = 'TIMEOUT'
    elif isinstance(exc, urllib.error.URLError):
        code = 'NETWORK'
    elif isinstance(exc, PermissionError):
        code = 'FILE_PERMISSION'
    elif isinstance(exc, FileNotFoundError):
        code = 'FILE_MISSING'
    elif isinstance(exc, (zipfile.BadZipFile, KeyError)):
        code = 'ZIP_INVALID'
    elif isinstance(exc, CommandError):
        code = 'COMMAND_FAILED'
        row.update(command=exc.command, exit_code=exc.returncode)
    elif isinstance(exc, HttpsError) and stage == 'admin':
        code = 'ADMIN_REQUIRED'
    elif isinstance(exc, HttpsError) and stage == 'checksum':
        code = 'CHECKSUM'
    elif isinstance(exc, HttpsError):
        code = 'GUARD_REJECTED'
    elif isinstance(exc, OSError):
        code = 'OS_ERROR'
    else:
        code = 'UNKNOWN'
    row.update(code=code, reason=MESSAGES[code])
    return row

class BackgroundJob:
    """Only the Tk owner calls start/poll; worker never calls any Tk method."""

    def __init__(self):
        self.busy = False
        self.events = queue.Queue()

    def start(self, fn):
        if self.busy:
            return False
        self.busy = True

        def run():
            try:
                self.events.put((True, fn()))
            except Exception as exc:
                self.events.put((False, exc))
        threading.Thread(target=run, daemon=True).start()
        return True

    def poll(self):
        try:
            result = self.events.get_nowait()
        except queue.Empty:
            return None
        self.busy = False
        return result
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXE = 128 * 1024 * 1024

def validate_archive(data):
    """Return only the pinned executable bytes. Never extract archive paths."""
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE or digest(data) != ARCHIVE_SHA256:
        raise HttpsError('Нужен неизменённый официальный Caddy ' + VERSION + ' Windows amd64 ZIP: SHA256 не совпал. Другие версии не поддерживаются.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = z.infolist()
            names = [i.filename for i in entries]
            if not entries or len(names) != len(set(names)) or set(names) - {'LICENSE', 'README.md', 'caddy.exe'} or ('caddy.exe' not in names):
                raise ValueError()
            binary = None
            for i in entries:
                mode = i.external_attr >> 16 & 65535
                if i.is_dir() or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or i.flag_bits & 1 or (i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError()
                if i.file_size < 0 or i.file_size > (MAX_EXE if i.filename == 'caddy.exe' else 1024 * 1024):
                    raise ValueError()
                value = z.read(i)
                if i.filename == 'caddy.exe':
                    binary = value
            if binary is None or len(binary) < 64 or binary[:2] != b'MZ':
                raise ValueError()
            pe = struct.unpack_from('<I', binary, 60)[0]
            if pe < 64 or pe + 26 > len(binary) or binary[pe:pe + 4] != b'PE\x00\x00':
                raise ValueError()
            machine = struct.unpack_from('<H', binary, pe + 4)[0]
            optional = struct.unpack_from('<H', binary, pe + 20)[0]
            flags = struct.unpack_from('<H', binary, pe + 22)[0]
            if machine != 34404 or optional < 2 or pe + 24 + optional > len(binary) or flags & 8192 or (struct.unpack_from('<H', binary, pe + 24)[0] != 523):
                raise ValueError()
            return binary
    except (ValueError, KeyError, struct.error, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise HttpsError('ZIP повреждён или содержит неподдерживаемые файлы/архитектуру. Нужен официальный архив Windows amd64; ничего не извлечено.') from None

def digest(data):
    return hashlib.sha256(data).hexdigest()

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def arguments(command):
    tokens = re.findall('"[^"\\r\\n]*"|[^ \\t\\r\\n]+', command)
    return [t[1:-1] if t.startswith('"') and t.endswith('"') else t for t in tokens]
INVENTORY = '\n$ErrorActionPreference=\'Stop\'\n$services=@(Get-CimInstance Win32_Service | Where-Object { $_.PathName -match \'(?i)caddy|https_ip\' -or $_.Name -eq \'AIImpulsHTTPS\' } | ForEach-Object {\n $s=$_; $g=Get-Service -Name $s.Name -ErrorAction Stop\n $p=$null; if($s.ProcessId -gt 0){$p=Get-CimInstance Win32_Process -Filter ("ProcessId="+$s.ProcessId) -ErrorAction Stop}\n [ordered]@{Name=$s.Name;PathName=$s.PathName;StartName=$s.StartName;StartMode=$s.StartMode;State=$s.State;ProcessId=[int]$s.ProcessId;\n Dependencies=@($g.ServicesDependedOn|ForEach-Object {$_.Name});Dependents=@($g.DependentServices|ForEach-Object {$_.Name});\n ProcessPath=if($p){$p.ExecutablePath}else{\'\'};ProcessCommand=if($p){$p.CommandLine}else{\'\'}}\n})\n$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {$_.LocalPort -in @(80,443,2020,8765)} | ForEach-Object {\n [ordered]@{port=[int]$_.LocalPort;pid=[int]$_.OwningProcess;address=$_.LocalAddress}\n})\n[ordered]@{services=$services;listeners=$listeners}|ConvertTo-Json -Depth 6 -Compress\n'

class Windows:

    def run(self, args, timeout=40):
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise CommandError(Path(args[0]).name, result.returncode)
        return result.stdout

    def inventory(self):
        raw = self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();' + INVENTORY])
        try:
            return json.loads(raw.decode('utf-8-sig'))
        except Exception:
            raise HttpsError('Не удалось проверить службы и порты.') from None

    def admin(self):
        if os.name != 'nt':
            raise HttpsError('Настройка служб доступна только в Windows.')
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise HttpsError('Откройте установщик от имени администратора. Изменения не применены.')

    def sc(self, *args):
        return self.run(['sc.exe', *args])

    def wait(self, name, state):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            svc = next((s for s in self.inventory()['services'] if s['Name'] == name), None)
            if state == 'Absent' and svc is None or (svc and svc['State'] == state):
                return
            time.sleep(0.25)
        raise HttpsError('Служба не подтвердила состояние ' + state + '. Повторное действие запрещено до диагностики.')

    def protect(self, folder):
        raw = self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'])
        sid = raw.decode('ascii').strip()
        if not re.fullmatch('S-1-[0-9-]+', sid):
            raise HttpsError('Не удалось определить владельца резервной копии.')
        self.run(['icacls.exe', str(folder), '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'])

    def fetch(self):
        request = urllib.request.Request(DOWNLOAD, headers={'User-Agent': 'AI-Impuls-HTTPS-Setup'})
        with urllib.request.urlopen(request, timeout=30) as response:
            from urllib.parse import urlsplit
            target = urlsplit(response.url)
            if target.scheme != 'https' or target.hostname not in ('github.com', 'release-assets.githubusercontent.com'):
                raise HttpsError('Неожиданный источник Caddy.')
            chunks = []
            size = 0
            deadline = time.monotonic() + 90
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 64 * 1024 * 1024 or time.monotonic() > deadline:
                    raise HttpsError('Загрузка Caddy превысила лимит.')
            data = b''.join(chunks)
        if len(data) > 64 * 1024 * 1024 or digest(data) != ARCHIVE_SHA256:
            raise HttpsError('Контрольная сумма Caddy не совпала.')
        return data

def verify_service_process(service):
    if service['State'] == 'Running':
        args = arguments(service['PathName'])
        actual = arguments(service.get('ProcessCommand', ''))
        if not service.get('ProcessId') or not actual or [x.lower() for x in actual] != [x.lower() for x in args] or (str(PureWindowsPath(service.get('ProcessPath', ''))).lower() != str(PureWindowsPath(args[0])).lower()):
            raise HttpsError('Процесс службы не соответствует её конфигурации.')

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
MESSAGES = {'ADMIN_REQUIRED': 'Для изменения служб нужен запуск установщика от имени администратора того же пользователя Windows.', 'CHECKSUM': 'SHA256 архива Caddy не совпал. Файл не исполнялся; не отключайте проверку.', 'HTTP_ERROR': 'HTTPS-сервер вернул ошибку HTTP; проверьте код ответа и этап операции.', 'TLS_VERIFY': 'Не удалось проверить TLS-сертификат соединения. Проверку TLS не отключать.', 'NETWORK': 'Сетевая загрузка не выполнена. Проверьте доступ к GitHub; причина на VPS требует отчёта.', 'TIMEOUT': 'Операция превысила тайм-аут. Состояние службы необходимо сверить до повторения.', 'FILE_PERMISSION': 'Windows отказала в доступе к файлу. Проверьте пользователя, права и защиту Windows.', 'FILE_MISSING': 'Не найден требуемый файл или исполняемая программа.', 'ZIP_INVALID': 'Архив не содержит ожидаемый корректный caddy.exe.', 'COMMAND_FAILED': 'Команда Windows/Caddy завершилась с ошибкой. Код возврата указан отдельно.', 'GUARD_REJECTED': 'Проверка безопасности отклонила действие: повторите диагностику и проверьте план/настройки.', 'OS_ERROR': 'Системная файловая/процессная операция не выполнена.', 'UNKNOWN': 'Неопознанная ошибка; сырой текст скрыт для защиты секретов.'}

MESSAGES.update({
    'PUBLIC_ADDRESS_REQUIRED': 'Нужен публичный адрес.',
    'INSTANCE_ID_INVALID': 'Неверный идентификатор PAPER.',
    'ADDRESS_INVALID': 'Неверный адрес.',
    'FIREWALL_PORTS_INVALID': 'Недопустимые порты firewall.',
    'HTTPS_ADDRESS_INVALID': 'Неверный HTTPS адрес.',
    'ROOT_INVALID': 'Нужна завершённая независимая установка PAPER.',
    'ROOT_CONFIG_INVALID': 'Нужен PAPER без реальных сделок с панелью 127.0.0.1:8766.',
    'FIREWALL_INVENTORY_INVALID': 'Не удалось проверить firewall.',
    'HTTPS_CONFLICT': 'Обнаружена LIVE/PAPER или неизвестная HTTPS-служба/firewall. Ничего не отключаем.',
    'PORT_BUSY': 'Порт занят. Сверьте владельца порта; чужие службы и LIVE не останавливайте. Если порт занят PAPER, остановите его через окно PAPER.',
    'PREVIOUS_ATTEMPT': 'Есть предыдущая попытка. Сначала диагностика/откат.',
    'PASSWORD_REQUIRED': 'Задайте пароль PAPER не короче 32 символов.',
    'PLAN_CHANGED': 'Настройки/службы/порты изменились. Подготовьте новый план.',
    'HTTPS_CONFIG_CHANGED': 'Настройки HTTPS изменились.',
    'CREATED_SERVICE_UNCONFIRMED': 'Созданная служба не подтверждена.',
    'PORTS_CHANGED': 'Порты изменились.',
    'CONFIRMATION_REQUIRED': 'Действие не подтверждено.',
    'OPERATION_BUSY': 'Операция уже выполняется.',
    'INSTALLATION_UNCONFIRMED': 'Завершённая собственная установка не подтверждена.',
    'INSTALLATION_FILES_CHANGED': 'Файлы установки изменились.',
    'OWN_SERVICE_UNCONFIRMED': 'Собственная служба PAPER не подтверждена.',
    'BACKEND_LOOPBACK_REQUIRED': 'Нужна работающая панель PAPER только на loopback8766.',
    'HTTPS_LISTENER_MISSING': 'Служба PAPER не слушает443.',
    'ADDRESS_FORMAT_INVALID': 'Введите публичный IP или домен без протокола, порта и пути.',
    'HTTPS_REDIRECT_REJECTED': 'Перенаправление HTTPS запрещено.',
    'HTTPS_INSTANCE_MISMATCH': 'HTTPS принадлежит другой установке.',
    'HTTPS_RESPONSE_TOO_LARGE': 'Ответ HTTPS превысил лимит.',
    'HTTPS_ORIGIN_CHANGED': 'Неожиданный адрес HTTPS.',
    'PAPER_PAGE_UNCONFIRMED': 'HTTPS не подтвердил страницу PAPER.',
    'ROOT_LINK_REJECTED': 'Ссылки и junction не поддерживаются.',
    'LOCAL_ARCHIVE_REQUIRED': 'Нужен локальный официальный ZIP.',
    'REAPPLY_BLOCKED': 'Повторное применение запрещено.',
    'RECOVERY_REVIEW_REQUIRED': 'Нужна сверка неизвестного исхода или откат.',
    'RECOVERY_OWNER_UNCONFIRMED': 'Владелец не подтверждён.',
    'CONFIG_RESTORE_REQUIRED': 'Сначала восстановите настройки откатом.',
    'INSTALLATION_OWNER_UNCONFIRMED': 'Владелец установки не подтверждён.',
    'UNKNOWN_COMMAND_OUTCOME': 'Исход предыдущей команды неизвестен. Автоматический откат запрещён; требуется сверка состояния.',
    'HTTPS_FILES_CHANGED': 'Файлы HTTPS изменились.',
    'BACKUP_OR_CONFIG_CHANGED': 'Настройки или резервная копия изменились.',
    'PAPER_STOP_REQUIRED': 'Сначала остановите PAPER.',
    'FIREWALL_ALREADY_EXISTS': 'Правило firewall уже появилось.',
    'RECOVERY_LINK_REJECTED': 'Ссылки не поддерживаются.',
    'SERVICE_IDENTITY_CHANGED': 'Служба изменилась после подтверждения.',
    'SERVICE_TRANSITIONING': 'Служба в переходном состоянии.',
    'AUTOSTART_DISABLE_UNCONFIRMED': 'Отключение автозапуска не подтверждено.',
    'SERVICE_STATE_CHANGED': 'Состояние службы изменилось.',
    'SERVICE_STOP_UNCONFIRMED': 'Служба не остановлена.',
    'SERVICE_OWNER_MISMATCH': 'Служба не соответствует собственной установке.',
    'FIREWALL_OWNER_UNCONFIRMED': 'Владелец правила firewall не подтверждён.',
    'CONFIG_CHANGED': 'Настройки изменились.',
    'INVENTORY_INVALID': 'Не удалось проверить службы и порты.',
    'WINDOWS_REQUIRED': 'Настройка служб доступна только в Windows.',
    'BACKUP_OWNER_INVALID': 'Не удалось определить владельца резервной копии.',
    'DOWNLOAD_SOURCE_INVALID': 'Неожиданный источник Caddy.',
    'DOWNLOAD_LIMIT': 'Загрузка Caddy превысила лимит.',
    'SERVICE_PROCESS_MISMATCH': 'Процесс службы не соответствует её конфигурации.',
    'MAINTENANCE_REJECTED': 'Проверка блокировки или состояния установки отклонила обслуживание. Сверьте состояние PAPER; повтор без диагностики запрещён.',
})
STAGES.update({"root", "prepare", "password", "firewall", "service_autostart", "maintenance", "verify", "diagnose"})

# Only these codes and constant messages may cross a diagnostic boundary.
class GuardError(HttpsError):
    def __init__(self, code, ports=None):
        self.ports = sorted({p for p in (ports or []) if type(p) is int and p in (80, 443, 8766)})
        self.code = code if code in MESSAGES else 'GUARD_REJECTED'
        super().__init__(MESSAGES[self.code])


def sanitize_failure(value):
    """Rebuild even persisted diagnostics from allowlisted fields; never trust text."""
    value = value if isinstance(value, dict) else {}
    code = value.get('code')
    code = code if isinstance(code, str) and code in MESSAGES else 'UNKNOWN'
    stage = value.get('stage')
    row = dict(code=code, stage=stage if isinstance(stage, str) and stage in STAGES else 'begin', reason=MESSAGES[code])
    if code == 'PORT_BUSY' and isinstance(value.get('ports'), list):
        row['ports'] = sorted({p for p in value['ports'] if type(p) is int and p in (80, 443, 8766)})
    if code == 'COMMAND_FAILED':
        command = value.get('command')
        row['command'] = command if command in ('sc.exe', 'reg.exe', 'icacls.exe', 'powershell.exe', 'caddy.exe') else 'command'
        row['exit_code'] = value.get('exit_code') if type(value.get('exit_code')) is int and -(2 ** 31) <= value['exit_code'] <= 2 ** 32 - 1 else None
    if code == 'HTTP_ERROR' and type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        row['http_status'] = value['http_status']
    if value.get('journal_save_failed') is True:
        row['journal_save_failed'] = True
    return row


class FailureError(HttpsError):
    """Safe structured failure, retained across worker and GUI boundaries."""
    def __init__(self, failure):
        self.failure = sanitize_failure(failure)
        super().__init__(self.failure['reason'])


def failure_text(exc, stage='begin'):
    row = safe_failure(exc, stage)
    text = row['code'] + ' [' + row['stage'] + ']: ' + row['reason']
    if 'http_status' in row:
        text += ' HTTP ' + str(row['http_status']) + '.'
    if row.get('ports'):
        text += ' TCP: ' + ', '.join(map(str, row['ports'])) + '.'
    if row['code'] == 'COMMAND_FAILED':
        text += ' ' + row['command'] + ', exit_code=' + str(row['exit_code'])
    if row.get('journal_save_failed'):
        text += ' Не удалось сохранить диагностику в журнал; исход операции требует сверки.'
    return text

def safe_failure(exc, stage):
    if isinstance(exc, FailureError):
        return sanitize_failure(exc.failure)
    row = {'stage': stage if stage in STAGES else 'begin'}
    if isinstance(exc, GuardError):
        code = exc.code if exc.code in MESSAGES else 'GUARD_REJECTED'
        if code == 'PORT_BUSY':
            row['ports'] = exc.ports
    elif isinstance(exc, urllib.error.HTTPError):
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
    return sanitize_failure(row)

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
                self.events.put((False, FailureError(safe_failure(exc, 'begin'))))
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
        raise GuardError('CHECKSUM')
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
        raise GuardError('ZIP_INVALID') from None

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
            raise GuardError('INVENTORY_INVALID') from None

    def admin(self):
        if os.name != 'nt':
            raise GuardError('WINDOWS_REQUIRED')
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise GuardError('ADMIN_REQUIRED')

    def sc(self, *args):
        return self.run(['sc.exe', *args])

    def wait(self, name, state):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            svc = next((s for s in self.inventory()['services'] if s['Name'] == name), None)
            if state == 'Absent' and svc is None or (svc and svc['State'] == state):
                return
            time.sleep(0.25)
        raise GuardError('TIMEOUT')

    def protect(self, folder):
        raw = self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'])
        sid = raw.decode('ascii').strip()
        if not re.fullmatch('S-1-[0-9-]+', sid):
            raise GuardError('BACKUP_OWNER_INVALID')
        self.run(['icacls.exe', str(folder), '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'])

    def fetch(self):
        request = urllib.request.Request(DOWNLOAD, headers={'User-Agent': 'AI-Impuls-HTTPS-Setup'})
        with urllib.request.urlopen(request, timeout=30) as response:
            from urllib.parse import urlsplit
            target = urlsplit(response.url)
            if target.scheme != 'https' or target.hostname not in ('github.com', 'release-assets.githubusercontent.com'):
                raise GuardError('DOWNLOAD_SOURCE_INVALID')
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
                    raise GuardError('DOWNLOAD_LIMIT')
            data = b''.join(chunks)
        if len(data) > 64 * 1024 * 1024 or digest(data) != ARCHIVE_SHA256:
            raise GuardError('CHECKSUM')
        return data

def verify_service_process(service):
    if service['State'] == 'Running':
        args = arguments(service['PathName'])
        actual = arguments(service.get('ProcessCommand', ''))
        if not service.get('ProcessId') or not actual or [x.lower() for x in actual] != [x.lower() for x in args] or (str(PureWindowsPath(service.get('ProcessPath', ''))).lower() != str(PureWindowsPath(args[0])).lower()):
            raise GuardError('SERVICE_PROCESS_MISMATCH')

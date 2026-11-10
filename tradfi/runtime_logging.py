"""Best-effort runtime diagnostics: bounded queue, safe projection, worker-only I/O."""
import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

from .log_export import BACKUPS, ROTATION_BYTES, project_record
import json


class QuietRotatingHandler:
    """Worker-owned sink, deliberately not a logging.Handler.

    logging.shutdown acquires every registered handler's lock on the main
    thread. A filesystem-stalled worker must not make interpreter exit wait.
    """
    def __init__(self, path, *, maxBytes, backupCount, encoding):
        self.path = Path(path)
        self.max_bytes = maxBytes
        self.backups = backupCount
        self.encoding = encoding
        self.stream = self.path.open('a', encoding=encoding)
        self.size = self.path.stat().st_size

    def setFormatter(self, formatter):
        self.formatter = formatter

    def handle(self, record):
        self.emit(record)

    def emit(self, record):
        line = self.formatter.format(record) + '\n'
        size = len(line.encode(self.encoding))
        if self.size and self.size + size >= self.max_bytes:
            self.stream.close()
            for index in range(self.backups, 0, -1):
                source = Path(str(self.path) + '.' + str(index - 1)) if index > 1 else self.path
                target = Path(str(self.path) + '.' + str(index))
                if source.exists():
                    os.replace(source, target)
            self.stream = self.path.open('a', encoding=self.encoding)
            self.size = 0
        self.stream.write(line)
        self.stream.flush()
        self.size += size

    def close(self):
        self.stream.close()


class RuntimeLog(logging.Handler):
    def __init__(self, path, *, capacity=1024):
        super().__init__()
        self.path = path
        self.queue = Queue(maxsize=capacity)
        self.stopping = Event()
        self.worker = None
        self.dropped = 0
        self.failed = False
        self.logger = logging.getLogger('tradfi.engine')
        self.previous_level = self.logger.level

    def emit(self, record):
        # Project before enqueueing: raw messages, args and traceback never leave
        # the caller. Do not format arbitrary objects or exception strings.
        if self.stopping.is_set() or self.failed:
            return
        try:
            row = project_record(record)
            if row is not None:
                self.queue.put_nowait(json.dumps(row, sort_keys=True, ensure_ascii=True))
        except Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    def event(self, name):
        self.emit(logging.LogRecord('tradfi.paper_runtime', logging.INFO,
                                    '', 0, name, (), None))

    def _write(self):
        sink = None
        try:
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            sink = QuietRotatingHandler(path, maxBytes=ROTATION_BYTES,
                                       backupCount=BACKUPS, encoding='utf-8')
            sink.setFormatter(logging.Formatter('%(message)s'))
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    line = self.queue.get(timeout=.05)
                except Empty:
                    continue
                sink.handle(logging.LogRecord('safe', logging.INFO, '', 0, line, (), None))
        except Exception:
            self.failed = True
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    self.failed = True

    def __enter__(self):
        try:
            self.worker = Thread(target=self._write, name='paper-runtime-log', daemon=True)
            self.worker.start()
            self.logger.setLevel(logging.INFO)
            self.logger.addHandler(self)
            self.event('service_started')
        except Exception:
            self.failed = True
        return self

    def __exit__(self, kind, value, traceback):
        self.event('service_failed' if kind else 'service_stopped')
        self.logger.removeHandler(self)
        self.logger.setLevel(self.previous_level)
        self.stopping.set()
        # A stalled filesystem must never hold the PAPER owner indefinitely.
        if self.worker is not None and self.worker.ident is not None:
            self.worker.join(timeout=1)
        self.close()

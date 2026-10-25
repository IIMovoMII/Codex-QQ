"""Stage-specific, bounded UI diagnostics with a complete redacted local log."""
from __future__ import annotations

from collections import deque
import locale
import os
from pathlib import Path
import re
import subprocess
import tempfile


def redact(text: str, sensitive=()) -> str:
    for value in sorted(filter(None, sensitive), key=len, reverse=True):
        text = text.replace(str(value), '[已隐藏]')
    text = re.sub(r'(?i)(bearer\s+)[^\s"\']+', r'\1[已隐藏]', text)
    text = re.sub(r'''(?ix)(["']?(?:app[_-]?secret|client[_-]?secret|secret|api[_-]?key|bot[_-]?token|access[_-]?token|refresh[_-]?token|token|openid|user_id|session_id|authorization|password)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r'\1[已隐藏]', text)
    text = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[已隐藏]@', text)
    return text


class InstallFailure(RuntimeError):
    def __init__(self, stage: str, *, code=None, summary: str = '', log_path: Path | None = None):
        self.stage, self.code, self.log_path = stage, code, log_path
        message = stage + ('失败' if code is None else f'失败（退出码 {code}）')
        if summary:
            message += '\n' + summary[-2400:]
        if log_path:
            message += '\n诊断日志：' + str(log_path)
        super().__init__(message)


def decode(data: bytes) -> str:
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) if locale.getpreferredencoding(False) != 'utf-8' else 'gb18030', errors='replace')


def new_log(logs: Path, stage: str):
    try:
        logs.mkdir(parents=True, exist_ok=True)
        return tempfile.mkstemp(prefix='setup-', suffix='.log', dir=logs)
    except OSError as error:
        raise InstallFailure(stage, summary=f'无法保存诊断日志，系统错误码 {getattr(error, "winerror", None) or error.errno}；目录：{logs}') from None


def run_logged(command: list[str], *, stage: str, logs: Path, sensitive=(), cwd=None) -> None:
    """Drain both streams, retain full sanitized output, cap only the UI tail.

    The command is deliberately not echoed: future command arguments must not
    accidentally expose a credential. Compiler output is line streamed to disk.
    """
    fd, name = new_log(logs, stage)
    sensitive = tuple(sensitive) + tuple(value for key, value in os.environ.items()
                                        if re.search(r'(?i)(token|secret|password|api_key)$', key) and len(value) >= 6)
    tail = deque(maxlen=12)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as log:
        log.write('阶段：' + stage + '\n')
        log.flush()
        child = None

        def stop_child() -> None:
            if child is not None and child.poll() is None:
                try:
                    child.terminate()
                    child.wait(timeout=3.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        child.kill()
                        child.wait(timeout=3.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass

        def close_child_stdout() -> None:
            if child is not None and child.stdout is not None:
                try:
                    child.stdout.close()
                except OSError:
                    pass

        try:
            child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
            for line in child.stdout:
                safe = redact(decode(line), sensitive)
                log.write(safe)
                log.flush()
                tail.append(safe[-2400:])
            code = child.wait()
        except OSError as error:
            stop_child()
            close_child_stdout()
            message = f'无法启动子进程：{type(error).__name__}，系统错误码 {getattr(error, "winerror", None) or error.errno}'
            log.write(message + '\n')
            raise InstallFailure(stage, summary=message, log_path=Path(name)) from None
        except BaseException:
            # Ctrl+C/installer cancellation must not leave a compiler or
            # packaging child running after the wizard has closed. A forced
            # parent termination cannot run this block; Windows Job Object
            # protection remains the installer-level safeguard for that case.
            stop_child()
            close_child_stdout()
            raise
        finally:
            close_child_stdout()
        log.write(f'\n退出码：{code}\n')
    if code:
        raise InstallFailure(stage, code=code, summary=''.join(tail), log_path=Path(name))


def record_failure(logs: Path, stage: str, error: Exception) -> InstallFailure:
    """Only non-secret stage/class/errno, not arbitrary HTTP or TOML bodies."""
    summary = f'{type(error).__name__}（系统错误码 {getattr(error, "winerror", None) or getattr(error, "errno", None)}）'
    if type(error) is ValueError or hasattr(error, 'safe_detail'):
        summary = redact(getattr(error, 'safe_detail', str(error)))
    fd, name = new_log(logs, stage)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(stage + '\n' + summary + '\n')
    return InstallFailure(stage, summary=summary, log_path=Path(name))

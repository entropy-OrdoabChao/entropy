"""
Global Logging System — structured, timestamped, file + stdout.
Every module calls: from core.logger import get_logger
"""
import os
import json
import logging
from datetime import datetime, timezone

# Import paths — handle both package and standalone usage
try:
    from core.paths import LOGS_DIR, LOG_FILE
except ImportError:
    LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    LOG_FILE = os.path.join(LOGS_DIR, "execution.log")

os.makedirs(LOGS_DIR, exist_ok=True)

class StructuredFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mod = getattr(record, 'module_name', record.name).upper()
        stage = getattr(record, 'stage', '-')
        status = record.levelname
        msg = record.getMessage()
        err = getattr(record, 'error_detail', '')
        line = f"[{ts}] [{mod}] [{stage}] {msg} [{status}]"
        if err:
            line += f" ERROR: {err}"
        return line

_loggers = {}

class BeezleLogger:
    """Wrapper that adds .stage() and custom .error() for backward compat."""
    def __init__(self, name):
        self.logger = logging.getLogger(f'beezle.{name}')
        self.name = name
    
    def stage(self, stage, message, status='SUCCESS'):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f'[{ts}] [{self.name}] [{stage}] {message} [{status}]'
        self.logger.info(line)
        print(line)
    
    def error(self, stage, message, error=None):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        err = f' | {error}' if error else ''
        line = f'[{ts}] [{self.name}] [{stage}] {message}{err} [FAIL]'
        self.logger.error(line)
        print(line)
    
    def info(self, message, *args, **kwargs):
        self.logger.info(message, *args)
    
    def warning(self, message, *args, **kwargs):
        self.logger.warning(message, *args)
    
    def debug(self, message, *args, **kwargs):
        self.logger.debug(message, *args)


def get_logger(name: str):
    """Return a BeezleLogger with .stage() support."""
    return BeezleLogger(name)


def read_logs(n=50):
    """Read last N lines from execution log."""
    import os
    try:
        log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs', 'execution.log')
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                return lines[-n:]
        return []
    except Exception:
        return []

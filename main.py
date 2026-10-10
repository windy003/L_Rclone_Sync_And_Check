"""Daily rclone sync health check with email reporting."""

from __future__ import annotations

import logging
import os
import re
import smtplib
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

load_dotenv(Path(__file__).resolve().parent / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rclone-healthcheck")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    remote: str
    local_dir: Path
    rclone_bin: str
    check_time: str
    timeout: int
    poll_interval: int
    debounce_seconds: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    email_to: str
    email_subject_success: str
    email_subject_failure: str
    use_tls: bool
    use_ssl: bool

    @classmethod
    def from_env(cls) -> "Config":
        required = ("RCLONE_REMOTE", "LOCAL_SYNC_DIR", "SMTP_HOST", "SMTP_USERNAME",
                    "SMTP_PASSWORD", "SMTP_FROM", "EMAIL_TO")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError("Missing .env settings: " + ", ".join(missing))
        check_time = os.getenv("CHECK_TIME", "09:00")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", check_time):
            raise ValueError("CHECK_TIME must use 24-hour HH:MM format")
        remote = os.environ["RCLONE_REMOTE"].strip().rstrip("/")
        if ":" not in remote:
            raise ValueError("RCLONE_REMOTE must include a remote name, e.g. mydrive:folder")
        return cls(
            remote=remote,
            local_dir=Path(os.environ["LOCAL_SYNC_DIR"]).expanduser(),
            rclone_bin=os.getenv("RCLONE_BIN", "rclone"),
            check_time=check_time,
            timeout=env_int("SYNC_TIMEOUT_SECONDS", 60),
            poll_interval=env_int("POLL_INTERVAL_SECONDS", 30),
            debounce_seconds=env_int("WATCH_DEBOUNCE_SECONDS", 3),
            smtp_host=os.environ["SMTP_HOST"],
            smtp_port=env_int("SMTP_PORT", 587),
            smtp_username=os.environ["SMTP_USERNAME"],
            smtp_password=os.environ["SMTP_PASSWORD"],
            smtp_from=os.environ["SMTP_FROM"],
            email_to=os.environ["EMAIL_TO"],
            email_subject_success=os.getenv("EMAIL_SUBJECT_SUCCESS", "[rclone同步成功] {time}"),
            email_subject_failure=os.getenv("EMAIL_SUBJECT_FAILURE", "[rclone同步异常] {time}"),
            use_tls=os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
            use_ssl=os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"},
        )


def next_run(now: datetime, check_time: str) -> datetime:
    hour, minute = map(int, check_time.split(":"))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return scheduled if scheduled > now else scheduled + timedelta(days=1)


def create_marker(config: Config) -> tuple[Path, str]:
    config.local_dir.mkdir(parents=True, exist_ok=True)
    name = f"rclone-healthcheck-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.txt"
    path = config.local_dir / name
    path.write_text(f"rclone sync health check\ncreated_at={datetime.now().astimezone().isoformat()}\n", encoding="utf-8")
    return path, name


def remote_has_marker(config: Config, name: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [config.rclone_bin, "lsjson", f"{config.remote}/{name}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, "Probe file found on rclone remote."
    return False, (result.stderr or result.stdout or f"rclone exit code {result.returncode}").strip()


def run_check(config: Config) -> tuple[bool, str, str]:
    path, name = create_marker(config)
    log.info("Created probe file: %s", path)
    deadline = time.monotonic() + config.timeout
    detail = "尚未查询远端"
    try:
        while True:
            found, detail = remote_has_marker(config, name)
            if found:
                return True, f"Sync succeeded: found {config.remote}/{name}", name
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, f"Sync failed or timed out: probe file not found on {config.remote}/{name} within {config.timeout} seconds. Last rclone result: {detail}", name
            time.sleep(min(config.poll_interval, remaining))
    except Exception as exc:
        return False, f"Health check error: {type(exc).__name__}: {exc}", name


def delete_probe(config: Config, name: str) -> None:
    local_path = config.local_dir / name
    try:
        local_path.unlink(missing_ok=True)
    except OSError:
        log.exception("Could not delete local probe file %s", local_path)

    try:
        result = subprocess.run(
            [config.rclone_bin, "deletefile", f"{config.remote}/{name}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode == 0:
            log.info("Deleted probe file from remote: %s/%s", config.remote, name)
        else:
            log.warning("Could not delete remote probe file %s/%s: %s", config.remote, name,
                        (result.stderr or result.stdout).strip())
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Could not delete remote probe file %s/%s", config.remote, name)


class SyncOnChangeHandler(FileSystemEventHandler):
    """Debounce filesystem events and mirror the local directory to rclone."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._sync_lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return
        if event.is_directory and event.event_type not in {"created", "deleted", "moved"}:
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.config.debounce_seconds, self.sync)
            self._timer.daemon = True
            self._timer.start()

    def sync(self) -> None:
        self._sync_lock.acquire()
        try:
            log.info("Syncing %s to %s", self.config.local_dir, self.config.remote)
            result = subprocess.run(
                [self.config.rclone_bin, "sync", str(self.config.local_dir), self.config.remote],
                capture_output=True, text=True, timeout=self.config.timeout, check=False,
            )
            if result.returncode == 0:
                log.info("Sync completed successfully")
            else:
                log.error("rclone sync failed (exit %s): %s", result.returncode,
                          (result.stderr or result.stdout).strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.exception("Could not sync local changes: %s", exc)
        finally:
            self._sync_lock.release()

    def cancel_pending(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()


def send_email(config: Config, success: bool, report: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = EmailMessage()
    subject_template = config.email_subject_success if success else config.email_subject_failure
    message["Subject"] = subject_template.replace("{time}", now)
    message["From"] = config.smtp_from
    message["To"] = config.email_to
    message.set_content(f"Check time: {now}\nResult: {'success' if success else 'failure'}\n\n{report}\n")
    smtp_class = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
    with smtp_class(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if config.use_tls and not config.use_ssl:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(message)


def main() -> int:
    try:
        config = Config.from_env()
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    config.local_dir.mkdir(parents=True, exist_ok=True)
    handler = SyncOnChangeHandler(config)
    observer = Observer()
    observer.schedule(handler, str(config.local_dir), recursive=True)
    observer.start()
    log.info("Watching %s; changes sync to %s", config.local_dir, config.remote)
    handler.sync()
    log.info("Daily check scheduled for %s local time", config.check_time)
    try:
        while True:
            now = datetime.now().astimezone()
            scheduled = next_run(now, config.check_time)
            log.info("Next check: %s", scheduled.isoformat(timespec="minutes"))
            time.sleep(max(0.0, (scheduled - now).total_seconds()))
            success, report, marker_name = False, "", None
            try:
                success, report, marker_name = run_check(config)
            except Exception as exc:
                report = f"检查程序发生错误：{type(exc).__name__}: {exc}"
                log.exception("Health check failed")
            try:
                send_email(config, success, report)
                log.info("Email report sent to %s", config.email_to)
            except Exception:
                log.exception("Could not send report email")
            finally:
                if marker_name is not None:
                    delete_probe(config, marker_name)
    except KeyboardInterrupt:
        log.info("Stopping directory watcher")
    finally:
        handler.cancel_pending()
        observer.stop()
        observer.join()


if __name__ == "__main__":
    raise SystemExit(main())

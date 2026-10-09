"""Daily rclone sync health check with email reporting."""

from __future__ import annotations

import argparse
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

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
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    email_to: str
    use_tls: bool

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
            smtp_host=os.environ["SMTP_HOST"],
            smtp_port=env_int("SMTP_PORT", 587),
            smtp_username=os.environ["SMTP_USERNAME"],
            smtp_password=os.environ["SMTP_PASSWORD"],
            smtp_from=os.environ["SMTP_FROM"],
            email_to=os.environ["EMAIL_TO"],
            use_tls=os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
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
        return True, "鏂囦欢宸插湪 rclone 杩滅鎵惧埌銆?
    return False, (result.stderr or result.stdout or f"rclone exit code {result.returncode}").strip()


def run_check(config: Config) -> tuple[bool, str]:
    path, name = create_marker(config)
    log.info("Created probe file: %s", path)
    deadline = time.monotonic() + config.timeout
    detail = "灏氭湭鏌ヨ杩滅"
    while True:
        found, detail = remote_has_marker(config, name)
        if found:
            return True, f"鍚屾鎴愬姛锛氳繙绔?{config.remote}/{name} 宸叉壘鍒版帰娴嬫枃浠躲€?
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"鍚屾澶辫触鎴栬秴鏃讹細{config.timeout} 绉掑唴鏈壘鍒拌繙绔枃浠?{config.remote}/{name}銆傛渶杩戜竴娆?rclone 淇℃伅锛歿detail}"
        time.sleep(min(config.poll_interval, remaining))


def send_email(config: Config, success: bool, report: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = EmailMessage()
    message["Subject"] = f"[rclone鍚屾{'鎴愬姛' if success else '寮傚父'}] {now}"
    message["From"] = config.smtp_from
    message["To"] = config.email_to
    message.set_content(f"妫€鏌ユ椂闂达細{now}\n缁撴灉锛歿'鎴愬姛' if success else '澶辫触'}\n\n{report}\n")
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if config.use_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check rclone sync status and email a report.")
    parser.add_argument("--now", action="store_true", help="run one check immediately, then exit")
    args = parser.parse_args()
    try:
        config = Config.from_env()
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    if args.now:
        success, report = False, ""
        try:
            success, report = run_check(config)
        except Exception as exc:
            report = f"妫€鏌ョ▼搴忓彂鐢熼敊璇細{type(exc).__name__}: {exc}"
            log.exception("Health check failed")
        try:
            send_email(config, success, report)
            log.info("Email report sent to %s", config.email_to)
        except Exception:
            log.exception("Could not send report email")
            return 3
        return 0 if success else 1
    log.info("Daily check scheduled for %s local time", config.check_time)
    while True:
        now = datetime.now().astimezone()
        scheduled = next_run(now, config.check_time)
        log.info("Next check: %s", scheduled.isoformat(timespec="minutes"))
        time.sleep(max(0.0, (scheduled - now).total_seconds()))
        success, report = False, ""
        try:
            success, report = run_check(config)
        except Exception as exc:
            report = f"妫€鏌ョ▼搴忓彂鐢熼敊璇細{type(exc).__name__}: {exc}"
            log.exception("Health check failed")
        try:
            send_email(config, success, report)
            log.info("Email report sent to %s", config.email_to)
        except Exception:
            log.exception("Could not send report email")


if __name__ == "__main__":
    raise SystemExit(main())


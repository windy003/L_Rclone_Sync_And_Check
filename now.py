"""Run one rclone sync health check and email its result."""

import logging

from main import Config, delete_probe, run_check, send_email

log = logging.getLogger("rclone-healthcheck")


def main() -> int:
    try:
        config = Config.from_env()
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2

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
        return 3
    finally:
        if marker_name is not None:
            delete_probe(config, marker_name)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

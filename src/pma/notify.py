"""Approval-request notifications.

Channels are configured in src/config.yaml under `notifications:` and are
all opt-in. The webhook channel POSTs a small JSON payload to any endpoint
(WeCom/Slack/custom); the email channel uses stdlib smtplib and reads
the SMTP password only from the PMA_SMTP_PASSWORD environment variable so
no secret ever lands in config files or git history.
"""

import json
import os
import platform
import subprocess


def send(cfg, title, message):
    """Dispatch a notification to all enabled channels. Returns the list of
    channels that accepted the message; failures are non-fatal (approval
    requests also live as files under runs/approvals/)."""
    ncfg = (cfg or {}).get("notifications") or {}
    if not ncfg.get("enabled"):
        return []
    sent = []
    if ncfg.get("desktop") and _desktop(title, message):
        sent.append("desktop")
    if ncfg.get("webhook_url") and _webhook(ncfg["webhook_url"],
                                            title, message):
        sent.append("webhook")
    email = ncfg.get("email") or {}
    if email.get("host") and _email(email, title, message):
        sent.append("email")
    return sent


def _desktop(title, message):
    if platform.system() != "Darwin":
        return False
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s"'
             % (message.replace('"', "'"), title.replace('"', "'"))],
            check=True, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _webhook(url, title, message):
    import requests
    try:
        resp = requests.post(url, json={"text": "%s\n%s" % (title, message)},
                             timeout=10)
        return resp.ok
    except Exception:
        return False


def _email(email_cfg, title, message):
    import smtplib
    from email.mime.text import MIMEText

    password = os.environ.get("PMA_SMTP_PASSWORD", "")
    if not email_cfg.get("to") or not email_cfg.get("from"):
        return False
    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]
    try:
        with smtplib.SMTP_SSL(email_cfg["host"],
                              int(email_cfg.get("port", 465)),
                              timeout=15) as smtp:
            if email_cfg.get("username"):
                smtp.login(email_cfg["username"], password)
            smtp.sendmail(email_cfg["from"], [email_cfg["to"]],
                          msg.as_string())
        return True
    except Exception:
        return False

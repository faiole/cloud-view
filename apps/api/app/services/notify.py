"""Envio de notificações: e-mail, Slack, Microsoft Teams e webhook assinado (HMAC-SHA256).

URLs de webhook são validadas na criação e de novo no envio:
- Slack só em hooks.slack.com; Teams só em domínios da Microsoft;
- webhook genérico só HTTPS e nunca para IPs privados/locais (evita SSRF).
"""

import hashlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.models import Alert, NotificationChannel

SEVERITY_PT = {"info": "Informativo", "warning": "Atenção", "critical": "Crítico"}
SLACK_HOSTS = {"hooks.slack.com"}
TEAMS_SUFFIXES = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")


class InvalidTarget(ValueError):
    pass


def _resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def validate_url(channel_type: str, url: str, *, resolve: bool = True) -> str:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.username or u.password:
        raise InvalidTarget("use uma URL https:// sem usuário/senha")
    if channel_type == "slack" and host not in SLACK_HOSTS:
        raise InvalidTarget("URL do Slack deve ser https://hooks.slack.com/...")
    if channel_type == "teams" and not host.endswith(TEAMS_SUFFIXES):
        raise InvalidTarget("URL do Teams deve ser de um webhook/fluxo da Microsoft")
    if channel_type == "webhook" and resolve and not _resolves_public(host):
        raise InvalidTarget("o endereço do webhook não pode ser interno ou privado")
    return url


def panel_link(alert: Alert | None) -> str:
    base = get_settings().public_url.rstrip("/")
    if alert and alert.tenant_id:
        return f"{base}/clientes/{alert.tenant_id}/alertas"
    return f"{base}/alertas"


def payload(alert: Alert | None, tenant_name: str | None) -> dict:
    if alert is None:
        return {
            "type": "test",
            "title": "Teste do Cloud View",
            "message": "Se você recebeu esta mensagem, o canal está funcionando.",
            "severity": "info",
            "link": panel_link(None),
        }
    return {
        "type": alert.type,
        "id": alert.id,
        "tenant": tenant_name,
        "category": alert.category,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message,
        "data": alert.data,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "link": panel_link(alert),
    }


def _text(p: dict) -> str:
    who = f"[{p['tenant']}] " if p.get("tenant") else ""
    return f"{SEVERITY_PT.get(p['severity'], p['severity'])}: {who}{p['title']}\n{p['message']}\n{p['link']}"


def send_email(
    ch: NotificationChannel, p: dict, attachments: list[tuple[str, bytes, str]] | None = None
) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise RuntimeError("SMTP não configurado (CV_SMTP_HOST)")
    msg = EmailMessage()
    msg["Subject"] = f"[Cloud View] {SEVERITY_PT.get(p['severity'], '')}: {p['title']}"[:250]
    msg["From"] = s.smtp_from
    msg["To"] = ", ".join(ch.emails)
    msg.set_content(_text(p) + "\n\n— Cloud View")
    for filename, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        if s.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password or "")
        smtp.send_message(msg)


def _post(url: str, body: bytes, headers: dict) -> None:
    r = httpx.post(url, content=body, headers={"Content-Type": "application/json", **headers}, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"destino respondeu {r.status_code}")


def send_slack(url: str, p: dict) -> None:
    validate_url("slack", url, resolve=False)
    emoji = {"critical": ":red_circle:", "warning": ":large_orange_circle:"}.get(
        p["severity"], ":large_blue_circle:"
    )
    body = {
        "text": _text(p),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{emoji} *{p['title']}*\n{p['message']}"},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{p['link']}|Abrir no Cloud View>"}],
            },
        ],
    }
    _post(url, json.dumps(body).encode(), {})


def send_teams(url: str, p: dict) -> None:
    """Formato Adaptive Card aceito pelos webhooks do Teams (fluxo 'Quando uma solicitação de webhook é recebida')."""
    validate_url("teams", url, resolve=False)
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": p["title"],
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
                "color": {"critical": "Attention", "warning": "Warning"}.get(p["severity"], "Default"),
            },
            {"type": "TextBlock", "text": p["message"], "wrap": True},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Abrir no Cloud View", "url": p["link"]}],
    }
    body = {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }
    _post(url, json.dumps(body).encode(), {})


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def send_webhook(url: str, signing_secret: str, p: dict) -> None:
    validate_url("webhook", url)
    body = json.dumps(p, default=str).encode()
    ts = str(int(time.time()))
    _post(url, body, {"X-CloudView-Timestamp": ts, "X-CloudView-Signature": sign(signing_secret, ts, body)})


def deliver(ch: NotificationChannel, p: dict, secret: dict | None) -> None:
    if ch.type == "email":
        send_email(ch, p)
    elif ch.type == "slack":
        send_slack(secret["url"], p)
    elif ch.type == "teams":
        send_teams(secret["url"], p)
    elif ch.type == "webhook":
        send_webhook(secret["url"], secret["signing_secret"], p)
    else:
        raise ValueError(f"canal desconhecido: {ch.type}")

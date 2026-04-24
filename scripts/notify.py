#!/usr/bin/env python3
"""Notification sender - supports Telegram Bot API and WeCom (企业微信) webhook.

Usage:
  python3 scripts/notify.py --message "text"
  python3 scripts/notify.py --file path/to/message.txt
  python3 scripts/notify.py --file msg.txt --channel telegram
  python3 scripts/notify.py --file msg.txt --channel wechat
  echo "text" | python3 scripts/notify.py

Config (in config.json):
  {
    "notifications": {
      "telegram": {
        "bot_token": "123456:ABC-DEF...",   # from @BotFather
        "chat_id": "654604920"              # your personal chat ID
      },
      "wechat": {
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
      }
    }
  }
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'config.json'

# Telegram messages are capped at 4096 chars; WeCom text at 2048 bytes.
TELEGRAM_MAX_CHARS = 4000
WECHAT_MAX_BYTES = 2000


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open('r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json; charset=utf-8'}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _truncate(text: str, max_chars: int, suffix: str = '\n…(内容已截断)') -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - len(suffix)] + suffix


def send_telegram(message: str, config: dict) -> bool | None:
    """Send via Telegram Bot API (no external dependencies). Returns None if not configured."""
    tg = config.get('notifications', {}).get('telegram', {})
    bot_token = tg.get('bot_token', '').strip()
    chat_id = str(tg.get('chat_id', '')).strip()
    if not bot_token or not chat_id:
        print('[notify] Telegram: not configured (missing bot_token or chat_id)', file=sys.stderr)
        return None

    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    text = _truncate(message, TELEGRAM_MAX_CHARS)
    try:
        result = _post_json(url, {'chat_id': chat_id, 'text': text})
        if result.get('ok'):
            print('[notify] Telegram: sent successfully')
            return True
        print(f'[notify] Telegram API error: {result}', file=sys.stderr)
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'[notify] Telegram HTTP {e.code}: {body}', file=sys.stderr)
        return False
    except Exception as e:
        print(f'[notify] Telegram exception: {e}', file=sys.stderr)
        return False


def send_wechat(message: str, config: dict) -> bool | None:
    """Send via WeCom (企业微信) group bot webhook. Returns None if not configured.

    Setup:
      1. 在企业微信群里添加「群机器人」
      2. 复制 Webhook URL 填入 config.json notifications.wechat.webhook_url
    """
    wc = config.get('notifications', {}).get('wechat', {})
    webhook_url = wc.get('webhook_url', '').strip()
    if not webhook_url:
        print('[notify] WeChat: not configured (missing webhook_url)', file=sys.stderr)
        return None

    # WeCom text limit: 2048 bytes
    encoded = message.encode('utf-8')
    if len(encoded) > WECHAT_MAX_BYTES:
        message = encoded[:WECHAT_MAX_BYTES].decode('utf-8', errors='ignore') + '\n…(内容已截断)'

    try:
        result = _post_json(webhook_url, {'msgtype': 'text', 'text': {'content': message}})
        if result.get('errcode') == 0:
            print('[notify] WeChat: sent successfully')
            return True
        print(f'[notify] WeChat API error: {result}', file=sys.stderr)
        return False
    except Exception as e:
        print(f'[notify] WeChat exception: {e}', file=sys.stderr)
        return False


SENDERS: dict[str, object] = {
    'telegram': send_telegram,
    'wechat': send_wechat,
}


def main() -> None:
    p = argparse.ArgumentParser(description='Send notifications to configured channels')
    p.add_argument('--message', '-m', help='Message text')
    p.add_argument('--file', '-f', help='Read message from file')
    p.add_argument(
        '--channel', '-c', action='append', dest='channels',
        choices=list(SENDERS.keys()),
        help='Channel to send to (can repeat; default: all configured channels)',
    )
    args = p.parse_args()

    if args.file:
        message = Path(args.file).read_text(encoding='utf-8').strip()
    elif args.message:
        message = args.message.strip()
    else:
        message = sys.stdin.read().strip()

    if not message:
        print('[notify] No message to send', file=sys.stderr)
        sys.exit(1)

    config = load_config()
    notification_config = config.get('notifications', {})

    channels: list[str]
    if args.channels:
        channels = args.channels
    else:
        # Auto-detect: use all channels that have non-empty config
        channels = [ch for ch in SENDERS if notification_config.get(ch)]

    if not channels:
        print('[notify] No channels configured. Add notifications section to config.json', file=sys.stderr)
        sys.exit(1)

    failures = []
    for channel in channels:
        sender = SENDERS[channel]
        ok = sender(message, config)  # type: ignore[operator]
        if ok is False:  # None means not configured (skip), False means send failed
            failures.append(channel)

    if failures:
        print(f'[notify] Failed channels: {", ".join(failures)}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

"""Cloud-side connection/auth check. Never fetches or deletes messages."""
import json
import os
from pathlib import Path

from check_mailbox import check


def main():
    config = json.loads(Path(__file__).with_name('mailbox.json').read_text())
    config['host'] = os.environ.get('BEAR_MAIL_HOST', config['host'])
    config['username'] = os.environ.get('BEAR_MAIL_ADDRESS', config['username'])
    password = os.environ.pop('BEAR_MAIL_PASSWORD', None)
    if not password or not config['host'] or not config['username']:
        raise SystemExit('Required cloud mailbox settings are missing.')
    try:
        check(config, password)
    finally:
        password = None
    print('TLS/authentication/read-only INBOX check succeeded. No messages fetched, removed, or sent.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Mailbox check failed; credentials are omitted.')

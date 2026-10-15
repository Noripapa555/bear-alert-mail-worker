"""Interactive IMAP connection check. Does not fetch mail or send notifications.

Run in your own terminal; the password is hidden and is never saved.
Uses only Python's standard library and certificate-verified TLS.
"""
import argparse
import getpass
import imaplib
import json
from pathlib import Path
import ssl
import socket


class CheckError(ValueError):
    pass


def connect(config, factory):
    try:
        return factory(config['host'], config['port'],
                       ssl_context=ssl.create_default_context(), timeout=30)
    except socket.gaierror:
        raise CheckError('E01: サーバー名を解決できません。サーバー名・ネット接続を確認してください。') from None
    except ssl.SSLError:
        raise CheckError('E02: 暗号化接続の確認に失敗しました。証明書の検証は無効にしないでください。') from None
    except (OSError, imaplib.IMAP4.error):
        raise CheckError('E03: IMAPサーバーに接続できません。通信制限やサーバー稼働状況を確認してください。') from None


def check(config, password, factory=imaplib.IMAP4_SSL):
    client = connect(config, factory)
    try:
        try:
            status, _ = client.login(config['username'], password)
        except imaplib.IMAP4.abort:
            raise CheckError('E05: 認証中に接続が切れました。') from None
        except (imaplib.IMAP4.error, UnicodeError):
            raise CheckError('E04: 認証に失敗しました。専用アドレス作成時のメールパスワードを確認してください。') from None
        if status != 'OK':
            raise CheckError('E04: 認証に失敗しました。専用アドレス作成時のメールパスワードを確認してください。')
        # EXAMINE instead of SELECT: never mark messages read or change mail.
        status, _ = client.select(config['folder'], readonly=True)
        if status != 'OK':
            raise CheckError('E06: 認証後、受信箱を開けませんでした。')
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network-only', action='store_true', help='接続のみ。認証しません。')
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name('mailbox.json').read_text())
    print('接続先: ' + config['host'] + ' / ' + config['username'])
    if args.network_only:
        try:
            client = connect(config, imaplib.IMAP4_SSL)
            client.logout()
        except CheckError as error:
            print(str(error))
            return 1
        except (OSError, imaplib.IMAP4.error):
            print('E05: 接続が切れました。')
            return 1
        print('暗号化接続成功。認証・メール取得はしていません。')
        return 0
    password = getpass.getpass('専用メールのパスワード（非表示・保存しません）: ')
    try:
        check(config, password)
    except CheckError as error:
        print(str(error))
        return 1
    except (OSError, imaplib.IMAP4.error, ValueError):
        # Server exception text may contain account details. Never print it.
        print('E05: 受信箱の確認中に通信または処理が失敗しました。')
        return 1
    finally:
        password = None
    print('接続成功。本文取得・既読変更・削除・警報配信は行っていません。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

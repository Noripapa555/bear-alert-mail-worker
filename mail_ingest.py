"""Bounded IMAP -> durable private queue -> targeted removal.

Default is read-only inspection, with counts only in logs. --apply saves and
removes processed messages. Credentials come from a cloud secret environment.
No source is enabled until its delivery format and reuse terms are reviewed.
"""
import argparse
import datetime as dt
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

from check_mailbox import connect
from mail_retention import MailIdentity, process_then_remove

UTC = dt.timezone.utc
MAX_BYTES = 256 * 1024
CONTROL = re.compile(r'仮登録|登録確認|登録完了|認証|配信設定|登録用|パスワード|確認コード|verification|confirm.*subscription', re.I)
BEAR = re.compile(r'ヒグマ|ツキノワグマ|クマ|熊')


def verify_signature(raw, source, *, dnsfunc=None):
    """Verify real DKIM signatures, never trust arbitrary Authentication-Results.

    Exact sender and signing domains are adapter configuration, not inferred.
    Signed Date/Subject/From and a fully signed body are required.
    """
    import dkim
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    for index, signature in enumerate(msg.get_all('DKIM-Signature', [])):
        tags = dict(re.findall(r'(?:^|;)\s*([a-z]+)\s*=\s*([^;]*)', str(signature)))
        domain = tags.get('d', '').strip().lower()
        signed = {s.strip().lower() for s in tags.get('h', '').split(':')}
        if domain not in source['signing_domains'] or 'l' in tags:
            continue
        if not {'from', 'subject', 'date'}.issubset(signed):
            continue
        options = {} if dnsfunc is None else {'dnsfunc': dnsfunc}
        try:
            if dkim.DKIM(raw, timeout=5).verify(idx=index, **options):
                return True
        except (dkim.DKIMException, ValueError, UnicodeError):
            continue
    return False


def classify(raw, sources, *, now=None, verifier=verify_signature):
    now = now or dt.datetime.now(UTC)
    if len(raw) > MAX_BYTES:
        return {'decision': 'unknown'}
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if msg.defects or any(len(msg.get_all(h, [])) != 1 for h in ('From', 'Subject', 'Date')):
        return {'decision': 'unknown'}
    addresses = getaddresses(msg.get_all('From', []))
    if len(addresses) != 1:
        return {'decision': 'unknown'}
    sender = addresses[0][1].lower()
    matched = [s for s in sources if s.get('enabled') is True and sender in s['senders']]
    if len(matched) != 1:
        return {'decision': 'unknown'}
    source = matched[0]
    if not source.get('reuse_reviewed') or not verifier(raw, source):
        return {'decision': 'unknown'}
    subject = str(msg['Subject'])
    part = msg.get_body(preferencelist=('plain',))
    if part is None or part.get_content_disposition() == 'attachment':
        return {'decision': 'unknown'}
    body = part.get_content()
    if not isinstance(body, str) or part.defects:
        return {'decision': 'unknown'}
    # Protect registration tokens even when a bear service appears in the subject.
    if CONTROL.search(subject) or CONTROL.search(body[:400]):
        return {'decision': 'registration'}
    if not re.search(source['bear_subject_pattern'], subject) or not BEAR.search(subject + body):
        if any(re.fullmatch(p, subject) for p in source.get('unnecessary_subject_patterns', [])):
            return {'decision': 'unnecessary', 'source': source['id']}
        return {'decision': 'unknown'}
    extracted = re.fullmatch(source['public_body_pattern'], body.strip(), re.S)
    if not extracted:
        return {'decision': 'unknown'}
    text = extracted.group('alert').strip()
    # Adapters must exclude private footer URLs/addresses. Refuse suspect content.
    if not text or len(text) > 800 or re.search(r'https?://|\S+@\S+|配信停止|登録解除', text):
        return {'decision': 'unknown'}
    issued = parsedate_to_datetime(str(msg['Date']))
    if issued.tzinfo is None or issued > now + dt.timedelta(minutes=5):
        return {'decision': 'unknown'}
    issued = issued.astimezone(UTC)
    content = {'source': source['id'], 'sourceName': source['name'],
               'sourceUrl': source['official_url'], 'region': source['region'],
               'subject': subject[:120], 'text': text, 'issuedAt': issued.isoformat(),
               'expiresAt': (issued + dt.timedelta(hours=24)).isoformat()}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'decision': 'bear', 'id': 'mail_' + digest, 'content': content,
            'expired': issued + dt.timedelta(hours=24) <= now}


def run(client, config, sources, store=None, *, apply=False, max_messages=20, verifier=verify_signature):
    status, _ = client.select(config['folder'], readonly=not apply)
    if status != 'OK':
        raise RuntimeError('Cannot open mailbox')
    _, validity = client.response('UIDVALIDITY')
    if not validity or not validity[0]:
        raise RuntimeError('Missing UIDVALIDITY')
    uid_validity = validity[0].decode('ascii')
    status, data = client.uid('SEARCH', None, 'ALL')
    if status != 'OK':
        raise RuntimeError('Search failed')
    counts = dict(inspected=0, retained=0, saved=0, removed=0, errors=0)
    started = time.monotonic()
    # Rotate over retained mail with a persisted cursor; do not starve new mail.
    uids = data[0].split() if data and data[0] else []
    cursor = store.cursor(config['username'], uid_validity) if apply else 0
    uids = [u for u in uids if int(u) > cursor] + [u for u in uids if int(u) <= cursor]
    for uid_bytes in uids[:max_messages]:
        if time.monotonic() - started > 40:
            break
        uid = uid_bytes.decode('ascii')
        try:
            # Ask size first. Oversize mail is retained, not silently discarded.
            status, info = client.uid('FETCH', uid, '(RFC822.SIZE)')
            size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(x for x in info if isinstance(x, bytes)))
            if status != 'OK' or not size or int(size[1]) > MAX_BYTES:
                counts['retained'] += 1
                continue
            status, data = client.uid('FETCH', uid, '(BODY.PEEK[])')
            raw = next((x[1] for x in data if isinstance(x, tuple)), None)
            if status != 'OK' or not isinstance(raw, bytes):
                raise RuntimeError('Fetch failed')
            counts['inspected'] += 1
            result = classify(raw, sources, verifier=verifier)
            if not apply or result['decision'] not in ('bear', 'unnecessary'):
                counts['retained'] += 1
                continue
            identity = MailIdentity(config['username'] + '/' + config['folder'], uid_validity,
                                    uid, hashlib.sha256(raw).hexdigest())
            # Save even on servers without UIDPLUS, so the queue is not blocked.
            store.save(identity, result)
            counts['saved'] += 1
            removed = process_then_remove(client, identity, raw, decision=result['decision'],
                                         source_verified=True,
                                         persist_result=lambda i, _: store.receipt(i))
            counts['removed' if removed == 'removed' else 'retained'] += 1
        except Exception:
            # No message text, addresses, authentication errors or tokens in logs.
            counts['errors'] += 1
        finally:
            if apply:
                store.set_cursor(config['username'], uid_validity, int(uid))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--publish', action='store_true', help='Publish queued email alerts via FCM')
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error('--publish requires --apply')
    config = json.loads(Path(__file__).with_name('mailbox.json').read_text())
    # The standalone cloud repository uses variables instead of publishing the
    # user's mailbox address or host in source code.
    config['host'] = os.environ.get('BEAR_MAIL_HOST', config['host'])
    config['username'] = os.environ.get('BEAR_MAIL_ADDRESS', config['username'])
    sources = json.loads(Path(__file__).with_name('mail_sources.json').read_text())['sources']
    if not any(s.get('enabled') for s in sources):
        raise SystemExit('No reviewed mail sources configured; nothing read or removed.')
    password = os.environ.pop('BEAR_MAIL_PASSWORD', None)
    if not password:
        raise SystemExit('Missing BEAR_MAIL_PASSWORD cloud secret.')
    import imaplib
    from mail_store import MailStore
    store = MailStore() if args.apply else None
    client = connect(config, imaplib.IMAP4_SSL)
    try:
        client.login(config['username'], password)
        password = None
        status, capabilities = client.capability()
        if status != 'OK':
            raise RuntimeError('Cannot confirm server capabilities')
        client.capabilities = tuple(b' '.join(capabilities).upper().split())
        print(json.dumps(run(client, config, sources, store, apply=args.apply)))
        if args.publish:
            print(json.dumps(store.dispatch()))
    finally:
        password = None
        # LOGOUT does not implicitly expunge unrelated deleted messages.
        client.logout()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Mail worker failed. Credentials and message contents are omitted.')

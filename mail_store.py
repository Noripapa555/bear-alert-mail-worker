"""Private Firestore receipts and crash-conservative FCM dispatch.

Uses Application Default Credentials (cloud workload identity recommended).
No service-account keys or OAuth tokens belong in the repository.
"""
import datetime as dt
import hashlib
import json

from mail_protocol import BASE, PROJECT, LOCALES, encode, decode

LABELS = {
    'ja': ('熊情報', '自治体配信メール・日本語原文'),
    'ko': ('곰 정보', '공공기관 이메일 · 일본어 원문'),
    'zh': ('熊出没信息', '官方邮件 · 日语原文'),
    'fr': ('Information ours', 'Courriel officiel · texte original japonais'),
    'en': ('Bear information', 'Official email · Japanese original'),
    'es': ('Información sobre osos', 'Correo oficial · original en japonés'),
}
REGIONS = set('hokkaido aomori iwate miyagi akita yamagata fukushima ibaraki tochigi gunma saitama chiba tokyo kanagawa niigata toyama ishikawa fukui yamanashi nagano gifu shizuoka aichi mie shiga kyoto osaka hyogo nara wakayama tottori shimane okayama hiroshima yamaguchi tokushima kagawa ehime kochi fukuoka saga nagasaki kumamoto oita miyazaki kagoshima okinawa'.split())


def alert_payload(content):
    if content['region'] not in REGIONS:
        raise ValueError('Invalid prefecture')
    if len(content['sourceName']) > 100 or len(content['sourceUrl']) > 500:
        raise ValueError('Source metadata too long')
    if not content['sourceUrl'].startswith('https://'):
        raise ValueError('Official source URL must be HTTPS')
    titles = {l: LABELS[l][0] + ' / ' + content['sourceName'] for l in LOCALES}
    bodies = {l: LABELS[l][1] + '\n' + content['text'] + '\n' + content['sourceName'] for l in LOCALES}
    return dict(titles=titles, bodies=bodies, region=content['region'],
                sourceUrl=content['sourceUrl'], sourceName=content['sourceName'],
                sourceType='municipality_email', originalLanguage='ja')


def receipt_id(identity):
    return hashlib.sha256(json.dumps(identity.__dict__, sort_keys=True).encode()).hexdigest()


class MailStore:
    def __init__(self, session=None):
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            session = AuthorizedSession(credentials)
        self.session = session

    def get(self, collection, key):
        response = self.session.get(BASE + '/' + collection + '/' + key, timeout=10)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError('Database read failed')
        document = response.json()
        return ({k: decode(v) for k, v in document['fields'].items()}, document['updateTime'])

    def create(self, collection, key, data):
        fields = {k: encode(v) for k, v in data.items()}
        response = self.session.post(BASE + ':commit', json={'writes': [{
            'update': {'name': BASE.split('/v1/')[1] + '/' + collection + '/' + key, 'fields': fields},
            'currentDocument': {'exists': False}}]}, timeout=10)
        if response.status_code not in (200, 409):
            raise RuntimeError('Database save failed')
        return response.status_code == 200

    def update(self, collection, key, fields, revision=None):
        import urllib.parse
        query = '&'.join('updateMask.fieldPaths=' + k for k in fields)
        if revision:
            query += '&currentDocument.updateTime=' + urllib.parse.quote(revision, safe='')
        response = self.session.patch(BASE + '/' + collection + '/' + key + '?' + query,
                                      json={'fields': {k: encode(v) for k, v in fields.items()}}, timeout=10)
        if response.status_code != 200:
            raise RuntimeError('Database update not confirmed')

    def save(self, identity, result):
        if result['decision'] == 'bear':
            # Validate public payload BEFORE deleting its source mail.
            alert_payload(result['content'])
            self.create('mail_jobs', result['id'], {'content': result['content'],
                        'state': 'archived' if result['expired'] else 'queued'})
            job = self.get('mail_jobs', result['id'])
            if job is None or job[0]['content'] != result['content']:
                raise RuntimeError('Saved alert mismatch')
        self.create('mail_receipts', receipt_id(identity), {
            'identity': identity.__dict__, 'decision': result['decision'],
            'job': result.get('id', ''), 'savedAt': dt.datetime.now(dt.timezone.utc).isoformat()})
        return self.receipt(identity)

    def receipt(self, identity):
        row = self.get('mail_receipts', receipt_id(identity))
        if row is None or row[0]['identity'] != identity.__dict__:
            raise RuntimeError('Receipt not confirmed')
        return identity

    def cursor(self, mailbox, validity):
        key = hashlib.sha256((mailbox + '/' + validity).encode()).hexdigest()
        row = self.get('mail_cursors', key)
        return int(row[0]['uid']) if row else 0

    def set_cursor(self, mailbox, validity, uid):
        key = hashlib.sha256((mailbox + '/' + validity).encode()).hexdigest()
        self.update('mail_cursors', key, {'uid': str(uid)})

    def dispatch(self):
        response = self.session.post(BASE + ':runQuery', json={'structuredQuery': {
            'from': [{'collectionId': 'mail_jobs'}], 'where': {'fieldFilter': {
                'field': {'fieldPath': 'state'}, 'op': 'EQUAL', 'value': encode('queued')}},
            'limit': 10}}, timeout=10)
        if response.status_code != 200:
            raise RuntimeError('Queue query failed')
        counts = dict(sent=0, archived=0, needs_review=0)
        for item in response.json():
            if 'document' not in item:
                continue
            document = item['document']
            key = document['name'].rsplit('/', 1)[1]
            job = {k: decode(v) for k, v in document['fields'].items()}
            content = job['content']
            expires = dt.datetime.fromisoformat(content['expiresAt'])
            if expires <= dt.datetime.now(dt.timezone.utc):
                self.update('mail_jobs', key, {'state': 'archived'}, document['updateTime'])
                counts['archived'] += 1
                continue
            public = alert_payload(content)
            # Validate every notification BEFORE acquiring the one-shot claim.
            notifications = {}
            for locale in LOCALES:
                message = {
                    'condition': "'bear_all_" + locale + "' in topics || 'bear_" + content['region'] + '_' + locale + "' in topics",
                    'notification': {'title': public['titles'][locale], 'body': public['bodies'][locale]},
                    'data': {'alertId': key, 'region': content['region']},
                    'apns': {'headers': {'apns-expiration': str(int(expires.timestamp()))},
                             'payload': {'aps': {'sound': 'default'}}}}
                if len(json.dumps(message, ensure_ascii=False).encode()) > 4000:
                    raise ValueError('Payload too long')
                notifications[locale] = message
            # A sending job is NEVER automatically repeated after an uncertain send.
            self.update('mail_jobs', key, {'state': 'sending'}, document['updateTime'])
            sent = {}
            try:
                public.update(published=True)
                fields = {k: encode(v) for k, v in public.items()}
                fields['expiresAt'] = {'timestampValue': expires.isoformat()}
                fields['issuedAt'] = {'timestampValue': content['issuedAt']}
                reply = self.session.post(BASE + ':commit', json={'writes': [{
                    'update': {'name': BASE.split('/v1/')[1] + '/alerts/' + key, 'fields': fields},
                    'currentDocument': {'exists': False},
                    'updateTransforms': [{'fieldPath': 'createdAt', 'setToServerValue': 'REQUEST_TIME'}]}]}, timeout=10)
                if reply.status_code != 200:
                    raise RuntimeError('Publication not confirmed')
                for locale, message in notifications.items():
                    reply = self.session.post('https://fcm.googleapis.com/v1/projects/' + PROJECT + '/messages:send',
                                              json={'message': message}, timeout=10)
                    if reply.status_code != 200:
                        raise RuntimeError('Notification not confirmed')
                    sent[locale] = reply.json()['name']
                    self.update('mail_jobs', key, {'sent': sent})
                self.update('mail_jobs', key, {'state': 'sent'})
                counts['sent'] += 1
            except Exception:
                self.update('mail_jobs', key, {'state': 'needs_review', 'sent': sent})
                counts['needs_review'] += 1
        return counts

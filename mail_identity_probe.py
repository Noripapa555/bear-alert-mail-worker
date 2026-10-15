"""Cloud identity check: one private health record + FCM validate_only.

Never opens a mailbox, publishes an alert or delivers a notification.
"""
from mail_protocol import BASE, PROJECT, encode


def main():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    session = AuthorizedSession(credentials)
    document = BASE.split('/v1/')[1] + '/mail_health/identity_check'
    response = session.post(BASE + ':commit', json={'writes': [{
        'update': {'name': document, 'fields': {'status': encode('identity_test_only')}},
        'updateTransforms': [{'fieldPath': 'checkedAt', 'setToServerValue': 'REQUEST_TIME'}]}]}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError('Private health write failed, HTTP ' + str(response.status_code))
    response = session.get(BASE + '/mail_health/identity_check', timeout=20)
    if response.status_code != 200:
        raise RuntimeError('Private health read failed, HTTP ' + str(response.status_code))
    response = session.post('https://fcm.googleapis.com/v1/projects/' + PROJECT + '/messages:send',
                            json={'validate_only': True, 'message': {
                                'topic': 'bear_connectivity_test_only',
                                'notification': {'title': 'TEST ONLY', 'body': 'Validation only; never delivered.'}}}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError('FCM dry-run failed, HTTP ' + str(response.status_code))
    print('Cloud identity verified: private Firestore read/write and FCM validation. No notification delivered.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        safe = str(error)
        if safe.startswith(('Private health write failed, HTTP ', 'Private health read failed, HTTP ', 'FCM dry-run failed, HTTP ')):
            raise SystemExit(safe)
        raise SystemExit('Cloud identity check failed; credential details omitted.')

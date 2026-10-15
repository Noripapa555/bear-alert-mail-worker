"""Firestore wire values shared by the standalone mail worker."""
import os
import re

PROJECT = os.environ.get('BEAR_FIREBASE_PROJECT', 'bear-alert-japan')
if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', PROJECT):
    raise ValueError('Invalid Firebase project ID')
BASE = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
LOCALES = ('ja', 'ko', 'zh', 'fr', 'en', 'es')


def encode(value):
    if isinstance(value, bool):
        return {'booleanValue': value}
    if isinstance(value, dict):
        return {'mapValue': {'fields': {key: encode(item) for key, item in value.items()}}}
    return {'stringValue': str(value)}


def decode(value):
    for kind in ('stringValue', 'booleanValue', 'integerValue', 'timestampValue'):
        if kind in value:
            return value[kind]
    if 'mapValue' in value:
        return {key: decode(item) for key, item in value['mapValue'].get('fields', {}).items()}
    raise ValueError('Unsupported Firestore value')

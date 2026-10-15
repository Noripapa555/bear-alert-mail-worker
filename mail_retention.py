"""Mail retention primitives for the future municipality-mail worker.

No scheduler, credentials, classifier or live mailbox operation is provided here.
The caller must validate the source and persist the processed result durably.
"""
from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True)
class MailIdentity:
    mailbox: str
    uid_validity: str
    uid: str
    sha256: str

    def __post_init__(self):
        if not self.mailbox:
            raise ValueError('Mailbox identity required')
        for value in (self.uid_validity, self.uid):
            if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*', value):
                raise ValueError('Invalid UID identity')
        if not re.fullmatch(r'[0-9a-f]{64}', self.sha256):
            raise ValueError('Invalid message digest')


def process_then_remove(client, identity, raw_message, *, decision,
                        source_verified, persist_result):
    """Persist first, then UID-delete this message only.

    decision: bear / unnecessary / registration / unknown / failed.
    persist_result(identity, decision) must durably save the processed bear
    information (or minimal discard audit) and return the exact MailIdentity
    ONLY after commit. It must be idempotent by mailbox+UIDVALIDITY+UID+digest.
    For bear messages the callback must close over the extracted alert data;
    saving only a hash is not sufficient. A storage failure must raise.
    Caller serializes workers per mailbox and uses the same selected connection.
    """
    if decision not in {'bear', 'unnecessary', 'registration', 'unknown', 'failed'}:
        raise ValueError('Unknown decision')
    if not source_verified or decision in {'registration', 'unknown', 'failed'}:
        return 'retained'
    if hashlib.sha256(raw_message).hexdigest() != identity.sha256:
        raise ValueError('Message identity mismatch')
    # UIDPLUS permits targeted expunge. Never use mailbox-wide EXPUNGE/CLOSE,
    # which could destroy unrelated messages marked deleted by another client.
    capabilities = {c.decode('ascii').upper() if isinstance(c, bytes)
                    else c.upper() for c in client.capabilities}
    if 'UIDPLUS' not in capabilities:
        return 'retained_no_uidplus'
    _, validity = client.response('UIDVALIDITY')
    if validity != [identity.uid_validity.encode('ascii')]:
        raise ValueError('Mailbox UIDVALIDITY changed')
    receipt = persist_result(identity, decision)
    if receipt != identity:
        raise ValueError('Durable save not confirmed')
    status, _ = client.uid('STORE', identity.uid, '+FLAGS.SILENT', r'(\Deleted)')
    if status != 'OK':
        raise RuntimeError('Delete marking not confirmed; retry from durable receipt')
    status, _ = client.uid('EXPUNGE', identity.uid)
    if status != 'OK':
        raise RuntimeError('Removal not confirmed; retry from durable receipt')
    return 'removed'

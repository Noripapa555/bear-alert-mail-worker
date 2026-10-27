# Development restore

Use Python 3.9+ in a new virtual environment and install `requirements.lock` for
an exact snapshot of the complete resolved dependency set (2026-10-27):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -p 'test_*.py'
```

All 28 existing tests passed in a dedicated Mac virtual environment, with mocked
mail/Firestore/notification operations. No mailbox was contacted, no workflow
was dispatched and no runtime setting was enabled. The existing source tree and
public repository visibility are retained. This is environment preservation,
not app publication or worker activation; the owner decided not to publish Bear.

The lock is a resolved version snapshot, without package archive hashes. The
existing scheduled production workflows still use requirements.txt; adopting
the lock there requires its own review. No production configuration is changed
by this restoration PR. The test workflow now exercises the lock.

Credentials remain external. See README.md for item names and purposes; no
secret values, mail contents, credentials or new access are added here. Ignore
local .venv, .env, caches and signing material when saving development changes.

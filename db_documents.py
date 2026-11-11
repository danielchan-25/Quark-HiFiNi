"""Transactional JSON documents shared by the task runner and its panel."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import re


def apply_lyrics_result(documents, overrides_key, status_key, key, entry):
    """Discard results for a URL that was replaced or removed during verification."""
    current = documents[overrides_key].get('items', {}).get(key) or {}
    if not current.get('url') or current.get('url') != entry.get('url'):
        return False
    documents[status_key].setdefault('items', {})[key] = entry
    return True


class Documents:
    def __init__(self, connect, table):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', table):
            raise ValueError('Invalid document table')
        self.connect, self.table = connect, '`' + table + '`'

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()

    def get(self, key, default=None):
        db = self.connect()
        try:
            with db.cursor() as cursor:
                cursor.execute('SELECT content FROM ' + self.table + ' WHERE path=%s', (key,))
                row = cursor.fetchone()
            return json.loads(row[0]) if row else deepcopy(default)
        finally:
            db.close()

    def put(self, key, value):
        return self.change({key: value}, lambda documents: documents.__setitem__(key, value))

    def change(self, defaults, update):
        """Lock documents in a fixed order; publish all changes in one transaction.

        A missing document is inserted under the same transaction before locking.
        Callback exceptions roll back every affected document, including inserts.
        """
        db = self.connect()
        try:
            documents = {}
            original = {}
            with db.cursor() as cursor:
                for key in sorted(defaults):
                    data = self.encode(defaults[key])
                    # An upsert acquires an exclusive row lock immediately.
                    # INSERT IGNORE would acquire a shared duplicate-key lock,
                    # whose concurrent upgrade to FOR UPDATE can deadlock.
                    cursor.execute('INSERT INTO ' + self.table +
                                   ' (path,content,sha256) VALUES(%s,%s,%s) '
                                   'ON DUPLICATE KEY UPDATE path=%s',
                                   (key, data, hashlib.sha256(data).hexdigest(), key))
                    cursor.execute('SELECT content FROM ' + self.table + ' WHERE path=%s FOR UPDATE', (key,))
                    original[key] = bytes(cursor.fetchone()[0])
                    documents[key] = json.loads(original[key])
                result = update(documents)
                for key, value in documents.items():
                    data = self.encode(value)
                    if data == original[key]:
                        continue
                    cursor.execute('UPDATE ' + self.table +
                                   ' SET content=%s,sha256=%s,updated_at=CURRENT_TIMESTAMP(6) WHERE path=%s',
                                   (data, hashlib.sha256(data).hexdigest(), key))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

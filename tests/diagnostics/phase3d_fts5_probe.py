"""Bounded FTS5 experiments; creates only temporary databases, no runtime access.

Run with the runtime venv Python. These are hypotheses, not CI assertions that
concurrency must corrupt SQLite. Explicit embeddings prevent model downloads.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
import tempfile
import time
from pathlib import Path


def client(path):
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))


def write(path, mode, ready, count=80):
    c = client(path).get_collection('probe')
    ready.set()
    for n in range(count):
        ids = [str(i) for i in range(64)]
        if mode == 'delete':
            c.delete(ids=ids[::2])
        c.upsert(ids=ids, embeddings=[[float(i % 3), 1., 0.] for i in range(64)],
                 documents=[('test alpha ' if n % 2 else 'test omega ') * (20 + n % 13) + str(i) for i in range(64)])


def checkpoint(path, ready):
    with sqlite3.connect(str(Path(path) / 'chroma.sqlite3'), timeout=5) as c:
        ready.set()
        while True:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()


def inspect(path, immutable=False):
    uri = (Path(path) / 'chroma.sqlite3').as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
    with sqlite3.connect(uri, uri=True) as c:
        quick = [r[0] for r in c.execute('PRAGMA quick_check')]
        match = c.execute("SELECT count(*) FROM embedding_fulltext_search WHERE embedding_fulltext_search MATCH 'test'").fetchone()[0]
    return {'quick_check': quick, 'match': match}


def run(mode):
    ctx = mp.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='phase3d-fts5-') as path:
        c = client(path).create_collection('probe', embedding_function=None)
        c.upsert(ids=[str(i) for i in range(64)], embeddings=[[1., 0., 0.]] * 64,
                 documents=['test initial ' * 25] * 64)
        db = Path(path) / 'chroma.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        ready = ctx.Event()
        writer = ctx.Process(target=write, args=(path, 'upsert', ready))
        writer.start()
        if not ready.wait(30):
            writer.kill(); writer.join(); raise RuntimeError('writer startup timeout')
        extra = None
        errors = []; samples = 0; killed = False
        if mode == 'parallel_delete':
            other_ready = ctx.Event()
            extra = ctx.Process(target=write, args=(path, 'delete', other_ready))
            extra.start()
        elif mode == 'checkpoint_kill':
            r = ctx.Event(); extra = ctx.Process(target=checkpoint, args=(path, r)); extra.start()
            r.wait(10)
            # A signalled checkpoint loop, not proof of interruption inside fsync.
            time.sleep(.02); extra.kill(); extra.join(); killed = True
        deadline = time.monotonic() + 45
        while writer.is_alive() and time.monotonic() < deadline:
            try:
                sample = inspect(path, immutable=mode == 'immutable_read')
                for detail in sample['quick_check']:
                    if detail != 'ok' and detail not in errors:
                        errors.append(detail)
            except sqlite3.DatabaseError as exc:
                if str(exc) not in errors:
                    errors.append(str(exc))
            samples += 1
        writer.join(5)
        if writer.is_alive():
            writer.kill(); writer.join(); errors.append('writer timeout')
        if extra and not killed:
            extra.join(20)
            if extra.is_alive(): extra.kill(); extra.join(); errors.append('second writer timeout')
        try:
            final = inspect(path)
        except sqlite3.DatabaseError as exc:
            final = {'error': str(exc)}
        return {'hypothesis': mode, 'samples': samples, 'errors': errors,
                'writer_exit': writer.exitcode, 'other_exit': extra.exitcode if extra else None,
                'final_readonly': final}


if __name__ == '__main__':
    for mode in ('parallel_delete', 'immutable_read', 'checkpoint_kill'):
        print(json.dumps(run(mode)), flush=True)

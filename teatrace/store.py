"""Transactional evidence import, materialized ledgers and replay audit."""

import hashlib
import json
import sqlite3
from pathlib import Path
from .logic import calculate

SCHEMA='''
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS certificates(id TEXT PRIMARY KEY, garden TEXT NOT NULL, variety TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, organic INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pickers(id TEXT PRIMARY KEY, garden TEXT NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, garden TEXT NOT NULL, picker TEXT NOT NULL, ticket TEXT NOT NULL, picked_at TEXT NOT NULL, gross_kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS weights(id TEXT PRIMARY KEY, receipt TEXT NOT NULL, net_kg REAL NOT NULL, garden_sign TEXT NOT NULL, factory_sign TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blends(id TEXT PRIMARY KEY, receipts TEXT NOT NULL, output_kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reworks(id TEXT PRIMARY KEY, blend TEXT NOT NULL, receipts TEXT NOT NULL, output_kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS labels(id TEXT PRIMARY KEY, blend TEXT NOT NULL, organic INTEGER NOT NULL, grade TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, result TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id TEXT NOT NULL, revision INTEGER NOT NULL, result TEXT NOT NULL, PRIMARY KEY(id,revision));
'''
KIND_SUBSYSTEM={'certificate':'garden','picker':'garden','receipt':'leaf','weight':'leaf','blend':'blend','rework':'blend','label':'blend'}
TABLES=('certificates','pickers','receipts','weights','blends','reworks','labels')


def connect(path):
    if str(path)!=':memory:':Path(path).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.executescript(SCHEMA)
    c.execute('PRAGMA journal_mode=WAL')
    return c


def canonical(item):return json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def events(c):return [json.loads(r['body']) for r in c.execute('SELECT body FROM evidence ORDER BY at,id')]


def materialize(c, source):
    for table in TABLES:c.execute(f'DELETE FROM {table}')
    for e in source:
        k=e['kind']
        if k=='certificate':c.execute('INSERT INTO certificates VALUES (?,?,?,?,?,?)',(e['id'],e['garden'],e['variety'],e['version'],e['status'],int(e['organic'])))
        elif k=='picker':c.execute('INSERT INTO pickers VALUES (?,?,?,?,?)',(e['id'],e['garden'],e['start'],e['finish'],e['status']))
        elif k=='receipt':c.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)',(e['id'],e['garden'],e['picker'],e['pick_ticket'],e['picked_at'],e['gross_kg']))
        elif k=='weight':c.execute('INSERT INTO weights VALUES (?,?,?,?,?)',(e['id'],e['receipt'],e['net_kg'],e['garden_sign'],e['factory_sign']))
        elif k=='blend':c.execute('INSERT INTO blends VALUES (?,?,?)',(e['id'],canonical(e['receipts']),e['output_kg']))
        elif k=='rework':c.execute('INSERT INTO reworks VALUES (?,?,?,?)',(e['id'],e['blend'],canonical(e['receipts']),e['output_kg']))
        elif k=='label':c.execute('INSERT INTO labels VALUES (?,?,?,?)',(e['id'],e['blend'],int(e['organic']),e['grade']))


def rebuild(c):
    source=events(c);materialize(c,source)
    current=calculate(source)
    for key,result in current.items():
        body=canonical(result)
        old=c.execute('SELECT result,revision FROM decisions WHERE id=?',(key,)).fetchone()
        if old and old['result']==body:continue
        version=1 if old is None else old['revision']+1
        c.execute('INSERT INTO decision_history VALUES (?,?,?)',(key,version,body))
        c.execute('INSERT INTO decisions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result,revision=excluded.revision',(key,body,version))


def ingest(c, subsystem, payload):
    if not isinstance(payload,list) or not payload:raise ValueError('nonempty event array required')
    inserted=replayed=0
    with c:
        for e in payload:
            if not isinstance(e,dict) or KIND_SUBSYSTEM.get(e.get('kind'))!=subsystem or not e.get('id') or not e.get('at'):
                raise ValueError('event identity or subsystem mismatch')
            body=canonical(e);sha=hashlib.sha256(body.encode()).hexdigest()
            old=c.execute('SELECT sha256 FROM evidence WHERE id=?',(e['id'],)).fetchone()
            if old:
                if old['sha256']!=sha:raise ValueError('event ID content collision')
                replayed+=1;continue
            c.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',(e['id'],subsystem,e['at'],body,sha));inserted+=1
        rebuild(c)
    return {'inserted':inserted,'replayed':replayed}


def report(c):
    return {'evidence_count':c.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
        'decisions':{r['id']:json.loads(r['result']) for r in c.execute('SELECT * FROM decisions ORDER BY id')},
        'history':[dict(r) for r in c.execute('SELECT * FROM decision_history ORDER BY id,revision')]}


def audit(c):
    source=events(c)
    expected=calculate(source);actual=report(c)['decisions']
    changed=sorted(k for k in set(expected)|set(actual) if expected.get(k)!=actual.get(k))
    replay=connect(':memory:')
    try:
        materialize(replay,source)
        for table in TABLES:
            actual_rows=[tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY id')]
            replay_rows=[tuple(row) for row in replay.execute(f'SELECT * FROM {table} ORDER BY id')]
            if actual_rows!=replay_rows:changed.append(f'table:{table}')
    finally:replay.close()
    return {'ok':not changed,'changed':changed,'evidence_count':len(source)}

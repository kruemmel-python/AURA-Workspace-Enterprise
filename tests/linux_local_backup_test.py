#!/usr/bin/python3
import importlib.util, os, sqlite3, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-backup-') as tmp:
    path=Path(tmp)/'workspace.db'; backup=Path(tmp)/'Sicherung ä.sqlite'
    store=m.LocalStore(path);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
    store.save_room(0,'Sicherungsraum','Berlin',5,'');store.backup(backup)
    assert backup.stat().st_mode & 0o077 == 0
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT name FROM rooms').fetchone()[0]=='Sicherungsraum'
    original=backup.read_bytes()
    store.save_room(0,'Späterer Raum','Bonn',3,'')
    for target in (backup,path,Path(tmp)/'missing'/'x.sqlite'):
        try:store.backup(target);raise AssertionError('invalid backup destination accepted')
        except m.LocalStoreError:pass
    assert backup.read_bytes()==original
    with sqlite3.connect(backup) as db:assert db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]==1
    audit=store.audit();assert any('database.backup.created' in entry and str(backup) in entry for entry in audit)
    assert not list(Path(tmp).glob('*.init-*'))
    with sqlite3.connect(path) as db:db.execute('DELETE FROM employee_roles')
    try:store.backup(Path(tmp)/'unauthorized.sqlite');raise AssertionError('permission bypass')
    except m.LocalStoreError:pass
    store.close()
print('SQLite backup integrity, snapshot isolation, no overwrite, audit and permission: OK')

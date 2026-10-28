#!/usr/bin/python3
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-local-test-') as tmp:
    path=Path(tmp)/'workspace.sqlite'; store=m.LocalStore(path)
    store.create('Admin Ä','admin','Start-Passwort-2026')
    assert os.stat(path).st_mode & 0o077 == 0
    before=path.read_bytes()
    try: store.create('Other','other','Other-Passwort-2026'); raise AssertionError('overwrite accepted')
    except m.LocalStoreError: pass
    assert path.read_bytes()==before
    store.open()
    try: store.rooms(); raise AssertionError('unauthenticated read accepted')
    except m.LocalStoreError: pass
    try: store.login('admin','Falsches-Passwort'); raise AssertionError('wrong password accepted')
    except m.LocalStoreError: pass
    assert not store.login('admin','Start-Passwort-2026')
    store.save_room(0,'Besprechung Süd','Berlin',8,'Display')
    room=store.rooms()[0]; assert room['name']=='Besprechung Süd'
    store.save_room(room['id'],'Besprechung Nord','Bonn',12,'Beamer')
    assert store.rooms()[0]['location']=='Bonn'
    store.close(); store.open(); assert not store.login('admin','Start-Passwort-2026')
    assert store.rooms()[0]['name']=='Besprechung Nord'
    store.archive_room(room['id']); assert store.rooms()==[]
    store.change_password('Start-Passwort-2026','Anderes-Passwort-2026')
    store.close(); store.open()
    try: store.login('admin','Start-Passwort-2026'); raise AssertionError('old password accepted')
    except m.LocalStoreError: pass
    assert not store.login('admin','Anderes-Passwort-2026')
    store.close()
    with sqlite3.connect(path) as db: db.execute('UPDATE accounts SET must_change_password=1')
    store.open(); assert store.login('admin','Anderes-Passwort-2026')
    try: store.rooms(); raise AssertionError('mandatory password change bypass')
    except m.LocalStoreError: pass
    try: store.change_password('Wrong-Passwort-2026','Drittes-Passwort-2026'); raise AssertionError('incorrect password accepted')
    except m.LocalStoreError: pass
    store.change_password('Anderes-Passwort-2026','Drittes-Passwort-2026')
    assert store.rooms()==[]
    # Revoking a role is enforced even after authentication.
    with sqlite3.connect(path) as db: db.execute('DELETE FROM employee_roles')
    try: store.save_room(0,'Forbidden','Berlin',3,''); raise AssertionError('permission bypass')
    except m.LocalStoreError: pass
    with sqlite3.connect(path) as db: db.execute('UPDATE accounts SET enabled=0')
    try: store.rooms(); raise AssertionError('disabled session accepted')
    except m.LocalStoreError: pass
    store.close()
    missing=m.LocalStore(Path(tmp)/'absent.sqlite')
    try: missing.open(); raise AssertionError('missing database created')
    except m.LocalStoreError: pass
    assert not missing.path.exists()
print('Local login, permissions, persistent room CRUD, password change and safe creation: OK')

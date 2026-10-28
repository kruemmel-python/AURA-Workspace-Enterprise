#!/usr/bin/python3
import csv,importlib.util,os,sqlite3,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-csv-') as tmp:
    path=Path(tmp)/'workspace.sqlite';store=m.LocalStore(path);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
    store.save_room(0,'Raum Ä','Berlin',5,'Monitor, "groß"\nund mehr');export=Path(tmp)/'Räume.csv';store.export_rooms(export)
    assert export.stat().st_mode & 0o077 == 0
    with export.open(encoding='utf-8',newline='') as f: rows=list(csv.reader(f))
    assert rows[1][1]=='Raum Ä' and rows[1][4]=='Monitor, "groß"\nund mehr'
    original=export.read_bytes()
    try:store.export_rooms(export);raise AssertionError('overwrote CSV')
    except m.LocalStoreError:pass
    assert export.read_bytes()==original
    store.save_room(store.rooms()[0]['id'],'Raum Ä','Bonn',3,'Alt');assert store.import_rooms(export)==1
    assert store.rooms()[0]['location']=='Berlin' and store.rooms()[0]['equipment']=='Monitor, "groß"\nund mehr'
    bad=Path(tmp)/'invalid.csv';bad.write_text('id,name,location,capacity,equipment,active\n0,Erster,Berlin,5,X,1\n0,Zweiter,Berlin,5x,X,1\n',encoding='utf-8')
    try:store.import_rooms(bad);raise AssertionError('invalid CSV accepted')
    except m.LocalStoreError:pass
    assert len(store.rooms())==1
    bad.write_text('id,name,location,capacity,equipment,active\n0,"Nicht geschlossen,Berlin,5,X,1',encoding='utf-8')
    try:store.import_rooms(bad);raise AssertionError('unclosed quote accepted')
    except m.LocalStoreError:pass
    assert len(store.rooms())==1
    with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_log WHEN NEW.action='rooms.imported.csv' BEGIN SELECT RAISE(FAIL,'audit denied'); END")
    try:store.import_rooms(export);raise AssertionError('import without audit accepted')
    except m.LocalStoreError:pass
    with sqlite3.connect(path) as db:db.execute('DROP TRIGGER fail_audit');db.execute('DELETE FROM employee_roles')
    try:store.import_rooms(export);raise AssertionError('permission bypass')
    except m.LocalStoreError:pass
    assert not list(Path(tmp).glob('*.init-*'));store.close()
print('Room CSV round trip, Unicode, multiline, atomic rollback, no overwrite and permissions: OK')

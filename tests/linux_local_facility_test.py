#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-facility-') as tmp:
    path=Path(tmp)/'workspace.sqlite';admin=mod.LocalStore(path);admin.create('Admin','admin','Facility Passwort 2026');admin.open();admin.login('admin','Facility Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    reject(lambda:admin.save_asset(0,'','Gerät','Berlin','operational'))
    admin.save_asset(0,'Beamer','Präsentation','Berlin','operational');asset=admin.assets()[0]
    admin.save_asset(asset['id'],'Beamer Süd','Präsentation','Bonn','maintenance');assert admin.assets()[0]['name']=='Beamer Süd'
    reject(lambda:admin.save_maintenance(999,0,'Unbekannt','open',0,0))
    admin.save_maintenance(asset['id'],0,'Filter prüfen','open',1900000000,30);order=admin.maintenance(asset['id'])[0]
    reject(lambda:admin.save_maintenance(999,order['id'],'Fremd','open',0,0))
    admin.save_maintenance(asset['id'],order['id'],'Filter und Lampe','in_progress',1900000100,30)
    admin.maintenance_status(asset['id'],order['id'],'completed')
    entries=admin.maintenance(asset['id']);repeat=next(x for x in entries if x['id']!=order['id'])
    assert repeat['status']=='open' and repeat['dueAt']==1900000100+30*86400
    admin.archive_maintenance(asset['id'],order['id']);assert len(admin.maintenance(asset['id']))==1
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Leser','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Leser'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'reader',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    reader=mod.LocalStore(path);reader.open();reader.login('reader','Facility Passwort 2026')
    reject(lambda:reader.assets());reject(lambda:reader.save_asset(0,'X','Y','Z','operational'));reject(lambda:reader.maintenance_status(asset['id'],repeat['id'],'completed'))
    reader.close();admin.close();admin.open();admin.login('admin','Facility Passwort 2026')
    assert admin.assets()[0]['location']=='Bonn' and admin.maintenance(asset['id'])[0]['id']==repeat['id']
    admin.archive_asset(asset['id']);assert admin.assets()==[]
    reject(lambda:admin.save_maintenance(asset['id'],0,'Archiviert','open',0,0));admin.close()
print('Local facility assets, maintenance recurrence, authorization and persistence: OK')

#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-team-') as tmp:
    path=Path(tmp)/'workspace.db';admin=mod.LocalStore(path);admin.create('Admin','admin','Team Passwort 2026');admin.open();admin.login('admin','Team Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    admin.save_employee(0,'Sam','Techniker','IT');employee=next(x for x in admin.employees() if x['name']=='Sam')
    admin.save_employee(employee['id'],'Sam Weiß','Ingenieur','IT');admin.employee_presence(employee['id'],'remote')
    assert next(x for x in admin.employees() if x['id']==employee['id'])['presence']=='remote'
    reject(lambda:admin.archive_employee(1))
    admin.create_leave(1900000000,1900100000,'Eigener Urlaub');own=admin.leave()[0]
    reject(lambda:admin.decide_leave(own['id'],True))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'employee',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(employee['id'],))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(employee['id'],))
    reader=mod.LocalStore(path);reader.open();reader.login('employee','Team Passwort 2026')
    assert reader.leave()==[]
    reader.create_leave(1900200000,1900300000,'Urlaub Sam');leave=reader.leave()[0]
    reject(lambda:reader.decide_leave(leave['id'],True));reject(lambda:reader.save_employee(0,'Unbefugt','Rolle','IT'))
    reader.employee_presence(employee['id'],'meeting');reject(lambda:reader.employee_presence(1,'away'))
    admin.decide_leave(leave['id'],True);reject(lambda:admin.decide_leave(leave['id'],False))
    assert reader.leave()[0]['status']=='approved' and len(admin.leave())==2
    reader.close();admin.close();admin.open();admin.login('admin','Team Passwort 2026')
    assert next(x for x in admin.employees() if x['id']==employee['id'])['presence']=='meeting'
    assert next(x for x in admin.leave() if x['id']==leave['id'])['status']=='approved'
    admin.archive_employee(employee['id']);assert len(admin.employees())==1;admin.close()
print('Local team: CRUD, presence permissions, leave privacy, approval and persistence: OK')

#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-accounts-') as tmp:
    path=Path(tmp)/'workspace.db';admin=mod.LocalStore(path);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    admin.save_employee(0,'Robin','Technik','IT');user=next(e for e in admin.employees() if e['name']=='Robin')
    assert {'admin','employee'} <= {r['code'] for r in admin.roles()}
    admin.assign_role(user['id'],'employee');assert next(e for e in admin.employees() if e['id']==user['id'])['systemRole']=='employee'
    reject(lambda:admin.assign_role(1,'employee'));reject(lambda:admin.enable_account(1,False));reject(lambda:admin.reset_account(1,'Anderes Passwort 2026'))
    admin.create_account(user['id'],'robin','Temporär Passwort 2026')
    reject(lambda:admin.create_account(user['id'],'robin2','Temporär Passwort 2026'))
    assert next(a for a in admin.accounts() if a['employeeId']==user['id'])['mustChangePassword']
    employee=mod.LocalStore(path);employee.open();assert employee.login('robin','Temporär Passwort 2026')
    reject(lambda:employee.rooms());reject(lambda:employee.change_password('Falsch Passwort','Eigenes Passwort 2026'))
    employee.change_password('Temporär Passwort 2026','Eigenes Passwort 2026');assert employee.rooms()==[]
    reject(lambda:employee.assign_role(user['id'],'admin'));reject(lambda:employee.accounts());reject(lambda:employee.reset_account(1,'Unbefugt Passwort'))
    admin.enable_account(user['id'],False);reject(lambda:employee.rooms());reject(lambda:admin.reset_account(user['id'],'Anderes Passwort 2026'));employee.close();employee.open();reject(lambda:employee.login('robin','Eigenes Passwort 2026'))
    admin.enable_account(user['id'],True);admin.reset_account(user['id'],'Neues Temporär 2026')
    assert employee.login('robin','Neues Temporär 2026');employee.change_password('Neues Temporär 2026','Neues Eigenes 2026')
    employee.close();employee.open()
    for _ in range(5): reject(lambda:employee.login('robin','Falsches Passwort'))
    reject(lambda:employee.login('robin','Neues Eigenes 2026'))
    assert next(a for a in admin.accounts() if a['employeeId']==user['id'])['lockedUntil']>0
    admin.reset_account(user['id'],'Entsperrt Passwort 2026');assert employee.login('robin','Entsperrt Passwort 2026')
    employee.change_password('Entsperrt Passwort 2026','Finales Passwort 2026')
    admin.close();employee.close()
    admin.open();admin.login('admin','Admin Passwort 2026');assert len(admin.accounts())==2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='account.credentials.reset'").fetchone()[0]>=3
    admin.close()
print('Local accounts: roles, temporary passwords, permissions, deactivation, reset and persistence: OK')

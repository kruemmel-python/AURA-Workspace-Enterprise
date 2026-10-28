#!/usr/bin/python3
import importlib.util
from pathlib import Path
import tempfile
import sqlite3
import threading
import time
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-org-approval-') as tmp:
    path=Path(tmp)/'workspace.db';admin=mod.LocalStore(path);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    admin.save_employee(0,'Robin','Projektleitung','IT');user=next(e for e in admin.employees() if e['name']=='Robin')
    admin.assign_role(user['id'],'project_manager');admin.create_account(user['id'],'robin','Temporär Passwort 2026')
    approver=mod.LocalStore(path);approver.open();assert approver.login('robin','Temporär Passwort 2026');approver.change_password('Temporär Passwort 2026','Eigenes Passwort 2026')
    admin.save_organization(0,'Entwicklung','DEV',user['id']);org=admin.organizations()[0]
    reject(lambda:admin.save_organization(org['id'],'Entwicklung','DEV',9999))
    assert admin.organizations()[0]['managerId']==user['id']
    admin.save_organization(org['id'],'Software','SW',1);assert admin.organizations()[0]['name']=='Software'
    reject(lambda:approver.organizations());reject(lambda:approver.archive_organization(org['id']))
    # Starting a second session while another connection writes should wait,
    # then succeed rather than failing the schema initialization.
    lock=sqlite3.connect(path);lock.execute('BEGIN IMMEDIATE')
    contender=mod.LocalStore(path);failures=[]
    def open_while_locked():
        try:contender.open()
        except Exception as exc:failures.append(exc)
    thread=threading.Thread(target=open_while_locked);thread.start();time.sleep(.25)
    lock.commit();lock.close();thread.join(timeout=7)
    assert not thread.is_alive() and not failures,failures
    contender.close()
    admin.save_task(0,'Release','IT','Hoch',1);task=admin.tasks()[0]
    reject(lambda:admin.create_approval(task['id'],1));reject(lambda:admin.create_approval(task['id'],9999))
    admin.create_approval(task['id'],user['id']);assert admin.approvals()==[]
    approvals=approver.approvals();assert len(approvals)==1 and approvals[0]['taskTitle']=='Release' and approvals[0]['requesterName']=='Admin'
    reject(lambda:admin.decide_approval(approvals[0]['id'],True))
    approver.decide_approval(approvals[0]['id'],True);reject(lambda:approver.decide_approval(approvals[0]['id'],False))
    assert approver.approvals()[0]['status']=='approved'
    admin.close();approver.close();admin.open();admin.login('admin','Admin Passwort 2026')
    assert admin.organizations()[0]['name']=='Software';admin.archive_organization(org['id']);assert admin.organizations()==[];admin.close()
    approver.open();approver.login('robin','Eigenes Passwort 2026');assert approver.approvals()[0]['status']=='approved';approver.close()
print('Local organization, personal approvals, access control and persistence: OK')

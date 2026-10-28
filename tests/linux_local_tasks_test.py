#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-task-test-') as tmp:
    path=Path(tmp)/'workspace.sqlite';admin=m.LocalStore(path)
    admin.create('Admin','admin','Passwort-Aufgaben-2026');admin.open();admin.login('admin','Passwort-Aufgaben-2026')
    def reject(f):
        try:f()
        except m.LocalStoreError:return
        raise AssertionError('unexpected success')
    reject(lambda:admin.save_task(0,'','IT','Hoch',1))
    reject(lambda:admin.save_task(0,'Ungültig','IT','Dringend',1))
    assert not admin.tasks()
    admin.save_project(0,'Standort','IT','Plan','planned',1)
    project=admin.projects()[0];admin.save_project(project['id'],'Standort 2','IT','Neu','active',1)
    assert admin.projects()[0]['name']=='Standort 2'
    admin.save_task(0,'Wartung','IT','Hoch',1,1900000000,7)
    first=admin.tasks()[0];admin.assign_project(first['id'],project['id'])
    admin.save_task(first['id'],'Wartung Nord','IT','Mittel',1,1900000000,7)
    admin.task_status(first['id'],'done')
    tasks=admin.tasks(); assert len(tasks)==2,tasks
    completed=next(t for t in tasks if t['id']==first['id']); recurring=next(t for t in tasks if t['id']!=first['id'])
    assert completed['status']=='done' and recurring['status']=='open' and recurring['dueAt']==1900000000+7*86400
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(recurring['id'],)).fetchone()[0]==project['id']
    reject(lambda:admin.task_status(first['id'],'done'))
    admin.task_status(first['id'],'open');admin.close();admin.open();admin.login('admin','Passwort-Aufgaben-2026');admin.task_status(first['id'],'done')
    assert len(admin.tasks())==2 and next(t for t in admin.tasks() if t['id']==recurring['id'])['status']=='open'
    reject(lambda:admin.assign_project(recurring['id'],9999))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(recurring['id'],)).fetchone()[0]==project['id']
        db.execute('INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at) VALUES(?,?,\'open\',1,0)',(recurring['id'],'Teilaufgabe'))
    reject(lambda:admin.task_status(recurring['id'],'done'))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Mitarbeiter','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Mitarbeiter'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'employee',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    employee=m.LocalStore(path);employee.open();employee.login('employee','Passwort-Aufgaben-2026')
    reject(lambda:employee.save_task(0,'Unbefugt','IT','Hoch',uid))
    reject(lambda:employee.task_status(recurring['id'],'cancelled'))
    reject(lambda:employee.archive_project(project['id']))
    employee.close();admin.archive_task(first['id']);admin.archive_project(project['id']);admin.close()
    admin.open();admin.login('admin','Passwort-Aufgaben-2026')
    assert len(admin.projects())==0 and len(admin.tasks())==1
    admin.close()
print('Local tasks/projects: permissions, scheduling, recurrence, dependencies, assignment rollback and persistence: OK')
with tempfile.TemporaryDirectory(prefix='aura-subtask-stale-') as tmp:
    path=Path(tmp)/'workspace.sqlite';admin=m.LocalStore(path)
    admin.create('Admin','admin','Passwort-Aufgaben-2026');admin.open();admin.login('admin','Passwort-Aufgaben-2026')
    admin.save_employee(0,'Inaktiv','IT','IT');person=next(e['id'] for e in admin.employees() if e['name']=='Inaktiv')
    admin.save_task(0,'Aktive Aufgabe','IT','Mittel',1);task=admin.tasks()[0]['id']
    admin.save_subtask(task,0,'Archiviert','open',1,0);original=admin.subtasks(task)[0]
    admin.archive_subtask(task,original['id'])
    try:admin.save_subtask_compared(task,original['id'],'Unzulässig','done',1,0,original);raise AssertionError('archived subtask restored')
    except m.LocalStoreError:pass
    with sqlite3.connect(path) as db:assert db.execute('SELECT title,active FROM task_subtasks WHERE id=?',(original['id'],)).fetchone()==('Archiviert',0)
    admin.save_subtask(task,0,'Aktiv','open',1,0);active=admin.subtasks(task)[0]
    admin.save_subtask(task,active['id'],'Zwischenzeitlich geändert','open',1,0)
    try:admin.archive_subtask_compared(task,active['id'],active);raise AssertionError('stale subtask archived')
    except m.LocalStoreError:pass
    active=admin.subtasks(task)[0]
    assert active['title']=='Zwischenzeitlich geändert'
    admin.archive_employee(person)
    try:admin.save_subtask_compared(task,active['id'],'Unzulässig','done',person,0,active);raise AssertionError('inactive employee assigned')
    except m.LocalStoreError:pass
    assert admin.subtasks(task)[0]['title']=='Zwischenzeitlich geändert' and admin.subtasks(task)[0]['assigneeId']==1
    admin.archive_task(task)
    try:admin.save_subtask_compared(task,active['id'],'Unzulässig','done',1,0,active);raise AssertionError('archived parent accepted')
    except m.LocalStoreError:pass
    with sqlite3.connect(path) as db:assert db.execute('SELECT title,status FROM task_subtasks WHERE id=?',(active['id'],)).fetchone()==('Zwischenzeitlich geändert','open')
    admin.close()
print('Local compared subtasks: archived child, employee and parent remain protected: OK')

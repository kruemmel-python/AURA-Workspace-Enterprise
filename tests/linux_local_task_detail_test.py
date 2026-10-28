#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-task-detail-') as tmp:
    path=Path(tmp)/'workspace.db'; store=m.LocalStore(path)
    store.create('Admin','admin','Passwort-Aufgaben-2026');store.open();store.login('admin','Passwort-Aufgaben-2026')
    def reject(f):
        try:f()
        except m.LocalStoreError:return
        raise AssertionError('unexpected success')
    store.save_task(0,'Hauptaufgabe','IT','Hoch',1,1900000000,7)
    store.save_task(0,'Voraussetzung','IT','Mittel',1)
    tasks={t['title']:t for t in store.tasks()};main=tasks['Hauptaufgabe']['id'];pre=tasks['Voraussetzung']['id']
    store.dependency_change(main,pre,True)
    assert store.dependencies(main)==[{'id':pre,'title':'Voraussetzung'}]
    reject(lambda:store.dependency_change(pre,main,True))
    reject(lambda:store.dependency_change(main,main,True))
    reject(lambda:store.save_subtask(main,0,'Ohne Mitarbeiter','open',9999,0))
    store.save_subtask(main,0,'Teilaufgabe','open',1,1900000200)
    sub=store.subtasks(main)[0]
    reject(lambda:store.save_subtask(pre,sub['id'],'Fremd','done',1,0))
    reject(lambda:store.archive_subtask(pre,sub['id']))
    reject(lambda:store.task_status(main,'done'))
    store.task_status(pre,'done')
    reject(lambda:store.task_status(main,'done'))
    store.save_subtask(main,sub['id'],'Teilaufgabe fertig','done',1,1900000200)
    store.task_status(main,'done')
    repeat=next(t for t in store.tasks() if t['id'] not in (main,pre))
    copied=store.subtasks(repeat['id']);assert len(copied)==1 and copied[0]['status']=='open' and copied[0]['dueAt']==1900000200+7*86400
    store.dependency_change(main,pre,False);assert store.dependencies(main)==[]
    store.archive_subtask(main,sub['id']);assert store.subtasks(main)==[]
    store.close();store.open();store.login('admin','Passwort-Aufgaben-2026')
    assert len(store.subtasks(repeat['id']))==1 and store.dependencies(main)==[]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Leser','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Leser'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'reader',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    reader=m.LocalStore(path);reader.open();reader.login('reader','Passwort-Aufgaben-2026')
    reject(lambda:reader.dependency_change(repeat['id'],pre,True))
    reject(lambda:reader.save_subtask(repeat['id'],0,'Unbefugt','open',uid,0))
    reader.close();store.close()
print('Local task details: cycles, dependencies, subtasks, completion, recurrence and permissions: OK')

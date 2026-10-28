#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-helpdesk-') as tmp:
    path=Path(tmp)/'workspace.db';admin=m.LocalStore(path)
    admin.create('Admin Ä','admin','Helpdesk Passwort 2026');admin.open();admin.login('admin','Helpdesk Passwort 2026')
    def reject(action):
        try:action()
        except m.LocalStoreError:return
        raise AssertionError('unexpected success')
    reject(lambda:admin.save_ticket(0,'Fehler','IT','Hoch',99999))
    reject(lambda:admin.save_ticket(0,'Fehler','IT','Dringend',1))
    assert not admin.tickets()
    admin.save_ticket(0,'Monitor','IT','Hoch',1);ticket=admin.tickets()[0]
    admin.save_ticket(ticket['id'],'Monitor defekt','IT','Mittel',1)
    reject(lambda:admin.ticket_status(ticket['id'],'resolved'))
    admin.ticket_status(ticket['id'],'in_progress');admin.ticket_status(ticket['id'],'resolved');admin.ticket_status(ticket['id'],'closed');admin.ticket_status(ticket['id'],'open')
    reject(lambda:admin.add_comment('ticket',ticket['id'],''))
    admin.add_comment('ticket',ticket['id'],'Prüfung ä\nZeile 2')
    comments=admin.comments('ticket',ticket['id']);assert len(comments)==1 and comments[0]['body']=='Prüfung ä\nZeile 2' and comments[0]['author']=='Admin Ä'
    admin.save_task(0,'Aufgabe','IT','Hoch',1);task=admin.tasks()[0]
    admin.add_comment('task',task['id'],'Aufgabenverlauf')
    assert admin.comments('task',task['id'])[0]['body']=='Aufgabenverlauf'
    assert len(admin.comments('ticket',ticket['id']))==1
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Leser','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Leser'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'reader',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    reader=m.LocalStore(path);reader.open();reader.login('reader','Helpdesk Passwort 2026')
    reject(lambda:reader.save_ticket(0,'Unbefugt','IT','Hoch',uid))
    reject(lambda:reader.ticket_status(ticket['id'],'closed'))
    reject(lambda:reader.comments('ticket',ticket['id']))
    reject(lambda:reader.add_comment('task',task['id'],'Unbefugt'))
    reader.close();admin.close();admin.open();admin.login('admin','Helpdesk Passwort 2026')
    assert admin.tickets()[0]['title']=='Monitor defekt' and admin.comments('ticket',ticket['id'])[0]['body']=='Prüfung ä\nZeile 2'
    admin.archive_ticket(ticket['id']);assert admin.tickets()==[]
    reject(lambda:admin.add_comment('ticket',ticket['id'],'Archiviert'))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE action='ticket.comment.created'").fetchone()[0]==1
    admin.close()
print('Local helpdesk: tickets, workflow, comments, authorization, audit and persistence: OK')

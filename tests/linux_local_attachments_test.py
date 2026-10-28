#!/usr/bin/python3
import importlib.util,os,shutil,sqlite3,sys,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-attachments-') as tmp:
    path=Path(tmp)/'workspace.db';admin=mod.LocalStore(path);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    admin.save_task(0,'Aufgabe A','IT','Hoch',1);admin.save_task(0,'Aufgabe B','IT','Hoch',1)
    tasks={t['title']:t['id'] for t in admin.tasks()};a=tasks['Aufgabe A'];b=tasks['Aufgabe B']
    source=Path(tmp)/'dokument ä.txt';source.write_text('Testinhalt\nZeile 2',encoding='utf-8')
    admin.import_attachment('task',a,source);item=admin.attachments('task',a)[0]
    saved=Path(admin.verify_attachment('task',a,item['id']));assert saved.read_bytes()==source.read_bytes() and saved.stat().st_mode & 0o077 == 0 and saved.suffix=='.txt'
    reject(lambda:admin.archive_attachment('task',b,item['id']))
    oversized=Path(tmp)/'large.bin'
    with oversized.open('wb') as f:f.truncate(100*1024*1024+1)
    reject(lambda:admin.import_attachment('task',a,oversized));assert len(admin.attachments('task',a))==1
    # Failure of the audit write must roll back metadata and delete the new blob.
    with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER reject_attachment_audit BEFORE INSERT ON audit_log WHEN NEW.action='attachment.created' BEGIN SELECT RAISE(FAIL,'blocked'); END")
    reject(lambda:admin.import_attachment('task',a,source))
    assert len(admin.attachments('task',a))==1 and list(saved.parent.iterdir())==[saved]
    with sqlite3.connect(path) as db:db.execute('DROP TRIGGER reject_attachment_audit')
    # Failure of an archive audit must leave both metadata and object intact.
    with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER reject_archive_audit BEFORE INSERT ON audit_log WHEN NEW.action='attachment.archived' BEGIN SELECT RAISE(FAIL,'blocked'); END")
    reject(lambda:admin.archive_attachment('task',a,item['id']));assert len(admin.attachments('task',a))==1 and saved.exists()
    with sqlite3.connect(path) as db:db.execute('DROP TRIGGER reject_archive_audit')
    saved.write_bytes(b'modifiziert');reject(lambda:admin.verify_attachment('task',a,item['id']))
    saved.write_bytes(source.read_bytes());assert Path(admin.verify_attachment('task',a,item['id']))==saved
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Leser','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Leser'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'reader',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    reader=mod.LocalStore(path);reader.open();reader.login('reader','Admin Passwort 2026')
    reject(lambda:reader.attachments('task',a));reject(lambda:reader.import_attachment('task',a,source));reader.close()
    admin.close();admin.open();admin.login('admin','Admin Passwort 2026');assert admin.verify_attachment('task',a,item['id'])==str(saved)
    moved=Path(tmp)/'umgezogen';moved.mkdir();admin.backup(moved/'workspace.db');shutil.copytree(Path(tmp)/'attachments',moved/'attachments')
    restored=mod.LocalStore(moved/'workspace.db');restored.open();restored.login('admin','Admin Passwort 2026')
    relocated=Path(restored.verify_attachment('task',a,item['id']));assert relocated!=saved and relocated.read_bytes()==source.read_bytes()
    sys.path.insert(0,str(root/'clients/linux'));sys.modules['local_store']=mod;from attachment_sync import _staged_copy
    staged=moved/'staged';_staged_copy(restored,'task',a,item['storedPath'],item['sizeBytes'],item['sha256'],staged);assert staged.read_bytes()==source.read_bytes()
    reject(lambda:restored.attachment_path('task',a,str(Path(item['storedPath']).parent.parent/'999'/Path(item['storedPath']).name)))
    relocated.write_bytes(b'corrupted');reject(lambda:restored.verify_attachment('task',a,item['id']));reject(lambda:_staged_copy(restored,'task',a,item['storedPath'],item['sizeBytes'],item['sha256'],staged))
    restored.close()
    admin.archive_attachment('task',a,item['id']);assert admin.attachments('task',a)==[] and saved.exists()
    reject(lambda:admin.verify_attachment('task',a,item['id']));admin.close()
print('Local attachments: import, integrity, size, authorization, atomic audit, archive and persistence: OK')

#!/usr/bin/python3
import importlib.util,os,shutil,sqlite3,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-restore-') as tmp:
    path=Path(tmp)/'workspace.sqlite';source=Path(tmp)/'backup.sqlite'
    s=m.LocalStore(path);s.create('Admin','admin','Erstes Passwort 2026');s.open();s.login('admin','Erstes Passwort 2026');s.save_room(0,'Erster Raum','Berlin',3,'')
    s.save_project(0,'Projekt aus Sicherung','IT','','planned',s.session_employee());project=s.projects()[0]['id']
    s.create_task_in_project('Aufgabe aus Sicherung','IT','Mittel',s.session_employee(),0,0,project);task=s.tasks()[0]['id'];s.backup(source)
    with sqlite3.connect(source) as db:db.execute('DROP TABLE linux_room_sync_resolution_details')
    s.save_room(0,'Späterer Raum','Bonn',5,'');s.update_task_project(task,'Spätere Änderung','IT','Hoch',s.session_employee(),0,0,project,0);s.change_password('Erstes Passwort 2026','Anderes Passwort 2026')
    invalid=Path(tmp)/'invalid.sqlite';invalid.write_bytes(b'keine datenbank')
    for bad in (invalid,path):
        try:s.restore(bad);raise AssertionError('invalid restore allowed')
        except m.LocalStoreError:pass
        assert len(s.rooms())==2
    wrong=Path(tmp)/'wrong-schema.sqlite'
    with sqlite3.connect(wrong) as db:db.execute('create table unrelated(x)')
    try:s.restore(wrong);raise AssertionError('incorrect schema accepted')
    except m.LocalStoreError:pass
    assert len(s.rooms())==2
    # A conflict history first created after the backup does not change the
    # functional schema. An incompatible history table must still be rejected.
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE linux_leave_sync_resolutions (id INTEGER PRIMARY KEY,leave_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE linux_maintenance_successor_links(id INTEGER PRIMARY KEY,parent_id INTEGER NOT NULL,local_id INTEGER NOT NULL,old_external_id TEXT NOT NULL,server_json TEXT NOT NULL,local_json TEXT NOT NULL,choice TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    bad_maintenance=Path(tmp)/'malformed-maintenance-history.sqlite';shutil.copyfile(source,bad_maintenance)
    with sqlite3.connect(bad_maintenance) as db:db.execute('CREATE TABLE linux_maintenance_successor_links(id INTEGER PRIMARY KEY)')
    try:s.restore(bad_maintenance);raise AssertionError('incompatible maintenance history accepted')
    except m.LocalStoreError:pass
    malformed=Path(tmp)/'malformed-history.sqlite';shutil.copyfile(source,malformed)
    with sqlite3.connect(malformed) as db:db.execute('CREATE TABLE linux_leave_sync_resolutions (id INTEGER PRIMARY KEY)')
    try:s.restore(malformed);raise AssertionError('incompatible history accepted')
    except m.LocalStoreError:pass
    assert len(s.rooms())==2
    other=m.LocalStore(path);other.open();other.login('admin','Anderes Passwort 2026')
    try:s.restore(source);raise AssertionError('restore with other session')
    except m.LocalStoreError as e:assert 'Sitzungen' in str(e)
    assert len(other.rooms())==2;other.close()
    try:s.rooms();raise AssertionError('session not invalidated after failed lock upgrade')
    except m.LocalStoreError:pass
    s.login('admin','Anderes Passwort 2026')
    previous=Path(s.restore(source));assert previous.exists() and previous.stat().st_mode & 0o077 == 0
    with sqlite3.connect(path) as db:assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='linux_room_sync_resolution_details'").fetchone()==(1,)
    with sqlite3.connect(previous) as db:
        assert db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='linux_maintenance_successor_links'").fetchone()[0]==1
        assert db.execute('SELECT title FROM tasks WHERE id=?',(task,)).fetchone()==('Spätere Änderung',)
        assert db.execute('SELECT count(*) FROM project_tasks WHERE task_id=?',(task,)).fetchone()==(0,)
    try:s.rooms();raise AssertionError('session survived restore')
    except m.LocalStoreError:pass
    s.close();s.open()
    try:s.login('admin','Anderes Passwort 2026');raise AssertionError('later password survived')
    except m.LocalStoreError:pass
    s.login('admin','Erstes Passwort 2026');assert [v['name'] for v in s.rooms()]==['Erster Raum']
    assert s.tasks()[0]['title']=='Aufgabe aus Sicherung' and s.tasks()[0]['projectIds']==[project]
    assert any('database.restore.completed' in e for e in s.audit());s.close()
    assert not list(Path(tmp).glob('*.restore-snapshot-*'))
print('Restore: incompatible input, concurrent session, prior backup, reauthentication and audit: OK')
with tempfile.TemporaryDirectory(prefix='aura-restore-attachment-trigger-') as tmp:
    path=Path(tmp)/'workspace.sqlite';backup=Path(tmp)/'backup.sqlite';source=Path(tmp)/'datei.txt';source.write_text('Inhalt')
    s=m.LocalStore(path);s.create('Admin','admin','Passwort 2026 sicher');s.open();s.login('admin','Passwort 2026 sicher')
    s.save_task(0,'Aufgabe','IT','Mittel',s.session_employee());task=s.tasks()[0]['id'];s.import_attachment('task',task,source)
    attachment=s.attachments('task',task)[0]['id'];s.archive_attachment('task',task,attachment)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE linux_task_attachment_sync_config(singleton INTEGER PRIMARY KEY,client_id TEXT)')
        db.execute('CREATE TABLE linux_task_attachment_sync_guard(singleton INTEGER PRIMARY KEY,suppressed INTEGER NOT NULL)')
        db.execute('INSERT INTO linux_task_attachment_sync_guard VALUES(1,0)')
        db.execute('CREATE TABLE linux_task_attachment_sync_outbox(seq INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,action TEXT NOT NULL,attachment_id INTEGER NOT NULL)')
    s.backup(backup);s.restore_attachment('task',task,attachment)
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE linux_task_attachment_sync_archive_actor(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL)')
    with sqlite3.connect(path) as db:assert db.execute("SELECT action FROM linux_task_attachment_sync_outbox").fetchall()==[('restore',)]
    previous=s.restore(backup);s.close();s.open();s.login('admin','Passwort 2026 sicher')
    with sqlite3.connect(path) as db:assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='linux_task_attachment_sync_restore'").fetchone()==(1,)
    with sqlite3.connect(path) as db:db.execute('DELETE FROM linux_task_attachment_sync_guard')
    try:s.restore_attachment('task',task,attachment);raise AssertionError('missing outbox guard accepted')
    except m.LocalStoreError:pass
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT active FROM attachments WHERE id=?',(attachment,)).fetchone()==(0,)
        db.execute('INSERT INTO linux_task_attachment_sync_guard VALUES(1,0)')
    s.restore_attachment('task',task,attachment)
    with sqlite3.connect(path) as db:assert db.execute("SELECT action FROM linux_task_attachment_sync_outbox").fetchall()==[('restore',)]
    with sqlite3.connect(previous) as db:assert db.execute("SELECT action FROM linux_task_attachment_sync_outbox").fetchall()==[('restore',)]
    s.close()
print('Restore: older attachment snapshot receives outbox trigger before use: OK')

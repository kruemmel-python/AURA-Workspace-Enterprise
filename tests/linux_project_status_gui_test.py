#!/usr/bin/python3
"""Exercise the project status shortcut in local and central GTK modes."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-project-status-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite'
    for key in ('AURA_SYNC_URL','AURA_SYNC_TOKEN','AURA_SYNC_CA_CERT'):os.environ.pop(key,None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
    store.save_project(0,'Lokales Projekt','IT','Beschreibung','planned',store.session_employee())
    w=mod.AuraWindow();w.local=store;w.local_mode=True;w.show_all()
    def children(widget):
        result=[widget]
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children():result.extend(children(child))
        return result
    def wait(predicate):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if predicate():return
            time.sleep(.02)
        raise AssertionError('GTK status update timed out')
    def shortcut():
        return [x for x in children(w.content) if isinstance(x,mod.Gtk.Button) and x.get_label()=='Projektstatus weiter']
    def visible_status(status):
        return any(isinstance(x,mod.Gtk.Label) and x.get_text().startswith('Projekt ') and x.get_text().endswith(' · '+status) for x in children(w.content))
    w.select(2);wait(lambda:len(shortcut())==1 and visible_status('planned'))
    for expected in ('active','on_hold','completed','planned'):
        shortcut()[0].clicked();wait(lambda:store.projects()[0]['status']==expected and visible_status(expected))
    project=store.projects()[0];assert project['name']=='Lokales Projekt' and project['description']=='Beschreibung'
    store.save_project(project['id'],'Anderer Name','IT','Neuere Beschreibung','planned',project['ownerId'])
    shortcut()[0].clicked();wait(lambda:store.projects()[0]['status']=='active' and visible_status('active'))
    project=store.projects()[0];assert project['name']=='Anderer Name' and project['description']=='Neuere Beschreibung'
    try:store.project_status(project['id'],'planned','active');raise AssertionError('stale local status accepted')
    except mod.LocalStoreError:pass
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:
        db.execute("CREATE TRIGGER deny_project_status_audit BEFORE INSERT ON audit_log WHEN NEW.action='project.status.changed' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
    try:store.project_status(project['id'],'active','on_hold');raise AssertionError('status survived failed audit')
    except mod.LocalStoreError:pass
    assert store.projects()[0]['status']=='active'
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:db.execute('DROP TRIGGER deny_project_status_audit')
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url=f'http://127.0.0.1:{port}';env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_JOURNAL':tmp+'/server.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_URL':url,'AURA_SYNC_TOKEN':'test-token'})
    for key in ('AURA_SYNC_IDENTITIES','AURA_SYNC_REQUIRE_IDENTITIES','AURA_SYNC_REQUIRE_TLS','AURA_SYNC_TLS_CERT','AURA_SYNC_TLS_KEY','AURA_SYNC_CA_CERT'):env.pop(key,None)
    env['AURA_SYNC_IDENTITIES']=json.dumps({'test-token':{'employeeId':1,'permissions':['*']},'reader-token':{'employeeId':2,'permissions':['tasks.read','projects.read']}})
    server=subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server failed to start')
        client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
        def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,check=True,capture_output=True,text=True).stdout)
        cli('-action','create-project','-name','Zentrales Projekt','-department','IT','-owner-id','1','-description','Zentral')
        w.local_mode=False;w.local=None;store.close();w.url=url;w.token='test-token';w.ca='';w.select(2)
        wait(lambda:len(shortcut())==1 and visible_status('planned'))
        for expected in ('active','on_hold','completed','planned'):
            shortcut()[0].clicked();wait(lambda:cli('-action','projects')[0]['status']==expected and visible_status(expected))
        project=cli('-action','projects')[0];assert project['name']=='Zentrales Projekt' and project['description']=='Zentral'
        cli('-action','update-project','-id',project['id'],'-name','Neuer zentraler Name','-department','IT','-owner-id','1','-description','Neuer Inhalt','-status','planned')
        shortcut()[0].clicked();wait(lambda:cli('-action','projects')[0]['status']=='active' and visible_status('active'))
        project=cli('-action','projects')[0];assert project['name']=='Neuer zentraler Name' and project['description']=='Neuer Inhalt'
        stale=subprocess.run([str(client),'-action','project-status','-id',project['id'],'-expected-status','planned','-status','active'],env=env,capture_output=True,text=True)
        assert stale.returncode!=0 and '409' in stale.stderr
        task=cli('-action','create-task','-title','Projektaufgabe','-department','IT','-priority','Mittel','-assignee-id','1')
        w.select(2)
        def button(name):return [x for x in children(w.content) if isinstance(x,mod.Gtk.Button) and x.get_label()==name]
        wait(lambda:len(button('Projektzuordnungen'))==1);button('Projektzuordnungen')[0].clicked()
        wait(lambda:len(button('Zuordnen'))==1);button('Zuordnen')[0].clicked()
        wait(lambda:len(button('Entfernen'))==1)
        assert {'projectId':project['id'],'taskId':task['id']} in cli('-action','sync-project-assignments')
        stale_edge={'clientId':'stale-test','operationId':'stale-1','projectId':project['id'],'taskId':task['id'],'present':True,'expected':False}
        rejected=subprocess.run([str(client),'-action','sync-project-assignment-event','-payload',json.dumps(stale_edge)],env=env,capture_output=True,text=True)
        assert rejected.returncode!=0 and '409' in rejected.stderr
        cli('-action','sync-project-assignment-event','-payload',json.dumps({'clientId':'other','operationId':'removed','projectId':project['id'],'taskId':task['id'],'present':False,'expected':True}))
        def dismiss_stale():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
            if not dialogs:return True
            assert '409' in dialogs[-1].get_property('secondary-text');dialogs[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        mod.GLib.idle_add(dismiss_stale);button('Entfernen')[0].clicked();wait(lambda:len(button('Zuordnen'))==1)
        button('Zuordnen')[0].clicked();wait(lambda:len(button('Entfernen'))==1)
        button('Entfernen')[0].clicked();wait(lambda:len(button('Zuordnen'))==1)
        assert not cli('-action','sync-project-assignments')
        button('Zuordnen')[0].clicked();wait(lambda:len(button('Entfernen'))==1)
        cli('-action','archive-project','-id',project['id'])
        w.show_central_project_assignments(task);wait(lambda:len(button('Entfernen'))==1 and any(isinstance(x,mod.Gtk.Label) and 'Archiviert' in x.get_text() for x in children(w.content)))
        button('Entfernen')[0].clicked();wait(lambda:len(button('Entfernen'))==0 and any(isinstance(x,mod.Gtk.Label) and x.get_text()=='Keine zuordenbaren Projekte vorhanden.' for x in children(w.content)))
        assert not cli('-action','sync-project-assignments')
        w.select(2);wait(lambda:len(button('Status weiter'))==1)
        for status in ('in_progress','done','open'):
            button('Status weiter')[0].clicked();wait(lambda:cli('-action','tasks')[0]['status']==status and len(button('Status weiter'))==1 and any(isinstance(x,mod.Gtk.Label) and x.get_text().endswith(' · '+status) for x in children(w.content)))
        cli('-action','create-ticket','-title','Status-Ticket','-category','IT','-priority','Mittel','-assignee-id','1')
        w.select(4);wait(lambda:len(button('Status weiter'))==1)
        for status in ('in_progress','resolved','closed','open'):
            button('Status weiter')[0].clicked();wait(lambda:cli('-action','tickets')[0]['status']==status and len(button('Status weiter'))==1 and any(isinstance(x,mod.Gtk.Label) and x.get_text().endswith(' · '+status) for x in children(w.content)))
        cli('-action','create-task','-title','Leseraufgabe','-department','IT','-priority','Mittel','-assignee-id','2')
        w.token='reader-token';w.select(2)
        wait(lambda:any(isinstance(x,mod.Gtk.Label) and x.get_text()=='Leseraufgabe' for x in children(w.content)))
        assert not button('Projektzuordnungen')
    finally:
        server.terminate();server.wait(timeout=5)
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
print('GTK project status shortcut: local database and central server OK')

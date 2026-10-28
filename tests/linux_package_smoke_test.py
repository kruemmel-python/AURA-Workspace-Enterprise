#!/usr/bin/python3
"""Build, unpack and exercise the Linux distribution in an isolated directory."""
import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-package-test-') as tmp:
    tmp=Path(tmp);archive=tmp/'package.tar.gz'
    subprocess.run([str(root/'clients/linux/package.sh'),str(archive)],check=True)
    original=archive.read_bytes()
    repeat=subprocess.run([str(root/'clients/linux/package.sh'),str(archive)],capture_output=True,text=True)
    assert repeat.returncode!=0 and archive.read_bytes()==original
    with tarfile.open(archive,'r:gz') as tar:
        names=tar.getnames()
        assert not any('__pycache__' in n or n.endswith('.sqlite') or n.endswith('.db') for n in names)
        tar.extractall(tmp/'unpacked',filter='data')
    bundle=tmp/'unpacked'/'aura-workspace-linux-x64'
    env=os.environ.copy();env['AURA_LOCAL_DB']=str(tmp/'workspace.sqlite')
    env.pop('AURA_PYTHON',None)
    for key in ('AURA_SYNC_URL','AURA_SYNC_TOKEN','AURA_SYNC_CA_CERT','AURA_SYNC_IDENTITIES','AURA_SYNC_REQUIRE_TLS','AURA_SYNC_TLS_CERT','AURA_SYNC_TLS_KEY'):
        env.pop(key,None)
    launcher=bundle/'clients/linux/start.sh'
    library=bundle/'KR_BUILD/linux-x64/libaura_linux_local.so'
    hidden=library.with_suffix('.missing')
    library.rename(hidden)
    try:
        result=subprocess.run([str(launcher)],cwd=tmp,env=env,capture_output=True,text=True)
        assert result.returncode != 0 and 'lokale AURA-Bibliothek' in result.stderr
    finally:
        hidden.rename(library)
    python_without_site=tmp/'python-without-site'
    python_without_site.write_text('#!/bin/sh\nexec /usr/bin/python3 -S "$@"\n')
    python_without_site.chmod(0o755)
    missing_gtk_env=env.copy();missing_gtk_env['AURA_PYTHON']=str(python_without_site)
    result=subprocess.run([str(launcher)],cwd=tmp,env=missing_gtk_env,capture_output=True,text=True)
    assert result.returncode != 0 and 'python3-gi' in result.stderr
    # Exercise the packaged native library and actual GTK window, without
    # importing any modules from the original checkout.
    script='''import sys
sys.path.insert(0,sys.argv[1])
from local_store import LocalStore
from aura_gui import AuraWindow, Gtk
s=LocalStore(sys.argv[2]);s.create('Admin','admin','Sicheres Passwort 2026');s.open();s.login('admin','Sicheres Passwort 2026')
s.save_room(0,'Paketraum','Ubuntu',4,'');assert s.rooms()[0]['name']=='Paketraum'
s.save_employee_with_role(0,'Paket Mitarbeiter','Entwicklung','IT','employee')
assert next(e for e in s.employees() if e['name']=='Paket Mitarbeiter')['systemRole']=='employee'
try:s.save_employee_with_role(0,'Unvollständig','Entwicklung','IT','missing');raise AssertionError('invalid role accepted')
except Exception as exc:
    if isinstance(exc,AssertionError):raise
assert not any(e['name']=='Unvollständig' for e in s.employees())
s.create_leave(1900000000,1900086400,'Paket Urlaub');assert s.leave()[0]['reason']=='Paket Urlaub'
s.save_project(0,'Paketprojekt','IT','','planned',s.session_employee());project=s.projects()[0]['id']
s.create_task_in_project('Paketaufgabe','IT','Mittel',s.session_employee(),0,0,project)
task=s.tasks()[0]['id'];assert s.tasks()[0]['projectIds']==[project]
s.update_task_project(task,'Paketaufgabe aktualisiert','IT','Mittel',s.session_employee(),0,0,project,0)
try:s.update_task_project(task,'Veraltet','IT','Mittel',s.session_employee(),0,0,project,project);raise AssertionError('stale project accepted')
except Exception as exc:
    if isinstance(exc,AssertionError):raise
assert s.tasks()[0]['title']=='Paketaufgabe aktualisiert' and s.tasks()[0]['projectIds']==[]
s.update_task_project(task,'Paketaufgabe','IT','Mittel',s.session_employee(),0,0,0,project)
s.save_subtask(task,0,'Paket Teilaufgabe','open',s.session_employee(),0);sub=s.subtasks(task)[0]
s.save_subtask_compared(task,sub['id'],'Paket bearbeitet','done',s.session_employee(),0,sub)
try:s.save_subtask_compared(task,sub['id'],'Veraltet','open',s.session_employee(),0,sub);raise AssertionError('stale subtask accepted')
except Exception as exc:
    if isinstance(exc,AssertionError):raise
assert s.subtasks(task)[0]['title']=='Paket bearbeitet' and s.subtasks(task)[0]['status']=='done'
employee=next(e['id'] for e in s.employees() if e['name']=='Paket Mitarbeiter')
s.save_role(0,'packaged_collaborator','Paket Zusammenarbeit','','tasks.read,collaboration.comment,leave.request');s.assign_role(employee,'packaged_collaborator');s.create_account(employee,'paket-user','Temporär Passwort 2026')
viewer=LocalStore(sys.argv[2]);viewer.open();assert viewer.login('paket-user','Temporär Passwort 2026');viewer.change_password('Temporär Passwort 2026','Paket Passwort 2026')
viewer.add_comment('task',task,'Paketmitarbeit');assert viewer.comments('task',task)[0]['body']=='Paketmitarbeit'
viewer.create_leave(1901000000,1901086400,'Paket Mitarbeiterurlaub')
request=next(v for v in s.leave() if v['reason']=='Paket Mitarbeiterurlaub')
import sqlite3
with sqlite3.connect(sys.argv[2]) as db:db.execute('UPDATE leave_requests SET ends_at=ends_at+3600 WHERE id=?',(request['id'],))
try:s.decide_leave_compared(request['id'],True,request);raise AssertionError('stale leave accepted')
except Exception as exc:
    if isinstance(exc,AssertionError):raise
current=next(v for v in s.leave() if v['id']==request['id']);assert current['status']=='pending' and current['endsAt']==request['endsAt']+3600
s.decide_leave_compared(current['id'],True,current)
assert next(v for v in viewer.leave() if v['id']==request['id'])['status']=='approved'

from pathlib import Path
sample=Path(sys.argv[2]).parent/'paket-datei.txt';sample.write_text('Paketinhalt');viewer.import_attachment('task',task,sample)
attachment=viewer.attachments('task',task)[0];assert Path(viewer.verify_attachment('task',task,attachment['id'])).read_text()=='Paketinhalt'
try:viewer.archive_task(task);raise AssertionError('restricted user archived task')
except Exception as exc:
    if isinstance(exc,AssertionError):raise
w=AuraWindow();w.local=s;w.local_mode=True;w.show_all();w.select(3)
import time
deadline=time.monotonic()+10
def children(widget):
    result=[widget]
    if isinstance(widget,Gtk.Container):
        for child in widget.get_children():result.extend(children(child))
    return result
while time.monotonic()<deadline:
    while Gtk.events_pending(): Gtk.main_iteration()
    labels=[x.get_text() for x in children(w.content) if isinstance(x,Gtk.Label)]
    if 'Paket Urlaub' in labels and 'Paket Mitarbeiter' in labels:break
    time.sleep(.02)
else:raise AssertionError(labels)
w.disconnect_by_func(Gtk.main_quit);w.destroy()
viewer.close();s.close()
'''
    subprocess.run(['/usr/bin/python3','-c',script,str(bundle/'clients/linux'),str(tmp/'workspace.sqlite')],cwd=tmp,env=env,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_JOURNAL':str(tmp/'events.ndjson'),'AURA_CENTRAL_DB':str(tmp/'central.sqlite'),'AURA_SYNC_TOKEN':'isolated-test-token'})
    env['AURA_SYNC_IDENTITIES']=json.dumps({'isolated-test-token':{'employeeId':1,'permissions':['*']},'package-employee':{'employeeId':2,'permissions':['leave.request']}})
    server=subprocess.Popen([str(bundle/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],cwd=tmp,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:assert response.status==200
                break
            except OSError:time.sleep(.05)
        else:raise AssertionError('packaged server failed to start')
        env['AURA_SYNC_URL']=f'http://127.0.0.1:{port}'
        client=bundle/'KR_BUILD/linux-x64/aura-workspace-enterprise'
        subprocess.run([str(client),'-action','create-room','-name','Paketserver-Raum','-location','Ubuntu','-capacity','4'],cwd=tmp,env=env,check=True,capture_output=True,text=True)
        rooms=json.loads(subprocess.run([str(client),'-action','rooms'],cwd=tmp,env=env,check=True,capture_output=True,text=True).stdout)
        assert any(room['name']=='Paketserver-Raum' for room in rooms)
        def central(*args):return json.loads(subprocess.run([str(client),*args],cwd=tmp,env=env,check=True,capture_output=True,text=True).stdout)
        employee_env=env.copy();employee_env['AURA_SYNC_TOKEN']='package-employee'
        request=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1902000000','-ends-at','1902086400','-reason','Paketserver Urlaub'],cwd=tmp,env=employee_env,check=True,capture_output=True,text=True).stdout)
        expected=next(v for v in central('-action','leave') if v['id']==request['id'])
        stale=dict(expected);stale['endsAt']+=3600
        refused=subprocess.run([str(client),'-action','leave-decision','-id',request['id'],'-status','approved','-payload',json.dumps(stale)],cwd=tmp,env=env,capture_output=True,text=True)
        assert refused.returncode and '409' in refused.stderr
        assert next(v for v in central('-action','leave') if v['id']==request['id'])['status']=='pending'
        central('-action','leave-decision','-id',request['id'],'-status','approved','-payload',json.dumps(expected))
        assert next(v for v in central('-action','leave') if v['id']==request['id'])['status']=='approved'
        project=central('-action','create-project','-name','Paketserver Projekt','-department','IT','-owner-id','1')
        task=central('-action','create-task','-title','Paketserver Aufgabe','-department','IT','-priority','Mittel','-assignee-id','1','-project-id',project['id'])
        assert {'projectId':project['id'],'taskId':task['id']} in central('-action','sync-project-assignments')
        sub=central('-action','subtask-save','-task-id',task['id'],'-title','Paketserver Teil','-status','open','-assignee-id','1','-due','0')
        central('-action','subtask-save','-task-id',task['id'],'-id',sub['id'],'-title','Paketserver Teil geändert','-status','done','-assignee-id','1','-due','0','-payload',json.dumps(sub))
        stale=subprocess.run([str(client),'-action','subtask-save','-task-id',task['id'],'-id',sub['id'],'-title','Veraltet','-status','open','-assignee-id','1','-due','0','-payload',json.dumps(sub)],cwd=tmp,env=env,capture_output=True,text=True)
        assert stale.returncode and '409' in stale.stderr
        central('-action','update-task','-id',task['id'],'-title','Paketserver geändert','-department','IT','-priority','Hoch','-assignee-id','1','-expected-project-id',project['id'],'-project-id','')
        assert not any(edge['taskId']==task['id'] for edge in central('-action','sync-project-assignments'))
        history=json.loads(subprocess.run([str(client),'-action','central-event-history'],cwd=tmp,env=env,check=True,capture_output=True,text=True).stdout)
        assert any('room' in event['topic'] for event in history)
        assert all('payload' not in event for event in history)
        gui_history='''import sys,time
sys.path.insert(0,sys.argv[1])
from aura_gui import AuraWindow,Gtk
w=AuraWindow();w.show_all();w.select(5)
def children(widget):
    result=[widget]
    if isinstance(widget,Gtk.Container):
        for child in widget.get_children():result.extend(children(child))
    return result
buttons=[x for x in children(w.content) if isinstance(x,Gtk.Button) and x.get_label()=='Server-Ereignisverlauf']
assert len(buttons)==1
buttons[0].clicked()
end=time.monotonic()+10
while time.monotonic()<end:
    while Gtk.events_pending():Gtk.main_iteration()
    labels=[x.get_text() for x in children(w.content) if isinstance(x,Gtk.Label)]
    if any('room.' in value for value in labels):break
    time.sleep(.02)
else:raise AssertionError(labels)
w.disconnect_by_func(Gtk.main_quit);w.destroy()
'''
        subprocess.run(['/usr/bin/python3','-c',gui_history,str(bundle/'clients/linux')],cwd=tmp,env=env,check=True)
    finally:
        server.terminate();server.wait(timeout=5)
print('Linux package: isolated build, no overwrite, GTK, database and server: OK')

#!/usr/bin/python3
"""End-to-end project assignments against a real TLS server and local databases."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from project_sync import sync_projects
from project_assignment_sync import sync_project_assignments,inspect_assignment_conflict,resolve_assignment_conflict,AssignmentParentsPendingError
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-assignment-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage','tasks.manage']},'tasks':{'employeeId':9,'permissions':['tasks.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 ctx=ssl.create_default_context(cafile=str(cert))
 def start():
  process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();return process
   except OSError:time.sleep(.05)
  raise AssertionError('server did not start')
 def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,capture_output=True,text=True,check=True).stdout)
 process=start()
 try:
  db=Path(tmp)/'local.db';store=LocalStore(db);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock();current=lambda:True
  sync_employees(store,lock,current,client,url,token,str(cert));admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,current,client,url,token,str(cert));actor=store.session_employee()
  store.save_project(0,'Erstes','IT','','planned',actor);store.save_project(0,'Zweites','IT','','planned',actor)
  projects=store.projects();p1=next(x['id'] for x in projects if x['name']=='Erstes');p2=next(x['id'] for x in projects if x['name']=='Zweites')
  store.create_task_in_project('Aufgabe','IT','Mittel',actor,0,0,p1);task=store.tasks()[0]['id']
  store.update_task_project(task,'Bearbeitet','IT','Mittel',actor,0,0,p1,p2)
  try:store.update_task_project(task,'Veraltet','IT','Mittel',actor,0,0,p1,p1);raise AssertionError('stale project accepted')
  except LocalStoreError:pass
  assert store.tasks()[0]['title']=='Bearbeitet' and store.tasks()[0]['projectIds']==[p2]
  store.update_task_project(task,'Aufgabe','IT','Mittel',actor,0,0,p2,p1)
  try:store.create_task_in_project('Ungültig','IT','Mittel',actor,0,0,999999);raise AssertionError('invalid project accepted')
  except LocalStoreError:pass
  assert all(x['title']!='Ungültig' for x in store.tasks())
  sync=lambda:sync_project_assignments(store,lock,current,client,url,token,str(cert))
  try:sync();raise AssertionError('missing prerequisites accepted')
  except LocalStoreError:pass
  sync_projects(store,lock,current,client,url,token,str(cert));sync_tasks(store,lock,current,client,url,token,str(cert))
  with sqlite3.connect(db) as sql:sql.execute("CREATE TABLE linux_dummy(x)")
  assert sync()=={'sent':1,'received':0};edges=cli('-action','sync-project-assignments');assert len(edges)==1
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_assignment_queue BEFORE INSERT ON linux_project_assignment_sync_outbox BEGIN SELECT RAISE(ABORT,'queue failure'); END")
  try:store.create_task_in_project('Rollback','IT','Mittel',actor,0,0,p1);raise AssertionError('task survived failed assignment outbox')
  except LocalStoreError:pass
  try:store.update_task_project(task,'Rollback Änderung','IT','Hoch',actor,0,0,p1,p2);raise AssertionError('task update survived failed assignment outbox')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM tasks WHERE title='Rollback'").fetchone()[0]==0
   assert sql.execute('SELECT title,priority FROM tasks WHERE id=?',(task,)).fetchone()==('Aufgabe','Mittel')
   assert sql.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(task,)).fetchall()==[(p1,)]
   assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==0
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_assignment_queue')
  remote_project=edges[0]['projectId'];remote_task=edges[0]['taskId']
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_assignment_ack BEFORE DELETE ON linux_project_assignment_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  store.assign_project(task,p2)
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_assignment_ack')
  process.terminate();process.wait(timeout=5)
  try:sync();raise AssertionError('offline sync accepted')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':2,'received':0};edges=cli('-action','sync-project-assignments');assert len(edges)==1 and edges[0]['projectId']!=remote_project
  other_project=edges[0]['projectId']
  def remote(op,project,present,expected):cli('-action','sync-project-assignment-event','-payload',json.dumps({'clientId':'other','operationId':op,'projectId':project,'taskId':remote_task,'present':present,'expected':expected}))
  remote('remove',other_project,False,True);remote('add',remote_project,True,False)
  assert sync()=={'sent':0,'received':2}
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(task,)).fetchall()==[(p1,)]
  store.assign_project(task,p2);remote('remove-again',remote_project,False,True)
  try:sync();raise AssertionError('concurrent change overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  reviewed=inspect_assignment_conflict(store,lock,current,client,url,token,str(cert));assert reviewed['preview']['localPresent']==False and reviewed['remotePresent']==False
  resolve_assignment_conflict(store,lock,current,client,url,token,str(cert),reviewed,False);assert sync()=={'sent':1,'received':0}
  assert cli('-action','sync-project-assignments')==[{'projectId':other_project,'taskId':remote_task}]
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_conflicts').fetchone()[0]==1
  store.assign_project(task,0);remote('remove2',other_project,False,True)
  reviewed=inspect_assignment_conflict(store,lock,current,client,url,token,str(cert))
  remote('restore2',other_project,True,False)
  try:resolve_assignment_conflict(store,lock,current,client,url,token,str(cert),reviewed,True);raise AssertionError('stale review accepted')
  except LocalStoreError:pass
  reviewed=inspect_assignment_conflict(store,lock,current,client,url,token,str(cert));assert reviewed['remotePresent'] and not reviewed['preview']['localPresent']
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_assignment_audit BEFORE INSERT ON audit_log WHEN NEW.action='project_assignments.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:resolve_assignment_conflict(store,lock,current,client,url,token,str(cert),reviewed,True);raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert not sql.execute('SELECT 1 FROM project_tasks WHERE task_id=?',(task,)).fetchone()
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_assignment_audit')
  resolve_assignment_conflict(store,lock,current,client,url,token,str(cert),reviewed,True)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(task,)).fetchall()==[(p2,)]
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_outbox').fetchone()[0]==0
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_conflicts').fetchone()[0]==2
  # Individual UI operations preserve other project edges across offline
  # creation, server restart and a separate removal.
  process.terminate();process.wait(timeout=5)
  store.set_project_edge(task,p1,False,True)
  assert set(store.tasks()[0]['projectIds'])=={p1,p2}
  try:store.update_task_project(task,'Nicht überschreiben','IT','Mittel',actor,0,0,p2,p1);raise AssertionError('multiple projects replaced by stale form')
  except LocalStoreError:pass
  assert store.tasks()[0]['title']!='Nicht überschreiben' and set(store.tasks()[0]['projectIds'])=={p1,p2}
  try:store.assign_project(task,0);raise AssertionError('legacy reassignment erased multiple projects')
  except LocalStoreError as exc:assert 'mehreren Projekten' in str(exc)
  assert set(store.tasks()[0]['projectIds'])=={p1,p2}
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT present,expected FROM linux_project_assignment_sync_outbox').fetchall()==[(1,0)]
  process=start();assert sync()=={'sent':1,'received':0}
  assert {edge['projectId'] for edge in cli('-action','sync-project-assignments')}=={remote_project,other_project}
  store.set_project_edge(task,p2,True,False);assert sync()=={'sent':1,'received':0}
  assert cli('-action','sync-project-assignments')==[{'projectId':remote_project,'taskId':remote_task}]
  assert store.tasks()[0]['projectIds']==[p1]
  with sqlite3.connect(db) as sql:
   sql.execute("CREATE TRIGGER fail_local_edge_audit BEFORE INSERT ON audit_log WHEN NEW.action='project.task.assigned' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.set_project_edge(task,p2,False,True);raise AssertionError('edge survived failed audit')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_project_assignment_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_local_edge_audit')
  assert store.tasks()[0]['projectIds']==[p1]
  store.save_project(0,'Wartendes Projekt','IT','','planned',actor)
  pending_project=next(v['id'] for v in store.projects() if v['name']=='Wartendes Projekt')
  store.create_task_in_project('Wartende Aufgabe','IT','Mittel',actor,0,0,pending_project)
  pending_task=next(v['id'] for v in store.tasks() if v['title']=='Wartende Aufgabe')
  try:sync();raise AssertionError('assignment sent before its parents')
  except AssignmentParentsPendingError:pass
  assert next(v for v in store.tasks() if v['id']==pending_task)['projectIds']==[pending_project]
  sync_tasks(store,lock,current,client,url,token,str(cert))
  try:sync();raise AssertionError('assignment sent before project')
  except AssignmentParentsPendingError:pass
  sync_projects(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':1,'received':0}
  with sqlite3.connect(db) as sql:
   task_ext=sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(pending_task,)).fetchone()[0]
   project_ext=sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='project' AND local_id=?",(pending_project,)).fetchone()[0]
  assert {'taskId':task_ext,'projectId':project_ext} in cli('-action','sync-project-assignments')
  central_task=cli('-action','create-task','-title','Zentral mit Projekt','-department','IT','-priority','Mittel','-assignee-id','9','-project-id',remote_project)
  process.terminate();process.wait(timeout=5);process=start()
  assert {'taskId':central_task['id'],'projectId':remote_project} in cli('-action','sync-project-assignments')
  sync_tasks(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':1}
  imported=next(v for v in store.tasks() if v['title']=='Zentral mit Projekt')
  assert imported['projectIds']==[p1]
  cli('-action','update-task','-id',central_task['id'],'-title','Zentral geändert','-department','IT','-priority','Hoch','-assignee-id','9','-expected-project-id',remote_project,'-project-id',other_project)
  process.terminate();process.wait(timeout=5);process=start()
  assert {'taskId':central_task['id'],'projectId':other_project} in cli('-action','sync-project-assignments')
  sync_tasks(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':2}
  imported=next(v for v in store.tasks() if v['id']==imported['id'])
  assert imported['title']=='Zentral geändert' and imported['priority']=='Hoch' and imported['projectIds']==[p2]
  repeating=cli('-action','create-task','-title','Zentraler Zyklus','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900400000','-recurrence','7','-project-id',remote_project)
  cli('-action','update-task','-id',repeating['id'],'-title','Zentraler Zyklus','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900400000','-recurrence','7','-expected-project-id',remote_project,'-project-id',other_project)
  cli('-action','subtask-save','-task-id',repeating['id'],'-title','Zyklusschritt','-status','done','-assignee-id','9','-due','1900390000')
  cli('-action','task-status','-id',repeating['id'],'-status','done')
  process.terminate();process.wait(timeout=5);process=start()
  central_cycles=[v for v in cli('-action','sync-tasks') if v['title']=='Zentraler Zyklus'];assert len(central_cycles)==2
  successor=next(v for v in central_cycles if v['id']!=repeating['id'])
  edges=cli('-action','sync-project-assignments')
  assert {'taskId':repeating['id'],'projectId':other_project} in edges and {'taskId':successor['id'],'projectId':other_project} in edges
  assert not any(e['taskId'] in (repeating['id'],successor['id']) and e['projectId']==remote_project for e in edges)
  sync_tasks(store,lock,current,client,url,token,str(cert));sync()
  local_cycles=[v for v in store.tasks() if v['title']=='Zentraler Zyklus'];assert len(local_cycles)==2 and all(v['projectIds']==[p2] for v in local_cycles)
  for cycle in local_cycles:
   child=store.subtasks(cycle['id']);assert len(child)==1 and child[0]['title']=='Zyklusschritt' and child[0]['assigneeId']==actor
   assert child[0]['status']==('done' if cycle['status']=='done' else 'open')
   assert child[0]['dueAt']==(1900390000 if cycle['status']=='done' else 1900390000+7*86400)
  store.create_task_in_project('Lokaler Zyklus','IT','Mittel',actor,1900500000,7,p1)
  local_cycle=next(v for v in store.tasks() if v['title']=='Lokaler Zyklus')
  store.update_task_project(local_cycle['id'],'Lokaler Zyklus','IT','Mittel',actor,1900500000,7,p1,p2)
  store.save_subtask(local_cycle['id'],0,'Lokaler Zyklusschritt','done',actor,1900490000)
  store.task_status(local_cycle['id'],'done')
  local_cycles=[v for v in store.tasks() if v['title']=='Lokaler Zyklus'];assert len(local_cycles)==2 and all(v['projectIds']==[p2] for v in local_cycles)
  for cycle in local_cycles:
   child=store.subtasks(cycle['id']);assert len(child)==1 and child[0]['assigneeId']==actor
   assert child[0]['status']==('done' if cycle['status']=='done' else 'open')
   assert child[0]['dueAt']==(1900490000 if cycle['status']=='done' else 1900490000+7*86400)
  sync_tasks(store,lock,current,client,url,token,str(cert));sync()
  with sqlite3.connect(db) as sql:
   local_external=[sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(v['id'],)).fetchone()[0] for v in local_cycles]
  edges=cli('-action','sync-project-assignments')
  assert all({'taskId':ext,'projectId':other_project} in edges for ext in local_external)
  children=cli('-action','sync-subtasks')
  assert all(any(c['taskId']==ext and c['title']=='Lokaler Zyklusschritt' for c in children) for ext in local_external)
  store.close();print('Project assignment sync: initial, offline move, retry, incoming changes and conflict resolution OK')
 finally:process.terminate();process.wait(timeout=5)

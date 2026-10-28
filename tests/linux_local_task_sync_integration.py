#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks,inspect_task_conflict,resolve_task_conflict
import task_sync as task_sync_module
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-task-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  db=Path(tmp)/'local.db';store=LocalStore(db);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
  lock=threading.Lock();sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  admin_id=store.session_employee();sync=lambda:sync_tasks(store,lock,lambda:True,client,url,token,str(cert))
  assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_task_queue BEFORE INSERT ON linux_task_sync_outbox BEGIN SELECT RAISE(ABORT,'queue failure'); END")
  try:store.save_task(0,'Rollback','IT','Mittel',admin_id,0,0);raise AssertionError('task survived queue failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM tasks WHERE title='Rollback'").fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_task_queue')
  store.save_task(0,'Lokal','IT','Mittel',admin_id,1900000000,7)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_task_ack BEFORE DELETE ON linux_task_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync();raise AssertionError('failed acknowledgement accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_task_ack')
  assert sync()=={'sent':2,'received':0};remote=cli('-action','sync-tasks')[0];assert remote['title']=='Lokal' and remote['assigneeId']==9 and remote['dueAt']==1900000000
  local=next(v for v in store.tasks() if v['title']=='Lokal')
  process.terminate();process.wait(timeout=5)
  store.save_task(local['id'],'Lokal geändert','IT','Hoch',admin_id,1900100000,7)
  try:sync();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==2
  store.close();store.open();store.login('admin','Admin Passwort 2026');process=start();assert sync()=={'sent':2,'received':0}
  updated=next(v for v in cli('-action','sync-tasks') if v['id']==remote['id']);assert updated['title']=='Lokal geändert' and updated['dueAt']==1900100000
  central=cli('-action','create-task','-title','Zentral','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900200000','-recurrence','0')
  assert sync()=={'sent':0,'received':1};imported=next(v for v in store.tasks() if v['title']=='Zentral');assert imported['assigneeId']==admin_id and imported['dueAt']==1900200000
  cli('-action','update-task','-id',central['id'],'-title','Zentral neu','-department','Support','-priority','Hoch','-assignee-id','9','-due','1900300000','-recurrence','0')
  assert sync()=={'sent':0,'received':1};assert next(v for v in store.tasks() if v['id']==imported['id'])['dueAt']==1900300000
  process.terminate();process.wait(timeout=5);store.task_status(local['id'],'done');process=start()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_recurrence_ack BEFORE DELETE ON linux_task_sync_outbox WHEN json_extract(OLD.task_json,'$.status')='done' BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync();raise AssertionError('failed recurrence acknowledgement accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   frozen=json.loads(sql.execute("SELECT wire_json FROM linux_task_sync_outbox WHERE task_id=? ORDER BY seq LIMIT 1",(local['id'],)).fetchone()[0]);assert frozen['recurrenceSuccessorId']
   sql.execute('DROP TRIGGER fail_recurrence_ack')
  original_connect=task_sync_module.connect_sync_server
  def older_capability(*args):
   bound_url,bound_actor,command=original_connect(*args)
   def wrapped(*argv):
    result=command(*argv)
    if argv==('-action','capabilities'):return {**result,'taskRecurrenceReservation':0}
    return result
   return bound_url,bound_actor,wrapped
  task_sync_module.connect_sync_server=older_capability
  try:assert sync()=={'sent':3,'received':0}
  finally:task_sync_module.connect_sync_server=original_connect
  store.save_task(0,'Alter Versand','IT','Mittel',admin_id,1900500000,7);sync()
  old_task=next(v for v in store.tasks() if v['title']=='Alter Versand');store.task_status(old_task['id'],'done')
  with sqlite3.connect(db) as sql:
   client_id=sql.execute('SELECT client_id FROM linux_task_sync_config').fetchone()[0]
   seq,operation,action,payload,expected=sql.execute('SELECT seq,operation_id,action,task_json,expected_json FROM linux_task_sync_outbox WHERE task_id=? ORDER BY seq LIMIT 1',(old_task['id'],)).fetchone()
   def wire_task(raw):
    value=json.loads(raw);value['assigneeId']=9;value['createdBy']=9;return value
   legacy={'clientId':client_id,'operationId':operation,'action':action,'task':wire_task(payload),'expected':wire_task(expected)}
   sql.execute('UPDATE linux_task_sync_outbox SET wire_json=? WHERE seq=?',(json.dumps(legacy,ensure_ascii=False,separators=(',',':')),seq))
  def interrupted_after_reservation(*args):
   bound_url,bound_actor,command=original_connect(*args)
   def wrapped(*argv):
    if argv[:2]==('-action','sync-task-event') and json.loads(argv[3])['operationId']==operation:raise LocalStoreError('simulated interruption')
    return command(*argv)
   return bound_url,bound_actor,wrapped
  task_sync_module.connect_sync_server=interrupted_after_reservation
  try:
   try:sync();raise AssertionError('interrupted sync succeeded')
   except LocalStoreError:pass
  finally:task_sync_module.connect_sync_server=original_connect
  def competing_completion():
   result=subprocess.run([str(client),'-action','task-status','-id',legacy['task']['id'],'-status','done'],env=env,capture_output=True,text=True)
   assert result.returncode and '409' in result.stderr,result.stderr
  competing_completion()
  process.terminate();process.wait(timeout=5);process=start();competing_completion()
  sync();assert len([v for v in cli('-action','sync-tasks') if v['title']=='Alter Versand'])==2
  values=cli('-action','sync-tasks');assert len([v for v in values if v['title']=='Lokal geändert'])==2
  successor=next(v for v in values if v['title']=='Lokal geändert' and v['id']!=remote['id'])
  link={'clientId':'test-link','operationId':'one','sourceId':remote['id'],'successorId':successor['id']}
  assert cli('-action','task-recurrence-link','-payload',json.dumps(link))['accepted'];assert cli('-action','task-recurrence-link','-payload',json.dumps(link))['accepted']
  denied_env=env.copy();denied_env['AURA_SYNC_TOKEN']='other'
  denied=subprocess.run([str(client),'-action','task-recurrence-link','-payload',json.dumps({**link,'operationId':'denied'})],env=denied_env,capture_output=True,text=True);assert denied.returncode and '403' in denied.stderr
  changed=subprocess.run([str(client),'-action','task-recurrence-link','-payload',json.dumps({**link,'operationId':'different','successorId':central['id']})],env=env,capture_output=True,text=True);assert changed.returncode and '409' in changed.stderr
  process.terminate();process.wait(timeout=5);process=start()
  cli('-action','task-status','-id',remote['id'],'-status','open');cli('-action','task-status','-id',remote['id'],'-status','done')
  values=cli('-action','sync-tasks');assert len([v for v in values if v['title']=='Lokal geändert'])==2
  assert sync()=={'sent':0,'received':0}
  central_repeating=cli('-action','create-task','-title','Zentral wiederkehrend','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900400000','-recurrence','7')
  cli('-action','task-status','-id',central_repeating['id'],'-status','done')
  sync();central_local=next(v for v in store.tasks() if v['title']=='Zentral wiederkehrend' and v['status']=='done')
  assert len([v for v in store.tasks() if v['title']=='Zentral wiederkehrend'])==2
  store.close();store.open();store.login('admin','Admin Passwort 2026')
  store.task_status(central_local['id'],'open');store.task_status(central_local['id'],'done')
  assert len([v for v in store.tasks() if v['title']=='Zentral wiederkehrend'])==2
  sync();assert len([v for v in cli('-action','sync-tasks') if v['title']=='Zentral wiederkehrend'])==2
  store.save_task(0,'Mit Unteraufgabe','IT','Mittel',admin_id,0,0)
  parent=next(v for v in store.tasks() if v['title']=='Mit Unteraufgabe')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_subtask_audit BEFORE INSERT ON audit_log WHEN NEW.action LIKE 'task.subtask.%' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.save_subtask(parent['id'],0,'Nicht gespeichert','open',admin_id,0);raise AssertionError('subtask survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM task_subtasks WHERE title='Nicht gespeichert'").fetchone()[0]==0
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_subtask_audit')
  store.save_subtask(parent['id'],0,'Offline Schritt','open',admin_id,0)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_subtask_ack BEFORE DELETE ON linux_subtask_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync();raise AssertionError('failed subtask acknowledgement accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_subtask_ack')
  assert sync()=={'sent':1,'received':0}
  parent_remote=next(v for v in cli('-action','sync-tasks') if v['title']=='Mit Unteraufgabe')
  child=store.subtasks(parent['id'])[0];remote_children=cli('-action','sync-subtasks')
  assert any(v['taskId']==parent_remote['id'] and v['title']=='Offline Schritt' for v in remote_children)
  process.terminate();process.wait(timeout=5)
  store.save_subtask(parent['id'],child['id'],'Offline Schritt','done',admin_id,0);store.task_status(parent['id'],'done')
  try:sync();raise AssertionError('offline subtask sync succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':2,'received':0}
  assert next(v for v in cli('-action','sync-tasks') if v['id']==parent_remote['id'])['status']=='done'
  assert next(v for v in cli('-action','sync-subtasks') if v['taskId']==parent_remote['id'])['status']=='done'
  remote_parent=cli('-action','create-task','-title','Zentral mit Schritt','-department','IT','-priority','Mittel','-assignee-id','9')
  remote_child=cli('-action','subtask-save','-task-id',remote_parent['id'],'-title','Zentraler Schritt','-status','open','-assignee-id','9','-due','0')
  assert sync()=={'sent':0,'received':2};local_parent=next(v for v in store.tasks() if v['title']=='Zentral mit Schritt')
  local_child=store.subtasks(local_parent['id'])[0];assert local_child['title']=='Zentraler Schritt'
  cli('-action','subtask-save','-task-id',remote_parent['id'],'-id',remote_child['id'],'-title','Zentral bearbeitet','-status','done','-assignee-id','9','-due','0')
  assert sync()=={'sent':0,'received':1};assert store.subtasks(local_parent['id'])[0]['status']=='done'
  local_child=store.subtasks(local_parent['id'])[0]
  store.save_subtask(local_parent['id'],local_child['id'],local_child['title'],'done',0,0)
  assert sync()=={'sent':1,'received':0}
  assert next(v for v in cli('-action','sync-subtasks') if v['id']==remote_child['id'])['assigneeId']==0
  cli('-action','subtask-save','-task-id',remote_parent['id'],'-id',remote_child['id'],'-title','Zentral bearbeitet','-status','done','-assignee-id','9','-due','0')
  assert sync()=={'sent':0,'received':1} and store.subtasks(local_parent['id'])[0]['assigneeId']==admin_id
  cli('-action','subtask-archive','-task-id',remote_parent['id'],'-id',remote_child['id'])
  assert sync()=={'sent':0,'received':1} and not store.subtasks(local_parent['id'])
  blocked=cli('-action','create-task','-title','Serverabschluss','-department','IT','-priority','Mittel','-assignee-id','9')
  assert sync()=={'sent':0,'received':1};local_blocked=next(v for v in store.tasks() if v['title']=='Serverabschluss')
  store.save_subtask(local_blocked['id'],0,'Lokaler Schritt','open',admin_id,0)
  cli('-action','task-status','-id',blocked['id'],'-status','in_progress');cli('-action','task-status','-id',blocked['id'],'-status','done')
  try:sync();raise AssertionError('server completion ignored local subtask')
  except LocalStoreError as exc:assert '409' in str(exc)
  assert next(v for v in store.tasks() if v['id']==local_blocked['id'])['status']=='open'
  sub=store.subtasks(local_blocked['id'])[0];store.save_subtask(local_blocked['id'],sub['id'],'Lokaler Schritt','done',admin_id,0)
  assert sync()=={'sent':1,'received':1} and next(v for v in store.tasks() if v['id']==local_blocked['id'])['status']=='done'
  store.save_task(0,'Ohne Zuordnung','IT','Mittel',0,0,0)
  unassigned=next(v for v in store.tasks() if v['title']=='Ohne Zuordnung')
  store.save_task(0,'Unabhängig','IT','Mittel',admin_id,0,0)
  independent=next(v for v in store.tasks() if v['title']=='Unabhängig')
  store.save_task(independent['id'],'Unabhängig geändert','IT','Hoch',admin_id,0,0)
  store.save_subtask(unassigned['id'],0,'Wartender Schritt','open',admin_id,0)
  incoming=cli('-action','create-task','-title','Unabhängig empfangen','-department','IT','-priority','Mittel','-assignee-id','9')
  cli('-action','subtask-save','-task-id',incoming['id'],'-title','Zentraler Teil','-status','open','-assignee-id','9','-due','0')
  try:sync();raise AssertionError('unassigned task was sent')
  except LocalStoreError:pass
  assert any(v['title']=='Unabhängig geändert' and v['priority']=='Hoch' for v in cli('-action','sync-tasks'))
  imported_independent=next(v for v in store.tasks() if v['title']=='Unabhängig empfangen')
  assert any(v['title']=='Zentraler Teil' for v in store.subtasks(imported_independent['id']))
  with sqlite3.connect(db) as sql:assert all(v[0]==unassigned['id'] for v in sql.execute('SELECT task_id FROM linux_task_sync_outbox'))
  store.save_task(unassigned['id'],'Jetzt zugeordnet','IT','Mittel',admin_id,1900400000,0)
  assert sync()=={'sent':2,'received':0}
  assert any(v['title']=='Jetzt zugeordnet' and v['dueAt']==1900400000 for v in cli('-action','sync-tasks'))
  store.save_task(imported['id'],'Lokaler Konflikt','Support','Hoch',admin_id,1900300000,0)
  cli('-action','update-task','-id',central['id'],'-title','Server Konflikt','-department','Support','-priority','Hoch','-assignee-id','9','-due','1900300000','-recurrence','0')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==1
  assert next(v for v in store.tasks() if v['id']==imported['id'])['title']=='Lokaler Konflikt'
  inspect=lambda:inspect_task_conflict(store,lock,lambda:True,client,url,token,str(cert))
  resolve=lambda reviewed,server:resolve_task_conflict(store,lock,lambda:True,client,url,token,str(cert),reviewed,server)
  reviewed=inspect();assert reviewed['preview']['local']['title']=='Lokaler Konflikt' and reviewed['remote']['title']=='Server Konflikt'
  store.save_task(imported['id'],'Lokal später','Support','Hoch',admin_id,1900300000,0)
  try:resolve(reviewed,False);raise AssertionError('stale local preview accepted')
  except LocalStoreError:pass
  reviewed=inspect();cli('-action','update-task','-id',central['id'],'-title','Server später','-department','Support','-priority','Hoch','-assignee-id','9','-due','1900300000','-recurrence','0')
  try:resolve(reviewed,False);raise AssertionError('stale server preview accepted')
  except LocalStoreError:pass
  reviewed=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_task_resolution BEFORE INSERT ON audit_log WHEN NEW.action='tasks.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:resolve(reviewed,False);raise AssertionError('failed audit allowed resolution')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_task_resolution')
  resolve(reviewed,False);assert sync()=={'sent':1,'received':0}
  assert next(v for v in cli('-action','sync-tasks') if v['id']==central['id'])['title']=='Lokal später'
  store.save_task(imported['id'],'Lokaler Zweitstand','Support','Hoch',admin_id,1900300000,0)
  store.save_task(unassigned['id'],'Andere Änderung','IT','Mittel',admin_id,1900400000,0)
  cli('-action','update-task','-id',central['id'],'-title','Server Zweitstand','-department','Support','-priority','Hoch','-assignee-id','9','-due','1900300000','-recurrence','0')
  reviewed=inspect();resolve(reviewed,True)
  assert next(v for v in store.tasks() if v['id']==imported['id'])['title']=='Server Zweitstand'
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_task_sync_outbox').fetchone()[0]==1
   assert sql.execute('SELECT count(*) FROM linux_task_sync_resolutions').fetchone()[0]==2
  assert sync()=={'sent':1,'received':0}
  store.save_subtask(imported['id'],0,'Offener Prüfungsschritt','open',admin_id,0)
  store.save_task(imported['id'],'Lokaler Abschlusskonflikt','Support','Hoch',admin_id,1900300000,0)
  cli('-action','task-status','-id',central['id'],'-status','in_progress');cli('-action','task-status','-id',central['id'],'-status','done')
  reviewed=inspect()
  try:resolve(reviewed,True);raise AssertionError('server completion bypassed local subtask')
  except LocalStoreError as exc:assert 'Unteraufgaben' in str(exc)
  sub=next(v for v in store.subtasks(imported['id']) if v['title']=='Offener Prüfungsschritt');store.save_subtask(imported['id'],sub['id'],sub['title'],'done',admin_id,0)
  reviewed=inspect();resolve(reviewed,True);assert next(v for v in store.tasks() if v['id']==imported['id'])['status']=='done'
  store.close();print('Task sync: identity mapping, offline queue, schedules, recurrence, inbound and conflicts OK')
 finally:process.terminate();process.wait(timeout=5)

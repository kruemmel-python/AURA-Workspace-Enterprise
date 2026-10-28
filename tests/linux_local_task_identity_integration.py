#!/usr/bin/python3
"""Reconcile independently created tasks without replaying a prepared creation."""
import json,os,socket,ssl,subprocess,sys,tempfile,threading,time,urllib.request,sqlite3
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
import task_sync as task_sync_module
from task_sync import sync_tasks,inspect_task_conflict,resolve_task_conflict
from subtask_sync import inspect_subtask_conflict,resolve_subtask_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-task-identity-') as tmp:
 cert=Path(tmp)/'cert';key=Path(tmp)/'key'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='task-test'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 server_process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 try:
  for _ in range(100):
   if server_process.poll() is not None:raise RuntimeError(server_process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ssl.create_default_context(cafile=str(cert)),timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server startup failed')
  def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,text=True,capture_output=True,check=True).stdout)
  path=Path(tmp)/'local.sqlite';store=LocalStore(path);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock();current=lambda:True
  sync_employees(store,lock,current,client,url,token,str(cert));admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,current,client,url,token,str(cert))
  sync=lambda:sync_tasks(store,lock,current,client,url,token,str(cert));sync()
  def inspect():return inspect_task_conflict(store,lock,current,client,url,token,str(cert))
  def resolve(review,accept):return resolve_task_conflict(store,lock,current,client,url,token,str(cert),review,accept)
  remote=cli('-action','create-task','-title','Zuordnungstest','-department','IT','-priority','Mittel','-assignee-id','9')
  store.save_task(0,'Zuordnungstest','IT','Hoch',store.session_employee(),0,0)
  review=inspect();assert review['identityConflict'] and review['remote']['id']==remote['id']
  cli('-action','update-task','-id',remote['id'],'-title','Zuordnungstest','-department','Support','-priority','Mittel','-assignee-id','9')
  try:resolve(review,True);raise AssertionError('stale remote accepted')
  except LocalStoreError:pass
  review=inspect();resolve(review,True)
  task=next(v for v in store.tasks() if v['title']=='Zuordnungstest');assert task['department']=='Support' and task['priority']=='Mittel'
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(task['id'],)).fetchone()[0]==remote['id']
   assert db.execute('SELECT count(*) FROM linux_task_sync_outbox WHERE task_id=?',(task['id'],)).fetchone()[0]==0
  sync();assert len([t for t in cli('-action','sync-tasks') if t['title']=='Zuordnungstest'])==1
  remote2=cli('-action','create-task','-title','Lokal gewinnen','-department','IT','-priority','Mittel','-assignee-id','9')
  store.save_task(0,'Lokal gewinnen','Support','Hoch',store.session_employee(),0,0)
  review=inspect();assert review['identityConflict'];resolve(review,False);sync()
  assert next(t for t in cli('-action','sync-tasks') if t['id']==remote2['id'])['department']=='Support'
  frozen_remote=cli('-action','create-task','-title','Vorbereitet','-department','IT','-priority','Mittel','-assignee-id','9')
  store.save_task(0,'Vorbereitet','Support','Hoch',store.session_employee(),0,0)
  original_connect=task_sync_module.connect_sync_server
  def offline_before_send(*args):
   normalized,actor,command=original_connect(*args)
   def wrapped(*params):
    if params[0]=='-action' and params[1]=='sync-task-event':raise LocalStoreError('simulated network interruption')
    return command(*params)
   return normalized,actor,wrapped
  task_sync_module.connect_sync_server=offline_before_send
  try:
   try:sync();raise AssertionError('simulated interruption ignored')
   except LocalStoreError:pass
  finally:task_sync_module.connect_sync_server=original_connect
  reviewed=inspect();assert reviewed['identityConflict'] and reviewed.get('preparedOperation')
  original=reviewed['preparedOperation']
  with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_task_identity_audit BEFORE INSERT ON audit_log WHEN NEW.action='tasks.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve(reviewed,True);raise AssertionError('task identity audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==reviewed['preview']['local']['id']
   db.execute('DROP TRIGGER fail_task_identity_audit')
  resolve(reviewed,True)
  rejected=subprocess.run([str(client),'-action','sync-task-event','-payload',json.dumps(original)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  server_process.terminate();server_process.wait(timeout=5);server_process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if server_process.poll() is not None:raise RuntimeError(server_process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ssl.create_default_context(cafile=str(cert)),timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server restart failed')
  rejected=subprocess.run([str(client),'-action','sync-task-event','-payload',json.dumps(original)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  assert len([v for v in cli('-action','sync-tasks') if v['title']=='Vorbereitet'])==1
  accepted=json.loads(json.dumps(original));accepted['operationId']='accepted-operation';accepted['task']['id']=accepted['clientId']+':task:98765';accepted['task']['title']='Schon bestätigt'
  cli('-action','sync-task-event','-payload',json.dumps(accepted))
  separate=cli('-action','create-task','-title','Schon bestätigt','-department','IT','-priority','Mittel','-assignee-id','9')
  refused=subprocess.run([str(client),'-action','abandon-task-event','-payload',json.dumps({'operation':accepted,'remote':separate})],env=env,text=True,capture_output=True)
  assert refused.returncode and '409' in refused.stderr

  parent=next(v for v in store.tasks() if v['title']=='Lokal gewinnen')
  sub=cli('-action','subtask-save','-task-id',remote2['id'],'-title','Teil prüfen','-status','open','-assignee-id','9','-due','0')
  store.save_subtask(parent['id'],0,'Teil prüfen','done',store.session_employee(),0)
  inspect_sub=lambda:inspect_subtask_conflict(store,lock,current,client,url,token,str(cert))
  resolve_sub=lambda reviewed,accept:resolve_subtask_conflict(store,lock,current,client,url,token,str(cert),reviewed,accept)
  reviewed=inspect_sub();assert reviewed['identityConflict'] and reviewed['remote']['id']==sub['id']
  cli('-action','subtask-save','-task-id',remote2['id'],'-id',sub['id'],'-title','Teil prüfen','-status','in_progress','-assignee-id','9','-due','0')
  try:resolve_sub(reviewed,True);raise AssertionError('stale subtask review accepted')
  except LocalStoreError:pass
  reviewed=inspect_sub()
  with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_identity_audit BEFORE INSERT ON audit_log WHEN NEW.action='subtasks.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve_sub(reviewed,True);raise AssertionError('identity audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='subtask' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==reviewed['preview']['local']['id']
   db.execute('DROP TRIGGER fail_identity_audit')
  resolve_sub(reviewed,True)
  local_sub=next(v for v in store.subtasks(parent['id']) if v['title']=='Teil prüfen');assert local_sub['status']=='in_progress'
  with sqlite3.connect(path) as db:assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='subtask' AND local_id=?",(local_sub['id'],)).fetchone()[0]==sub['id']
  sub2=cli('-action','subtask-save','-task-id',remote2['id'],'-title','Teil lokal','-status','open','-assignee-id','9','-due','0')
  store.save_subtask(parent['id'],0,'Teil lokal','done',store.session_employee(),0)
  reviewed=inspect_sub();assert reviewed['identityConflict'];resolve_sub(reviewed,False);sync()
  assert next(v for v in cli('-action','sync-subtasks') if v['id']==sub2['id'])['status']=='done'
  frozen_sub=cli('-action','subtask-save','-task-id',remote2['id'],'-title','Vorbereiteter Schritt','-status','open','-assignee-id','9','-due','0')
  store.save_subtask(parent['id'],0,'Vorbereiteter Schritt','done',store.session_employee(),0)
  def offline_subtask_before_send(*args):
   normalized,actor,command=original_connect(*args)
   def wrapped(*params):
    if params[0]=='-action' and params[1]=='sync-subtask-event':raise LocalStoreError('simulated subtask network interruption')
    return command(*params)
   return normalized,actor,wrapped
  task_sync_module.connect_sync_server=offline_subtask_before_send
  try:
   try:sync();raise AssertionError('subtask interruption ignored')
   except LocalStoreError:pass
  finally:task_sync_module.connect_sync_server=original_connect
  reviewed=inspect_sub();assert reviewed['identityConflict'] and reviewed.get('preparedOperation')
  original_sub=reviewed['preparedOperation']
  wrong_parent=cli('-action','subtask-save','-task-id',remote['id'],'-title','Vorbereiteter Schritt','-status','open','-assignee-id','9','-due','0')
  refused=subprocess.run([str(client),'-action','abandon-subtask-event','-payload',json.dumps({'operation':original_sub,'remote':wrong_parent})],env=env,text=True,capture_output=True)
  assert refused.returncode and '400' in refused.stderr
  with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_frozen_subtask_audit BEFORE INSERT ON audit_log WHEN NEW.action='subtasks.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve_sub(reviewed,True);raise AssertionError('frozen subtask audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='subtask' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==reviewed['preview']['local']['id']
   db.execute('DROP TRIGGER fail_frozen_subtask_audit')
  resolve_sub(reviewed,True)
  rejected=subprocess.run([str(client),'-action','sync-subtask-event','-payload',json.dumps(original_sub)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  server_process.terminate();server_process.wait(timeout=5);server_process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if server_process.poll() is not None:raise RuntimeError(server_process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ssl.create_default_context(cafile=str(cert)),timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server restart failed')
  rejected=subprocess.run([str(client),'-action','sync-subtask-event','-payload',json.dumps(original_sub)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  assert len([v for v in cli('-action','sync-subtasks') if v['title']=='Vorbereiteter Schritt' and v['taskId']==remote2['id']])==1
  accepted_sub=json.loads(json.dumps(original_sub));accepted_sub['operationId']='accepted-subtask-operation';accepted_sub['subtask']['id']=accepted_sub['clientId']+':subtask:98765';accepted_sub['subtask']['title']='Schon bestätigter Schritt'
  cli('-action','sync-subtask-event','-payload',json.dumps(accepted_sub))
  separate=cli('-action','subtask-save','-task-id',remote2['id'],'-title','Schon bestätigter Schritt','-status','open','-assignee-id','9','-due','0')
  refused=subprocess.run([str(client),'-action','abandon-subtask-event','-payload',json.dumps({'operation':accepted_sub,'remote':separate})],env=env,text=True,capture_output=True)
  assert refused.returncode and '409' in refused.stderr
  cli('-action','subtask-save','-task-id',remote['id'],'-title','Anderes Elternteil','-status','open','-assignee-id','9','-due','0')
  store.save_subtask(parent['id'],0,'Anderes Elternteil','open',store.session_employee(),0)
  try:inspect_sub();raise AssertionError('wrong parent accepted')
  except LocalStoreError:pass
  for _ in range(2):cli('-action','subtask-save','-task-id',remote2['id'],'-title','Anderes Elternteil','-status','open','-assignee-id','9','-due','0')
  try:inspect_sub();raise AssertionError('ambiguous subtasks accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:db.execute("UPDATE linux_subtask_sync_outbox SET wire_json='{}' WHERE seq=(SELECT min(seq) FROM linux_subtask_sync_outbox)")
  try:inspect_sub();raise AssertionError('prepared subtask accepted')
  except LocalStoreError:pass
  cli('-action','create-task','-title','Doppelt','-department','IT','-priority','Mittel','-assignee-id','9')
  cli('-action','create-task','-title','Doppelt','-department','IT','-priority','Mittel','-assignee-id','9')
  store.save_task(0,'Doppelt','IT','Mittel',store.session_employee(),0,0)
  try:inspect();raise AssertionError('ambiguous match accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:db.execute("UPDATE linux_task_sync_outbox SET wire_json='{}' WHERE seq=(SELECT min(seq) FROM linux_task_sync_outbox)")
  try:inspect();raise AssertionError('prepared create accepted')
  except LocalStoreError:pass
  store.close()
 finally:
  server_process.terminate();server_process.wait(timeout=5)
print('Independent task mapping, stale review, server and local selection: OK')

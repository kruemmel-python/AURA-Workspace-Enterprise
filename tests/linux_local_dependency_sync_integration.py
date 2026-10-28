#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from dependency_sync import inspect_dependency_conflict,resolve_dependency_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-dependency-sync-') as tmp:
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
  lock=threading.Lock();sync_employees(store,lock,lambda:True,client,url,token,str(cert));admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  actor=store.session_employee();sync=lambda:sync_tasks(store,lock,lambda:True,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  store.save_task(0,'A','IT','Mittel',actor);store.save_task(0,'B','IT','Mittel',actor)
  a=next(v for v in store.tasks() if v['title']=='A');b=next(v for v in store.tasks() if v['title']=='B')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_dep_audit BEFORE INSERT ON audit_log WHEN NEW.action='task.dependency.added' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.dependency_change(a['id'],b['id'],True);raise AssertionError('dependency survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM task_dependencies').fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_dep_audit')
  store.dependency_change(a['id'],b['id'],True)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_dep_ack BEFORE DELETE ON linux_dependency_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync();raise AssertionError('failed acknowledgement accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_dep_ack')
  assert sync()=={'sent':1,'received':0}
  remote=cli('-action','sync-tasks');ra=next(v for v in remote if v['title']=='A');rb=next(v for v in remote if v['title']=='B')
  assert cli('-action','sync-dependencies')==[{'taskId':ra['id'],'dependencyId':rb['id']}]
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_dep_remove_audit BEFORE INSERT ON audit_log WHEN NEW.action='task.dependency.removed' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.dependency_change(a['id'],b['id'],False);raise AssertionError('removal survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM task_dependencies').fetchone()[0]==1 and sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_dep_remove_audit')
  process.terminate();process.wait(timeout=5);store.dependency_change(a['id'],b['id'],False);store.task_status(a['id'],'done')
  try:sync();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':2,'received':0};assert cli('-action','sync-dependencies')==[]
  assert next(v for v in cli('-action','sync-tasks') if v['id']==ra['id'])['status']=='done'
  c=cli('-action','create-task','-title','C','-department','IT','-priority','Mittel','-assignee-id','9');d=cli('-action','create-task','-title','D','-department','IT','-priority','Mittel','-assignee-id','9')
  cli('-action','dependency-add','-task-id',c['id'],'-dependency-id',d['id']);assert sync()=={'sent':0,'received':3}
  lc=next(v for v in store.tasks() if v['title']=='C');ld=next(v for v in store.tasks() if v['title']=='D');assert store.dependencies(lc['id'])[0]['id']==ld['id']
  cli('-action','dependency-remove','-task-id',c['id'],'-dependency-id',d['id']);cli('-action','dependency-add','-task-id',d['id'],'-dependency-id',c['id'])
  assert sync()=={'sent':0,'received':2};assert not store.dependencies(lc['id']) and store.dependencies(ld['id'])[0]['id']==lc['id']
  cli('-action','dependency-remove','-task-id',d['id'],'-dependency-id',c['id']);assert sync()=={'sent':0,'received':1};assert not store.dependencies(ld['id'])
  store.dependency_change(lc['id'],ld['id'],True);cli('-action','dependency-add','-task-id',d['id'],'-dependency-id',c['id'])
  try:sync();raise AssertionError('server cycle accepted')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==1
  inspect=lambda:inspect_dependency_conflict(store,lock,lambda:True,client,url,token,str(cert))
  resolve=lambda reviewed,server:resolve_dependency_conflict(store,lock,lambda:True,client,url,token,str(cert),reviewed,server)
  reviewed=inspect();assert reviewed['preview']['localPresent'] and not reviewed['remote']['present'] and reviewed['remote']['wouldCycle']
  try:resolve(reviewed,False);raise AssertionError('server cycle was accepted')
  except LocalStoreError as exc:assert 'Zyklus' in str(exc)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_dep_accept BEFORE INSERT ON audit_log WHEN NEW.action='dependencies.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve(reviewed,True);raise AssertionError('failed audit allowed server choice')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==1
   assert sql.execute('SELECT count(*) FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(lc['id'],ld['id'])).fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_dep_accept')
  resolve(reviewed,True);assert not store.dependencies(lc['id']);assert sync()=={'sent':0,'received':1}
  assert store.dependencies(ld['id'])[0]['id']==lc['id']
  cli('-action','dependency-remove','-task-id',d['id'],'-dependency-id',c['id']);assert sync()=={'sent':0,'received':1}
  store.dependency_change(lc['id'],ld['id'],True);cli('-action','dependency-add','-task-id',c['id'],'-dependency-id',d['id'])
  try:sync();raise AssertionError('independent edge collision accepted without review')
  except LocalStoreError as exc:assert '409' in str(exc)
  reviewed=inspect();assert reviewed['preview']['localPresent'] and reviewed['remote']['present']
  store.dependency_change(lc['id'],ld['id'],False);store.dependency_change(lc['id'],ld['id'],True)
  try:resolve(reviewed,False);raise AssertionError('stale local preview accepted')
  except LocalStoreError:pass
  reviewed=inspect();cli('-action','dependency-remove','-task-id',c['id'],'-dependency-id',d['id'])
  try:resolve(reviewed,False);raise AssertionError('stale server preview accepted')
  except LocalStoreError:pass
  cli('-action','dependency-add','-task-id',c['id'],'-dependency-id',d['id']);reviewed=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_dep_decision BEFORE INSERT ON audit_log WHEN NEW.action='dependencies.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve(reviewed,False);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_dependency_sync_outbox').fetchone()[0]==3
   sql.execute('DROP TRIGGER fail_dep_decision')
  resolve(reviewed,False);assert sync()=={'sent':0,'received':0}
  store.dependency_change(lc['id'],ld['id'],False);store.task_status(lc['id'],'done')
  reviewed=inspect();assert not reviewed['preview']['localPresent'] and reviewed['remote']['present']
  try:resolve(reviewed,True);raise AssertionError('open prerequisite accepted for completed local task')
  except LocalStoreError as exc:assert 'abgeschlossenen Aufgabe' in str(exc)
  resolve(reviewed,False);assert sync()=={'sent':2,'received':0}
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_dependency_sync_conflicts').fetchone()[0]==3
  store.close();print('Dependency sync: audit, offline ordering, inbound, removals, cycles and persistence OK')
 finally:process.terminate();process.wait(timeout=5)

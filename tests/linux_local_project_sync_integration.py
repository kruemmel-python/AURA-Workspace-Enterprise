#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from project_sync import sync_projects,inspect_project_conflict,resolve_project_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-project-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  actor=store.session_employee();sync=lambda:sync_projects(store,lock,lambda:True,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_project_audit BEFORE INSERT ON audit_log WHEN NEW.action='project.created' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.save_project(0,'Rollback','IT','','planned',actor);raise AssertionError('project survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM projects WHERE name='Rollback'").fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_project_audit')
  store.save_project(0,'Lokal','IT','Beschreibung','planned',actor)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_project_ack BEFORE DELETE ON linux_project_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_project_ack')
  assert sync()=={'sent':1,'received':0};remote=cli('-action','sync-projects')[0];assert remote['name']=='Lokal' and remote['ownerId']==9
  local=next(v for v in store.projects() if v['name']=='Lokal')
  process.terminate();process.wait(timeout=5);store.save_project(local['id'],'Lokal neu','IT','Offline','active',actor)
  try:sync();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  store.close();store.open();store.login('admin','Admin Passwort 2026');process=start();assert sync()=={'sent':1,'received':0}
  assert next(v for v in cli('-action','sync-projects') if v['id']==remote['id'])['description']=='Offline'
  # The direct status shortcut must enter the same durable project outbox.
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_project_status_audit BEFORE INSERT ON audit_log WHEN NEW.action='project.status.changed' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:store.project_status(local['id'],'active','on_hold');raise AssertionError('status survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM projects WHERE id=?',(local['id'],)).fetchone()[0]=='active'
   assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_project_status_audit')
  process.terminate();process.wait(timeout=5);store.project_status(local['id'],'active','on_hold')
  try:sync();raise AssertionError('offline status sync succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==1
  store.close();store.open();store.login('admin','Admin Passwort 2026')
  process=start();assert sync()=={'sent':1,'received':0}
  assert next(v for v in cli('-action','sync-projects') if v['id']==remote['id'])['status']=='on_hold'
  cli('-action','project-status','-id',remote['id'],'-expected-status','on_hold','-status','completed')
  assert sync()=={'sent':0,'received':1}
  assert next(v for v in store.projects() if v['id']==local['id'])['status']=='completed'
  store.project_status(local['id'],'completed','active');assert sync()=={'sent':1,'received':0}
  central=cli('-action','create-project','-name','Zentral','-department','IT','-owner-id','9','-description','X')
  assert sync()=={'sent':0,'received':1};imported=next(v for v in store.projects() if v['name']=='Zentral');assert imported['ownerId']==actor
  cli('-action','update-project','-id',central['id'],'-name','Zentral neu','-department','Support','-owner-id','9','-description','Y','-status','active')
  assert sync()=={'sent':0,'received':1};assert next(v for v in store.projects() if v['id']==imported['id'])['name']=='Zentral neu'
  store.save_project(0,'Ohne Leitung','IT','','planned',0)
  unassigned=next(v for v in store.projects() if v['name']=='Ohne Leitung')
  store.save_project(0,'Unabhängig','IT','Eins','planned',actor)
  independent=next(v for v in store.projects() if v['name']=='Unabhängig')
  store.save_project(independent['id'],'Unabhängig geändert','IT','Zwei','active',actor)
  cli('-action','create-project','-name','Unabhängig empfangen','-department','IT','-owner-id','9','-description','Server')
  try:sync();raise AssertionError('unassigned project synced')
  except LocalStoreError:pass
  assert any(v['name']=='Unabhängig geändert' and v['description']=='Zwei' for v in cli('-action','sync-projects'))
  assert any(v['name']=='Unabhängig empfangen' for v in store.projects())
  with sqlite3.connect(db) as sql:assert all(v[0]==unassigned['id'] for v in sql.execute('SELECT project_id FROM linux_project_sync_outbox'))
  store.save_project(unassigned['id'],'Mit Leitung','IT','','planned',actor);assert sync()=={'sent':1,'received':0}
  store.archive_project(unassigned['id']);assert sync()=={'sent':1,'received':0};assert not next(v for v in cli('-action','sync-projects') if v['name']=='Mit Leitung')['active']
  store.save_task(0,'Projektaufgabe','IT','Mittel',actor);member=next(v for v in store.tasks() if v['title']=='Projektaufgabe');store.assign_project(member['id'],imported['id'])
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_project_changes BEFORE INSERT ON audit_log WHEN NEW.action IN ('project.updated','project.archived') BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  for action in (lambda:store.save_project(imported['id'],'Fehler','IT','','active',actor),lambda:store.archive_project(imported['id'])):
   try:action();raise AssertionError('project change survived audit failure')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT name,active FROM projects WHERE id=?',(imported['id'],)).fetchone()==('Zentral neu',1)
   assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_project_changes')
  store.save_project(imported['id'],'Lokal Konflikt','IT','L','active',actor)
  cli('-action','update-project','-id',central['id'],'-name','Server Konflikt','-department','IT','-owner-id','9','-description','S','-status','active')
  try:sync();raise AssertionError('project conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==1
  assert next(v for v in store.projects() if v['id']==imported['id'])['name']=='Lokal Konflikt'
  inspect=lambda:inspect_project_conflict(store,lock,lambda:True,client,url,token,str(cert))
  resolve=lambda reviewed,server:resolve_project_conflict(store,lock,lambda:True,client,url,token,str(cert),reviewed,server)
  reviewed=inspect();assert reviewed['preview']['local']['name']=='Lokal Konflikt' and reviewed['remote']['name']=='Server Konflikt'
  store.save_project(imported['id'],'Lokal später','IT','L','active',actor)
  try:resolve(reviewed,False);raise AssertionError('stale local preview accepted')
  except LocalStoreError:pass
  reviewed=inspect();cli('-action','update-project','-id',central['id'],'-name','Server später','-department','IT','-owner-id','9','-description','S','-status','active')
  try:resolve(reviewed,False);raise AssertionError('stale server preview accepted')
  except LocalStoreError:pass
  reviewed=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_project_resolution BEFORE INSERT ON audit_log WHEN NEW.action='projects.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:resolve(reviewed,False);raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_project_resolution')
  resolve(reviewed,False);assert sync()=={'sent':1,'received':0};assert next(v for v in cli('-action','sync-projects') if v['id']==central['id'])['name']=='Lokal später'
  store.save_project(imported['id'],'Lokal Zweit','IT','L','active',actor);store.save_project(local['id'],'Unabhängig','IT','X','active',actor)
  cli('-action','update-project','-id',central['id'],'-name','Server Zweit','-department','IT','-owner-id','9','-description','S','-status','active')
  reviewed=inspect();resolve(reviewed,True);assert next(v for v in store.projects() if v['id']==imported['id'])['name']=='Server Zweit'
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_project_sync_outbox').fetchone()[0]==1
   assert sql.execute('SELECT count(*) FROM linux_project_sync_conflicts').fetchone()[0]==2
  assert sync()=={'sent':1,'received':0}
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT project_id FROM project_tasks WHERE task_id=?',(member['id'],)).fetchone()==(imported['id'],)
  store.close();print('Project sync: audit rollback, offline queue, identity, archival and conflicts OK')
 finally:process.terminate();process.wait(timeout=5)

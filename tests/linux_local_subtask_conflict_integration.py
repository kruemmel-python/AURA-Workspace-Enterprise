#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from subtask_sync import inspect_subtask_conflict,resolve_subtask_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-subtask-conflict-') as tmp:
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
  actor=store.session_employee();store.save_task(0,'Parent','IT','Mittel',actor);parent=next(v for v in store.tasks() if v['title']=='Parent');store.save_subtask(parent['id'],0,'Schritt','open',actor,0)
  sync=lambda:sync_tasks(store,lock,lambda:True,client,url,token,str(cert));assert sync()=={'sent':2,'received':0}
  task=next(v for v in cli('-action','sync-tasks') if v['title']=='Parent');remote=next(v for v in cli('-action','sync-subtasks') if v['taskId']==task['id']);child=store.subtasks(parent['id'])[0]
  try:store.save_subtask_compared(parent['id'],child['id'],'Veraltet','done',actor,0,{**child,'title':'anderer Ausgangsstand'});raise AssertionError('stale editor accepted')
  except LocalStoreError:pass
  assert store.subtasks(parent['id'])[0]['title']=='Schritt'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==0
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_compared_subtask_queue BEFORE INSERT ON linux_subtask_sync_outbox BEGIN SELECT RAISE(ABORT,'queue failure'); END")
  try:store.save_subtask_compared(parent['id'],child['id'],'Nicht speichern','done',actor,0,child);raise AssertionError('failed outbox accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT title,status FROM task_subtasks WHERE id=?',(child['id'],)).fetchone()==('Schritt','open')
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_compared_subtask_queue')
   sql.execute("CREATE TRIGGER fail_compared_subtask_audit BEFORE INSERT ON audit_log WHEN NEW.action='task.subtask.updated' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:store.save_subtask_compared(parent['id'],child['id'],'Nicht speichern','done',actor,0,child);raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT title,status FROM task_subtasks WHERE id=?',(child['id'],)).fetchone()==('Schritt','open')
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_compared_subtask_audit')
  store.save_subtask_compared(parent['id'],child['id'],'Mit Vergleich','open',actor,0,child)
  assert sync()=={'sent':1,'received':0}
  assert next(v for v in cli('-action','sync-subtasks') if v['id']==remote['id'])['title']=='Mit Vergleich'
  def central(title,status='open'):
   return cli('-action','subtask-save','-task-id',task['id'],'-id',remote['id'],'-title',title,'-status',status,'-assignee-id','9','-due','0')
  inspect=lambda:inspect_subtask_conflict(store,lock,lambda:True,client,url,token,str(cert))
  resolve=lambda v,server:resolve_subtask_conflict(store,lock,lambda:True,client,url,token,str(cert),v,server)
  store.save_subtask(parent['id'],child['id'],'Lokal','open',actor,0);central('Server')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  reviewed=inspect();assert reviewed['preview']['local']['title']=='Lokal' and reviewed['remote']['title']=='Server'
  store.save_subtask(parent['id'],child['id'],'Lokal später','open',actor,0)
  try:resolve(reviewed,False);raise AssertionError('stale local preview accepted')
  except LocalStoreError:pass
  reviewed=inspect();central('Server später')
  try:resolve(reviewed,False);raise AssertionError('stale remote preview accepted')
  except LocalStoreError:pass
  reviewed=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_subtask_decision BEFORE INSERT ON audit_log WHEN NEW.action='subtasks.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve(reviewed,False);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_subtask_decision')
  resolve(reviewed,False);assert sync()=={'sent':1,'received':0};assert next(v for v in cli('-action','sync-subtasks') if v['id']==remote['id'])['title']=='Lokal später'
  store.save_subtask(parent['id'],child['id'],'Lokal Zwei','open',actor,0);store.save_subtask(parent['id'],0,'Unabhängig','open',actor,0);central('Server Zwei')
  reviewed=inspect();resolve(reviewed,True);assert next(v for v in store.subtasks(parent['id']) if v['id']==child['id'])['title']=='Server Zwei'
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_outbox').fetchone()[0]==1
   assert sql.execute('SELECT count(*) FROM linux_subtask_sync_conflicts').fetchone()[0]==2
  assert sync()=={'sent':1,'received':0}
  store.save_task(0,'Sperrtest','IT','Mittel',actor);block=next(v for v in store.tasks() if v['title']=='Sperrtest');store.save_subtask(block['id'],0,'Sperrschritt','open',actor,0);assert sync()=={'sent':2,'received':0}
  block_server=next(v for v in cli('-action','sync-tasks') if v['title']=='Sperrtest');child_server=next(v for v in cli('-action','sync-subtasks') if v['taskId']==block_server['id']);local_child=store.subtasks(block['id'])[0]
  store.save_subtask(block['id'],local_child['id'],'Sperrschritt','done',actor,0);store.task_status(block['id'],'done')
  cli('-action','subtask-save','-task-id',block_server['id'],'-id',child_server['id'],'-title','Fremd offen','-status','open','-assignee-id','9','-due','0')
  reviewed=inspect()
  try:resolve(reviewed,True);raise AssertionError('open subtask accepted for completed local parent')
  except LocalStoreError as exc:assert 'abgeschlossene Hauptaufgabe' in str(exc)
  assert store.subtasks(block['id'])[0]['status']=='done'
  reviewed=inspect();resolve(reviewed,False);assert sync()=={'sent':2,'received':0}
  store.close();print('Subtask conflicts: stale reviews, audit rollback, local/server choices and independent queue OK')
 finally:process.terminate();process.wait(timeout=5)

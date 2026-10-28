#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from ticket_sync import sync_tickets,inspect_ticket_conflict,resolve_ticket_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-ticket-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage','tickets.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  sync=lambda:sync_tickets(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_ticket_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('ticket.created','ticket.updated','ticket.status.changed','ticket.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.save_ticket(0,'Rollback','IT','Mittel',actor);raise AssertionError('failed audit persisted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM tickets').fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_ticket_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_ticket_audit')
  store.save_ticket(0,'Lokal','IT','Mittel',actor);ticket=store.tickets()[0]['id']
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_ticket_ack BEFORE DELETE ON linux_ticket_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_ticket_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_ticket_ack')
  assert sync()=={'sent':1,'received':0};remote=cli('-action','sync-tickets')[0];assert remote['title']=='Lokal' and remote['assigneeId']==9 and remote['createdBy']==9
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_ticket_change_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('ticket.updated','ticket.status.changed','ticket.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  for action in (lambda:store.save_ticket(ticket,'Fehler','IT','Mittel',actor),lambda:store.ticket_status(ticket,'in_progress'),lambda:store.archive_ticket(ticket)):
   try:action();raise AssertionError('ticket change survived audit failure')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT title,status,active FROM tickets WHERE id=?',(ticket,)).fetchone()==('Lokal','open',1)
   assert sql.execute('SELECT count(*) FROM linux_ticket_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_ticket_change_audit')
  process.terminate();process.wait(timeout=5);store.save_ticket(ticket,'Offline','IT','Hoch',actor)
  try:sync();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':1,'received':0};assert cli('-action','sync-tickets')[0]['title']=='Offline'
  central=cli('-action','create-ticket','-title','Zentral','-category','IT','-priority','Mittel','-assignee-id','9')
  assert sync()=={'sent':0,'received':1};imported=next(x for x in store.tickets() if x['title']=='Zentral');assert imported['assigneeId']==actor
  # Older Windows databases can contain unassigned tickets, although the
  # current Linux editor requires an assignee for newly created tickets.
  with sqlite3.connect(db) as sql:
   legacy=sql.execute("INSERT INTO tickets(title,category,priority,status,assignee_id,active) VALUES('Ohne Person','IT','Mittel','open',0,1)").lastrowid
  store.save_ticket(0,'Unabhängig','IT','Mittel',actor);independent=next(x for x in store.tickets() if x['title']=='Unabhängig')
  store.save_ticket(independent['id'],'Unabhängig geändert','IT','Hoch',actor)
  cli('-action','create-ticket','-title','Unabhängig empfangen','-category','IT','-priority','Mittel','-assignee-id','9')
  try:sync();raise AssertionError('unassigned legacy ticket accepted')
  except LocalStoreError:pass
  assert any(x['title']=='Unabhängig geändert' for x in cli('-action','sync-tickets'))
  assert any(x['title']=='Unabhängig empfangen' for x in store.tickets())
  with sqlite3.connect(db) as sql:assert all(row[0]==legacy for row in sql.execute('SELECT ticket_id FROM linux_ticket_sync_outbox'))
  store.save_ticket(legacy,'Nun zugeordnet','IT','Mittel',actor);assert sync()=={'sent':1,'received':0}
  store.save_ticket(ticket,'Lokal Konflikt','IT','Hoch',actor);cli('-action','update-ticket','-id',remote['id'],'-title','Server Konflikt','-category','IT','-priority','Mittel','-assignee-id','9')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  inspect=lambda:inspect_ticket_conflict(store,lock,current,client,url,token,str(cert));resolve=lambda r,server:resolve_ticket_conflict(store,lock,current,client,url,token,str(cert),r,server)
  reviewed=inspect();assert reviewed['remote']['title']=='Server Konflikt';store.save_ticket(ticket,'Lokal neu','IT','Hoch',actor)
  try:resolve(reviewed,False);raise AssertionError('stale preview accepted')
  except LocalStoreError:pass
  reviewed=inspect();resolve(reviewed,False);assert sync()=={'sent':1,'received':0};assert next(x for x in cli('-action','sync-tickets') if x['id']==remote['id'])['title']=='Lokal neu'
  store.save_ticket(ticket,'Lokaler zweiter Konflikt','IT','Hoch',actor);cli('-action','update-ticket','-id',remote['id'],'-title','Server zweiter Konflikt','-category','IT','-priority','Mittel','-assignee-id','9')
  resolve(inspect(),True);assert next(x for x in store.tickets() if x['id']==ticket)['title']=='Server zweiter Konflikt'
  store.ticket_status(ticket,'in_progress');store.ticket_status(ticket,'resolved');assert sync()=={'sent':2,'received':0};store.archive_ticket(ticket);assert sync()=={'sent':1,'received':0};assert not next(x for x in cli('-action','sync-tickets') if x['id']==remote['id'])['active']
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_ticket_sync_conflicts').fetchone()[0]==2
  store.close();print('Ticket sync: rollback, retry, offline, import, conflict decisions and archive OK')
 finally:process.terminate();process.wait(timeout=5)

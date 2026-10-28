#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from ticket_sync import sync_tickets
from ticket_comment_sync import sync_ticket_comments
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-ticket-comment-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tickets.manage','tasks.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  store.save_ticket(0,'Kommentarticket','IT','Mittel',actor);ticket=store.tickets()[0]['id'];store.add_comment('ticket',ticket,'Erster Kommentar')
  sync_tickets(store,lock,current,client,url,token,str(cert));sync=lambda:sync_ticket_comments(store,lock,current,client,url,token,str(cert))
  assert sync()=={'sent':1,'received':0};remote_ticket=cli('-action','sync-tickets')[0]['id'];comments=cli('-action','sync-ticket-comments');assert len(comments)==1 and comments[0]['body']=='Erster Kommentar' and comments[0]['authorId']==admin['id'] and comments[0]['importedBy']==9
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_comment_audit BEFORE INSERT ON audit_log WHEN NEW.action='ticket.comment.created' BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.add_comment('ticket',ticket,'Rollback');raise AssertionError('comment survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM comments WHERE body='Rollback'").fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_ticket_comment_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_comment_audit')
  process.terminate();process.wait(timeout=5);store.add_comment('ticket',ticket,'Offline')
  # A previously prepared numeric-author operation remains retryable after the
  # client switches to shared employee identifiers.
  from ticket_comment_sync import _timestamp
  with sqlite3.connect(db) as sql:
   seq,operation,comment_id=sql.execute('SELECT seq,operation_id,comment_id FROM linux_ticket_comment_sync_outbox').fetchone()
   created=sql.execute('SELECT created_at FROM comments WHERE id=?',(comment_id,)).fetchone()[0]
   external=sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='comment' AND local_id=?",(comment_id,)).fetchone()[0]
   client_id=sql.execute('SELECT client_id FROM linux_ticket_comment_sync_config').fetchone()[0]
   old_wire={'clientId':client_id,'operationId':operation,'comment':{'id':external,'subjectType':'ticket','subjectId':remote_ticket,'authorId':'9','body':'Offline','createdAt':_timestamp(created)}}
   sql.execute('UPDATE linux_ticket_comment_sync_outbox SET wire_json=? WHERE seq=?',(json.dumps(old_wire,ensure_ascii=False,separators=(',',':')),seq))
  try:sync();raise AssertionError('offline succeeded')
  except LocalStoreError:pass
  process=start()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_comment_ack BEFORE DELETE ON linux_ticket_comment_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('ack failure succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_ticket_comment_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_comment_ack')
  assert sync()=={'sent':1,'received':0};assert len(cli('-action','sync-ticket-comments'))==2;assert next(x for x in cli('-action','sync-ticket-comments') if x['body']=='Offline')['authorId']=='9'
  cli('-action','add-comment','-subject-type','ticket','-subject-id',remote_ticket,'-body','Zentral')
  assert sync()=={'sent':0,'received':1};assert {x['body'] for x in store.comments('ticket',ticket)}=={'Erster Kommentar','Offline','Zentral'}
  assert sync()=={'sent':0,'received':0}
  # A separate local database imports the same ticket and comments, then writes
  # as a different mapped author while the administrator runs synchronization.
  peer=LocalStore(Path(tmp)/'peer.db');peer.create('Bob','bob','Bob Passwort 2026');peer.open();peer.login('bob','Bob Passwort 2026');peerlock=threading.Lock()
  sync_employees(peer,peerlock,current,client,url,token,str(cert))
  bob=next(x for x in cli('-action','sync-employees') if x['name']=='Bob');cli('-action','employee-account-link','-id',bob['id'],'-account-id','8')
  sync_employees(peer,peerlock,current,client,url,token,str(cert));sync_tickets(peer,peerlock,current,client,url,token,str(cert));peer_ticket=next(x for x in peer.tickets() if x['title']=='Kommentarticket')['id']
  assert sync_ticket_comments(peer,peerlock,current,client,url,token,str(cert))=={'sent':0,'received':3}
  assert {x['body'] for x in peer.comments('ticket',peer_ticket)}=={'Erster Kommentar','Offline','Zentral'}
  peer.add_comment('ticket',peer_ticket,'Von Bob')
  assert sync_ticket_comments(peer,peerlock,current,client,url,token,str(cert))=={'sent':1,'received':0}
  remote=next(x for x in cli('-action','sync-ticket-comments') if x['body']=='Von Bob');assert remote['authorId']==bob['id'] and remote['importedBy']==9
  sync_employees(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':1}
  comment=next(x for x in store.comments('ticket',ticket) if x['body']=='Von Bob');assert comment['author']=='Bob'
  cli('-action','create-comment','-subject-type','ticket','-subject-id',remote_ticket,'-author-id',bob['id'],'-body','Windows Format')
  assert sync()=={'sent':0,'received':1};assert next(x for x in store.comments('ticket',ticket) if x['body']=='Windows Format')['author']=='Bob'
  central_task=cli('-action','create-task','-title','Andere Art','-department','IT','-priority','Mittel','-assignee-id','9')
  cli('-action','add-comment','-subject-type','task','-subject-id',central_task['id'],'-body','Nur Aufgabe')
  assert not any(x['body']=='Nur Aufgabe' for x in cli('-action','sync-ticket-comments'))
  assert all(x['subjectType']=='task' for x in cli('-action','sync-task-comments'))
  assert sync()=={'sent':0,'received':0}
  # An older comment on an unconfirmed parent must not block independent
  # outgoing comments or incoming comments on confirmed parents.
  store.save_ticket(0,'Noch lokal','IT','Mittel',actor);waiting=next(x for x in store.tickets() if x['title']=='Noch lokal')['id']
  store.add_comment('ticket',waiting,'Wartender Kommentar')
  store.add_comment('ticket',ticket,'Unabhängiger Kommentar')
  cli('-action','add-comment','-subject-type','ticket','-subject-id',remote_ticket,'-body','Unabhängig zentral')
  try:sync();raise AssertionError('missing parent was not reported')
  except LocalStoreError as exc:assert 'wartet' in str(exc)
  assert any(x['body']=='Unabhängiger Kommentar' for x in cli('-action','sync-ticket-comments'))
  assert any(x['body']=='Unabhängig zentral' for x in store.comments('ticket',ticket))
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_ticket_comment_sync_outbox').fetchone()[0]==1
  sync_tickets(store,lock,current,client,url,token,str(cert))
  assert sync()=={'sent':1,'received':0}
  assert any(x['body']=='Wartender Kommentar' for x in cli('-action','sync-ticket-comments'))
  peer.close();store.close();print('Ticket comments: two workspaces, authors, offline retry, isolation and bidirectional sync OK')
 finally:process.terminate();process.wait(timeout=5)

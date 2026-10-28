#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,shutil,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from ticket_sync import sync_tickets
from attachment_sync import sync_attachments,inspect_attachment_conflict,resolve_attachment_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-ticket-attachment-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tickets.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  (Path(tmp)/'one').mkdir();(Path(tmp)/'two').mkdir();db=Path(tmp)/'one'/'local.db';store=LocalStore(db);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock();current=lambda:True
  sync_employees(store,lock,current,client,url,token,str(cert));admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,current,client,url,token,str(cert));actor=store.session_employee()
  store.save_ticket(0,'Anhangsticket','IT','Mittel',actor);ticket=store.tickets()[0]['id'];source=Path(tmp)/'dokument.txt';source.write_text('Erster Inhalt ä')
  store.import_attachment('ticket',ticket,source);sync_tickets(store,lock,current,client,url,token,str(cert));sync=lambda:sync_attachments(store,lock,current,client,url,token,str(cert),'ticket')
  assert sync()=={'sent':1,'received':0};remote_ticket=cli('-action','sync-tickets')[0]['id'];remote=cli('-action','sync-attachments','-subject-type','ticket');assert len(remote)==1 and remote[0]['uploadedBy']==admin['id'] and remote[0]['importedBy']==9
  local=store.attachments('ticket',ticket)[0];assert Path(store.verify_attachment('ticket',ticket,local['id'])).read_text()=='Erster Inhalt ä'
  peer=LocalStore(Path(tmp)/'two'/'peer.db');peer.create('Bob','bob','Bob Passwort 2026');peer.open();peer.login('bob','Bob Passwort 2026');peerlock=threading.Lock()
  sync_employees(peer,peerlock,current,client,url,token,str(cert));bob=next(x for x in cli('-action','sync-employees') if x['name']=='Bob');cli('-action','employee-account-link','-id',bob['id'],'-account-id','8');sync_employees(peer,peerlock,current,client,url,token,str(cert));sync_tickets(peer,peerlock,current,client,url,token,str(cert));peer_ticket=next(x for x in peer.tickets() if x['title']=='Anhangsticket')['id']
  syncpeer=lambda:sync_attachments(peer,peerlock,current,client,url,token,str(cert),'ticket')
  with sqlite3.connect(peer.path) as sql:sql.execute("CREATE TRIGGER fail_pull_audit BEFORE INSERT ON audit_log WHEN NEW.action='ticket_attachments.sync.pulled' BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:syncpeer();raise AssertionError('failed incoming audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(peer.path) as sql:
   assert sql.execute('SELECT count(*) FROM attachments').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_pull_audit')
  peer_dir=Path(tmp)/'two'/'attachments'/ 'ticket'/str(peer_ticket )
  assert not peer_dir.exists() or not list(peer_dir.iterdir())
  assert syncpeer()=={'sent':0,'received':1}
  peer_file=peer.attachments('ticket',peer_ticket)[0];assert Path(peer.verify_attachment('ticket',peer_ticket,peer_file['id'])).read_text()=='Erster Inhalt ä'
  other=Path(tmp)/'zweiter.txt';other.write_text('Von Bob');process.terminate();process.wait(timeout=5);peer.import_attachment('ticket',peer_ticket,other)
  moved=Path(tmp)/'moved';moved.mkdir();peer.backup(moved/'peer.db');shutil.copytree(Path(tmp)/'two'/'attachments',moved/'attachments');peer.close()
  peer=LocalStore(moved/'peer.db');peer.open();peer.login('bob','Bob Passwort 2026')
  pending=next(a for a in peer.attachments('ticket',peer_ticket) if a['name']=='zweiter.txt')
  assert Path(peer.verify_attachment('ticket',peer_ticket,pending['id'])).is_relative_to(moved)
  try:syncpeer();raise AssertionError('offline succeeded')
  except LocalStoreError:pass
  process=start()
  with sqlite3.connect(peer.path) as sql:sql.execute("CREATE TRIGGER fail_attachment_ack BEFORE DELETE ON linux_ticket_attachment_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:syncpeer();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(peer.path) as sql:
   assert sql.execute('SELECT count(*) FROM linux_ticket_attachment_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_attachment_ack')
  assert syncpeer()=={'sent':1,'received':0};sync_employees(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':1}
  bob_file=next(x for x in store.attachments('ticket',ticket) if x['name']=='zweiter.txt');assert Path(store.verify_attachment('ticket',ticket,bob_file['id'])).read_text()=='Von Bob'
  store.archive_attachment('ticket',ticket,local['id']);peer.archive_attachment('ticket',peer_ticket,peer_file['id']);assert syncpeer()=={'sent':1,'received':0}
  try:sync();raise AssertionError('concurrent archive accepted without review')
  except LocalStoreError as exc:assert '409' in str(exc)
  inspect=lambda:inspect_attachment_conflict(store,lock,current,client,url,token,str(cert),'ticket')
  resolve=lambda review,accept:resolve_attachment_conflict(store,lock,current,client,url,token,str(cert),review,accept)
  reviewed=inspect();assert not reviewed['preview']['local']['active'] and not reviewed['remote']['active']
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_resolution BEFORE INSERT ON audit_log WHEN NEW.action='ticket_attachments.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:resolve(reviewed,False);raise AssertionError('failed conflict audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_ticket_attachment_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_resolution')
  resolve(reviewed,False);assert sync()=={'sent':0,'received':0};assert all(x['id']!=local['id'] for x in store.attachments('ticket',ticket) )
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_ticket_attachment_sync_conflicts').fetchone()[0]==1
  central_file=Path(tmp)/'zentral.txt';central_file.write_text('Zentrale Datei')
  cli('-action','subject-attachment-upload','-subject-type','ticket','-subject-id',remote_ticket,'-file',str(central_file))
  assert sync()=={'sent':0,'received':1};central_item=next(x for x in store.attachments('ticket',ticket) if x['name']=='zentral.txt')
  assert Path(store.verify_attachment('ticket',ticket,central_item['id'])).read_text()=='Zentrale Datei'
  store.archive_attachment('ticket',ticket,central_item['id']);reviewed=inspect();assert reviewed['remote']['active'] and not reviewed['preview']['local']['active']
  with sqlite3.connect(db) as sql:sql.execute('UPDATE attachments SET original_name=? WHERE id=?',('geändert.txt',central_item['id']))
  try:resolve(reviewed,True);raise AssertionError('stale local preview accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:sql.execute('UPDATE attachments SET original_name=? WHERE id=?',('zentral.txt',central_item['id']))
  reviewed=inspect();resolve(reviewed,True);restored=next(x for x in store.attachments('ticket',ticket) if x['id']==central_item['id']);assert Path(store.verify_attachment('ticket',ticket,restored['id'])).read_text()=='Zentrale Datei'
  store.archive_attachment('ticket',ticket,central_item['id']);assert sync()=={'sent':1,'received':0}
  store.save_ticket(0,'Noch lokales Ticket','IT','Mittel',actor);waiting=next(x for x in store.tickets() if x['title']=='Noch lokales Ticket')['id']
  pending_file=Path(tmp)/'wartend.txt';pending_file.write_text('Wartender Anhang');store.import_attachment('ticket',waiting,pending_file)
  ready_file=Path(tmp)/'bereit.txt';ready_file.write_text('Unabhängiger Anhang');store.import_attachment('ticket',ticket,ready_file)
  inbound=Path(tmp)/'eingang.txt';inbound.write_text('Empfang während Wartezeit')
  cli('-action','subject-attachment-upload','-subject-type','ticket','-subject-id',remote_ticket,'-file',str(inbound))
  try:sync();raise AssertionError('missing parent was not reported')
  except LocalStoreError as exc:assert 'wartet' in str(exc)
  assert any(x['originalName']=='bereit.txt' for x in cli('-action','sync-attachments','-subject-type','ticket'))
  item=next(x for x in store.attachments('ticket',ticket) if x['name']=='eingang.txt')
  assert Path(store.verify_attachment('ticket',ticket,item['id'])).read_text()=='Empfang während Wartezeit'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_ticket_attachment_sync_outbox').fetchone()[0]==1
  sync_tickets(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':1,'received':0}
  assert any(x['originalName']=='wartend.txt' for x in cli('-action','sync-attachments','-subject-type','ticket'))
  bad=Path(tmp)/'defekt.txt';bad.write_text('ursprünglich');store.import_attachment('ticket',ticket,bad);bad_item=next(x for x in store.attachments('ticket',ticket) if x['name']=='defekt.txt');Path(bad_item['storedPath']).write_text('manipuliert')
  try:sync();raise AssertionError('corrupt file accepted')
  except LocalStoreError as exc:assert 'Integritätsprüfung' in str(exc) or 'Größe' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_ticket_attachment_sync_outbox').fetchone()[0]==1
  peer.close();store.close();print('Attachment sync: two workspaces, verified files, offline retry and archive OK')
 finally:process.terminate();process.wait(timeout=5)

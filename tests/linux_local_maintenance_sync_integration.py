#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from facility_asset_sync import sync_facility_assets
from maintenance_sync import sync_maintenance,inspect_maintenance_conflict,resolve_maintenance_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-maintenance-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage','tickets.manage','facility.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  store.save_asset(0,'Anlage','Technik','EG','operational');asset=store.assets()[0]['id'];sync_asset=lambda:sync_facility_assets(store,lock,current,client,url,token,str(cert));sync_asset();remote_asset=cli('-action','sync-facility-assets')[0]['id']
  sync=lambda:sync_maintenance(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_maint_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('maintenance_order.created','maintenance_order.updated','maintenance_order.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.save_maintenance(asset,0,'Rollback','open',1900000000,7);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM maintenance_orders').fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_maint_audit')
  store.save_maintenance(asset,0,'Wartung','open',1900000000,7);order=store.maintenance(asset)[0]['id']
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_maint_ack BEFORE DELETE ON linux_maintenance_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_maint_ack')
  assert sync()=={'sent':1,'received':0};remote=cli('-action','sync-maintenance')[0];rid=remote['id'];assert remote['assetId']==remote_asset
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_maint_change BEFORE INSERT ON audit_log WHEN NEW.action IN ('maintenance_order.updated','maintenance_order.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  for action in (lambda:store.save_maintenance(asset,order,'Fehler','open',1900000000,7),lambda:store.archive_maintenance(asset,order)):
   try:action();raise AssertionError('audit failure accepted')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT title,active FROM maintenance_orders WHERE id=?',(order,)).fetchone()==('Wartung',1)
   assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_maint_change')
  process.terminate();process.wait(timeout=5);store.save_maintenance(asset,order,'Offline','open',1900000000,7)
  try:sync();raise AssertionError('offline succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':1,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_recurrence_audit BEFORE INSERT ON audit_log WHEN NEW.action='maintenance_order.recurrence.created' BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.maintenance_status(asset,order,'completed');raise AssertionError('failed recurrence audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM maintenance_orders WHERE id=?',(order,)).fetchone()==('open',)
   assert sql.execute('SELECT count(*) FROM maintenance_orders').fetchone()[0]==1
   assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_recurrence_audit')
  store.maintenance_status(asset,order,'completed')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_reserved_ack BEFORE DELETE ON linux_maintenance_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('reservation acknowledgement failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   frozen=json.loads(sql.execute('SELECT wire_json FROM linux_maintenance_sync_outbox ORDER BY seq LIMIT 1').fetchone()[0])
   assert frozen['recurrenceSuccessorId'].startswith(frozen['clientId']+':maintenance_order:')
   sql.execute('DROP TRIGGER fail_reserved_ack')
  process.terminate();process.wait(timeout=5);process=start()
  assert sync()=={'sent':2,'received':0}
  values=cli('-action','sync-maintenance');assert len(values)==2 and len([x for x in values if x['status']=='open' and x['dueAt']==1900000000+7*86400])==1
  store.maintenance_status(asset,order,'open');store.maintenance_status(asset,order,'completed');assert sync()=={'sent':2,'received':0}
  assert len(cli('-action','sync-maintenance'))==2 and len(store.maintenance(asset))==2
  remote_parent=next(x for x in cli('-action','sync-maintenance') if x['status']=='completed')
  cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',remote_parent['id'],'-status','open');cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',remote_parent['id'],'-status','completed')
  assert len(cli('-action','sync-maintenance'))==2
  (Path(tmp)/'peer').mkdir();peer=LocalStore(Path(tmp)/'peer'/'peer.db');peer.create('Bob','bob','Bob Passwort 2026');peer.open();peer.login('bob','Bob Passwort 2026');peerlock=threading.Lock()
  assert sync_facility_assets(peer,peerlock,current,client,url,token,str(cert))=={'sent':0,'received':1};peer_asset=peer.assets()[0]['id']
  assert sync_maintenance(peer,peerlock,current,client,url,token,str(cert))=={'sent':0,'received':2};peer_order=next(v for v in peer.maintenance(peer_asset) if v['status']=='open')
  peer.save_maintenance(peer_asset,peer_order['id'],'Peer Wartung','open',1900000000+7*86400,7)
  assert sync_maintenance(peer,peerlock,current,client,url,token,str(cert))=={'sent':1,'received':0};assert sync()=={'sent':0,'received':1};assert any(v['title']=='Peer Wartung' for v in store.maintenance(asset));peer.close()
  # A central recurring completion independently produces exactly one successor.
  central=cli('-action','central-maintenance-create','-asset-id',remote_asset,'-title','Zentral','-due','1900100000','-recurrence','3')
  assert sync()=={'sent':0,'received':1};imported=next(v for v in store.maintenance(asset) if v['title']=='Zentral')
  cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',central['id'],'-status','completed')
  assert sync()=={'sent':0,'received':2};assert len([x for x in store.maintenance(asset) if x['title']=='Zentral'])==2
  process.terminate();process.wait(timeout=5);process=start()
  cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',central['id'],'-status','open');cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',central['id'],'-status','completed')
  assert len([x for x in cli('-action','sync-maintenance') if x['title']=='Zentral'])==2
  store.maintenance_status(asset,imported['id'],'open');store.maintenance_status(asset,imported['id'],'completed')
  assert sync()=={'sent':2,'received':0};assert len([x for x in store.maintenance(asset) if x['title']=='Zentral'])==2
  peer.open();peer.login('bob','Bob Passwort 2026');sync_facility_assets(peer,peerlock,current,client,url,token,str(cert));sync_maintenance(peer,peerlock,current,client,url,token,str(cert))
  peer_parent=next(x for x in peer.maintenance(peer_asset) if x['title']=='Zentral' and x['status']=='completed')
  peer.maintenance_status(peer_asset,peer_parent['id'],'open');peer.maintenance_status(peer_asset,peer_parent['id'],'completed')
  assert len([x for x in peer.maintenance(peer_asset) if x['title']=='Zentral'])==2
  assert sync_maintenance(peer,peerlock,current,client,url,token,str(cert))=={'sent':2,'received':0};peer.close()

  store.save_maintenance(asset,0,'Einmalig','open',1900300000,0);one=next(v for v in store.maintenance(asset) if v['title']=='Einmalig');assert sync()=={'sent':1,'received':0};one_remote=next(x for x in cli('-action','sync-maintenance') if x['title']=='Einmalig')
  store.save_maintenance(asset,one['id'],'Lokal','open',1900300000,0);cli('-action','central-maintenance-update','-asset-id',remote_asset,'-id',one_remote['id'],'-title','Server','-due','1900300000','-recurrence','0')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  inspect=lambda:inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));resolve=lambda r,a:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),r,a)
  reviewed=inspect();store.save_maintenance(asset,one['id'],'Später','open',1900300000,0)
  try:resolve(reviewed,False);raise AssertionError('stale decision accepted')
  except LocalStoreError:pass
  resolve(inspect(),False);assert sync()=={'sent':1,'received':0};assert next(x for x in cli('-action','sync-maintenance') if x['id']==one_remote['id'])['title']=='Später'
  store.save_maintenance(asset,one['id'],'Lokal 2','open',1900300000,0);cli('-action','central-maintenance-update','-asset-id',remote_asset,'-id',one_remote['id'],'-title','Server 2','-due','1900300000','-recurrence','0');resolve(inspect(),True)
  assert next(x for x in store.maintenance(asset) if x['id']==one['id'])['title']=='Server 2'
  # Independent completion on both sides must not silently create two successors.
  store.save_maintenance(asset,0,'Parallel','open',1900200000,5);parallel=next(v for v in store.maintenance(asset) if v['title']=='Parallel');assert sync()=={'sent':1,'received':0};p_remote=next(x for x in cli('-action','sync-maintenance') if x['title']=='Parallel')
  store.maintenance_status(asset,parallel['id'],'completed');cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',p_remote['id'],'-status','completed')
  try:sync();raise AssertionError('parallel completion overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  reviewed=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert reviewed['possibleDuplicateSuccessor']
  for accept in (True,False):
   try:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),reviewed,accept);raise AssertionError('duplicate recurrence accepted')
   except LocalStoreError as exc:assert 'Folgeaufträge' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==2
  local_follow=next(x for x in store.maintenance(asset) if x['title']=='Parallel' and x['status']=='open')
  store.save_maintenance(asset,local_follow['id'],'Lokal geändert','open',1900200000+5*86400,5)
  try:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),reviewed,True,True);raise AssertionError('stale follow-up review accepted')
  except LocalStoreError:pass
  reviewed=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert len(reviewed['preview']['successorDetails'])==len(reviewed['serverSuccessors'])==1
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_link_audit BEFORE INSERT ON audit_log WHEN NEW.action='maintenance.successor.linked_local' BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),reviewed,False,True);raise AssertionError('failed link audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==3
   sql.execute('DROP TRIGGER fail_link_audit')
  resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),reviewed,False,True);assert sync()=={'sent':2,'received':0}
  values=cli('-action','sync-maintenance');assert len([v for v in values if v['id']==p_remote['id'] or (v['title']=='Lokal geändert' and v['dueAt']==1900200000+5*86400)])==2
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_maintenance_successor_links').fetchone()[0]==1
  # Server choice preserves the remote successor and records the discarded local operations.
  store.save_maintenance(asset,0,'Serverwahl','open',1900400000,2);p2=next(v for v in store.maintenance(asset) if v['title']=='Serverwahl');assert sync()=={'sent':1,'received':0};remote2=next(x for x in cli('-action','sync-maintenance') if x['title']=='Serverwahl')
  store.maintenance_status(asset,p2['id'],'completed');cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',remote2['id'],'-status','completed')
  child2=next(v for v in store.maintenance(asset) if v['title']=='Serverwahl' and v['status']=='open');store.save_maintenance(asset,child2['id'],'Nur lokal','open',1900400000+2*86400,2)
  review2=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert review2['possibleDuplicateSuccessor']
  resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),review2,True,True);assert sync()=={'sent':0,'received':0}
  assert next(v for v in store.maintenance(asset) if v['id']==child2['id'])['title']=='Serverwahl'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_maintenance_successor_links').fetchone()[0]==2
  # Ambiguous remote candidates must never be linked automatically.
  store.save_maintenance(asset,0,'Mehrdeutig','open',1900500000,4);p3=next(v for v in store.maintenance(asset) if v['title']=='Mehrdeutig');assert sync()=={'sent':1,'received':0};remote3=next(v for v in cli('-action','sync-maintenance') if v['title']=='Mehrdeutig')
  store.maintenance_status(asset,p3['id'],'completed');cli('-action','central-maintenance-status','-asset-id',remote_asset,'-id',remote3['id'],'-status','completed')
  first_review=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert len(first_review['serverSuccessors'])==1
  follow_id=first_review['preview']['successorDetails'][0]['localId']
  with sqlite3.connect(db) as sql:sql.execute("UPDATE linux_maintenance_sync_outbox SET wire_json='{}' WHERE maintenance_id=? AND action='create'",(follow_id,))
  potentially_sent=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert))
  try:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),potentially_sent,True,True);raise AssertionError('potentially sent successor linked')
  except LocalStoreError as exc:assert 'übertragen' in str(exc)
  with sqlite3.connect(db) as sql:sql.execute("UPDATE linux_maintenance_sync_outbox SET wire_json=NULL WHERE maintenance_id=? AND action='create'",(follow_id,))
  other=cli('-action','central-maintenance-create','-asset-id',remote_asset,'-title','Andere mögliche Folge','-due',str(1900500000+4*86400),'-recurrence','4')
  ambiguous=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert len(ambiguous['serverSuccessors'])==2
  try:resolve_maintenance_conflict(store,lock,current,client,url,token,str(cert),ambiguous,True,True);raise AssertionError('ambiguous successor linked')
  except LocalStoreError as exc:assert 'eindeutig' in str(exc)
  cli('-action','central-maintenance-archive','-asset-id',remote_asset,'-id',other['id'])
  after_archive=inspect_maintenance_conflict(store,lock,current,client,url,token,str(cert));assert len(after_archive['serverSuccessors'])==2
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_maintenance_successor_links').fetchone()[0]==2
   assert sql.execute('SELECT count(*) FROM linux_maintenance_sync_outbox').fetchone()[0]==2
  store.close();print('Maintenance sync: recurrence, duplicate guard, offline and audit rollback OK')
 finally:process.terminate();process.wait(timeout=5)

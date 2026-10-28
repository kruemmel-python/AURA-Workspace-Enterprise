#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from facility_asset_sync import sync_facility_assets,inspect_facility_asset_conflict,resolve_facility_asset_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-asset-sync-') as tmp:
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
  sync=lambda:sync_facility_assets(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_asset_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('facility_asset.created','facility_asset.updated','facility_asset.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.save_asset(0,'Rollback','Technik','EG','operational');raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM facility_assets').fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_facility_asset_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_asset_audit')
  store.save_asset(0,'Lokal','Technik','EG','operational');asset=store.assets()[0]['id'];store.save_maintenance(asset,0,'Wartung','open',0,0)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_asset_ack BEFORE DELETE ON linux_facility_asset_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_facility_asset_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_asset_ack')
  assert sync()=={'sent':1,'received':0};remote=cli('-action','sync-facility-assets')[0];rid=remote['id'];assert remote['name']=='Lokal'
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_asset_change BEFORE INSERT ON audit_log WHEN NEW.action IN ('facility_asset.updated','facility_asset.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  for action in (lambda:store.save_asset(asset,'Fehler','Technik','EG','maintenance'),lambda:store.archive_asset(asset)):
   try:action();raise AssertionError('audit failure accepted')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT name,active FROM facility_assets WHERE id=?',(asset,)).fetchone()==('Lokal',1)
   assert sql.execute('SELECT count(*) FROM linux_facility_asset_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_asset_change')
  process.terminate();process.wait(timeout=5);store.save_asset(asset,'Offline','Technik','EG','maintenance')
  try:sync();raise AssertionError('offline succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':1,'received':0}
  central=cli('-action','create-facility-asset','-name','Zentral','-asset-category','Gerät','-location','OG');assert sync()=={'sent':0,'received':1}
  (Path(tmp)/'peer').mkdir();peer=LocalStore(Path(tmp)/'peer'/'peer.db');peer.create('Bob','bob','Bob Passwort 2026');peer.open();peer.login('bob','Bob Passwort 2026');peerlock=threading.Lock();sync_peer=lambda:sync_facility_assets(peer,peerlock,current,client,url,token,str(cert))
  assert sync_peer()=={'sent':0,'received':2};peer_asset=next(v for v in peer.assets() if v['name']=='Zentral');peer.save_asset(peer_asset['id'],'Peer-Zentral','Gerät','OG','maintenance')
  assert sync_peer()=={'sent':1,'received':0};assert sync()=={'sent':0,'received':1};assert next(v for v in store.assets() if v['name']=='Peer-Zentral')['status']=='maintenance';peer.close()
  store.save_asset(asset,'Lokal Konflikt','Technik','EG','maintenance')
  cli('-action','update-facility-asset','-id',rid,'-name','Server Konflikt','-asset-category','Technik','-location','EG')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  inspect=lambda:inspect_facility_asset_conflict(store,lock,current,client,url,token,str(cert));resolve=lambda r,a:resolve_facility_asset_conflict(store,lock,current,client,url,token,str(cert),r,a)
  reviewed=inspect();store.save_asset(asset,'Lokal später','Technik','EG','maintenance')
  try:resolve(reviewed,False);raise AssertionError('stale decision accepted')
  except LocalStoreError:pass
  resolve(inspect(),False);assert sync()=={'sent':1,'received':0};assert next(v for v in cli('-action','sync-facility-assets') if v['id']==rid)['name']=='Lokal später'
  store.save_asset(asset,'Lokal 2','Technik','EG','maintenance');cli('-action','update-facility-asset','-id',rid,'-name','Server 2','-asset-category','Technik','-location','EG');resolve(inspect(),True)
  assert next(v for v in store.assets() if v['id']==asset)['name']=='Server 2';assert len(store.maintenance(asset))==1
  store.archive_asset(asset);assert sync()=={'sent':1,'received':0};assert not next(v for v in cli('-action','sync-facility-assets') if v['id']==rid)['active']
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_facility_asset_sync_conflicts').fetchone()[0]==2
  store.close();print('Facility asset sync: offline, audit rollback, conflicts and archive OK')
 finally:process.terminate();process.wait(timeout=5)

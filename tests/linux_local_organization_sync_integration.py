#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from organization_sync import sync_organizations,inspect_organization_conflict,resolve_organization_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-organization-sync-') as tmp:
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
  sync=lambda:sync_organizations(store,lock,current,client,url,token,str(cert));assert sync()=={'sent':0,'received':0}
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_org_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('organization_unit.created','organization_unit.updated','organization_unit.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  try:store.save_organization(0,'Rollback','RB',actor);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM organization_units').fetchone()[0]==0 and sql.execute('SELECT count(*) FROM linux_organization_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_org_audit')
  store.save_organization(0,'Ohne Leitung','NULL',0);store.save_organization(0,'Entwicklung','DEV',actor)
  without=next(v for v in store.organizations() if v['code']=='NULL');unit=next(v for v in store.organizations() if v['code']=='DEV')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_org_ack BEFORE DELETE ON linux_organization_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:sync();raise AssertionError('ack failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_organization_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_org_ack')
  assert sync()=={'sent':2,'received':0};remote=cli('-action','sync-organizations');assert next(v for v in remote if v['code']=='NULL')['managerId']==0;rid=next(v['id'] for v in remote if v['code']=='DEV');assert next(v for v in remote if v['id']==rid)['managerId']==9
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_org_change_audit BEFORE INSERT ON audit_log WHEN NEW.action IN ('organization_unit.updated','organization_unit.archived') BEGIN SELECT RAISE(ABORT,'audit'); END")
  for action in (lambda:store.save_organization(unit['id'],'Fehler','DEV',actor),lambda:store.archive_organization(unit['id'])):
   try:action();raise AssertionError('audit failure accepted')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT name,active FROM organization_units WHERE id=?',(unit['id'],)).fetchone()==('Entwicklung',1)
   assert sql.execute('SELECT count(*) FROM linux_organization_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_org_change_audit')
  process.terminate();process.wait(timeout=5);store.save_organization(unit['id'],'Offline','DEV',actor)
  try:sync();raise AssertionError('offline succeeded')
  except LocalStoreError:pass
  process=start();assert sync()=={'sent':1,'received':0}
  central=cli('-action','central-organization-create','-name','Zentral','-code','Z','-manager-id','0');assert sync()=={'sent':0,'received':1}
  (Path(tmp)/'peer').mkdir();peer=LocalStore(Path(tmp)/'peer'/'peer.db');peer.create('Bob','bob','Bob Passwort 2026');peer.open();peer.login('bob','Bob Passwort 2026');peerlock=threading.Lock()
  sync_employees(peer,peerlock,current,client,url,token,str(cert));sync_peer=lambda:sync_organizations(peer,peerlock,current,client,url,token,str(cert))
  assert sync_peer()=={'sent':0,'received':3};peerunit=next(v for v in peer.organizations() if v['code']=='Z');peer.save_organization(peerunit['id'],'Peer-Zentral','Z',0)
  assert sync_peer()=={'sent':1,'received':0};assert sync()=={'sent':0,'received':1};assert next(v for v in store.organizations() if v['code']=='Z')['name']=='Peer-Zentral'
  peer.close()
  store.save_organization(unit['id'],'Lokal','DEV',actor);cli('-action','central-organization-update','-id',rid,'-name','Server','-code','DEV','-manager-id','9')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  inspect=lambda:inspect_organization_conflict(store,lock,current,client,url,token,str(cert));resolve=lambda r,a:resolve_organization_conflict(store,lock,current,client,url,token,str(cert),r,a)
  reviewed=inspect();store.save_organization(unit['id'],'Lokal später','DEV',actor)
  try:resolve(reviewed,False);raise AssertionError('stale decision accepted')
  except LocalStoreError:pass
  resolve(inspect(),False);assert sync()=={'sent':1,'received':0};assert next(v for v in cli('-action','sync-organizations') if v['id']==rid)['name']=='Lokal später'
  store.save_organization(unit['id'],'Lokal 2','DEV',actor);cli('-action','central-organization-update','-id',rid,'-name','Server 2','-code','DEV','-manager-id','0');resolve(inspect(),True)
  assert next(v for v in store.organizations() if v['id']==unit['id'])['name']=='Server 2';assert next(v for v in store.organizations() if v['id']==unit['id'])['managerId']==0
  store.archive_organization(unit['id']);assert sync()=={'sent':1,'received':0};assert not next(v for v in cli('-action','sync-organizations') if v['id']==rid)['active']
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_organization_sync_conflicts').fetchone()[0]==2
  store.close();print('Organization sync: manager mapping, offline, rollback, conflicts and archive OK')
 finally:process.terminate();process.wait(timeout=5)

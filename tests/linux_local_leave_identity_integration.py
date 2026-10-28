#!/usr/bin/python3
"""Explicit reconciliation of independently created personal leave requests."""
import json,os,socket,ssl,subprocess,sys,tempfile,threading,time,urllib.request,sqlite3
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
import leave_sync as leave_sync_module
from leave_sync import sync_leave,inspect_leave_conflict,accept_server_leave
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-leave-identity-') as tmp:
 cert=Path(tmp)/'cert';key=Path(tmp)/'key'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='leave-test'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','leave.request','leave.approve']},'other':{'employeeId':8,'permissions':['leave.request']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 try:
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ssl.create_default_context(cafile=str(cert)),timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server startup failed')
  def cli(*args,environment=env):return json.loads(subprocess.run([str(client),*args],env=environment,text=True,capture_output=True,check=True).stdout)
  path=Path(tmp)/'local.sqlite';store=LocalStore(path);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock();current=lambda:True
  sync_employees(store,lock,current,client,url,token,str(cert));admin=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',admin['id'],'-account-id','9');sync_employees(store,lock,current,client,url,token,str(cert))
  sync=lambda:sync_leave(store,lock,current,client,url,token,str(cert));sync()
  inspect=lambda:inspect_leave_conflict(store,lock,current,client,url,token,str(cert))
  accept=lambda review:accept_server_leave(store,lock,current,client,url,token,str(cert),review)
  remote=cli('-action','create-leave','-starts-at','1901000000','-ends-at','1901086400','-reason','Familienurlaub')
  store.create_leave(1901000000,1901086400,'Familienurlaub')
  reviewed=inspect();assert reviewed['identityConflict'] and reviewed['remote']['id']==remote['id']
  with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_leave_identity BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:accept(reviewed);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='leave' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==reviewed['preview']['local']['id']
   db.execute('DROP TRIGGER fail_leave_identity')
  accept(reviewed)
  assert store.leave()[0]['reason']=='Familienurlaub'
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='leave' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==remote['id']
   assert db.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==0
  sync();assert len([v for v in cli('-action','leave') if v['reason']=='Familienurlaub'])==1
  other_env=env.copy();other_env['AURA_SYNC_TOKEN']='other'
  cli('-action','create-leave','-starts-at','1901500000','-ends-at','1901586400','-reason','Andere Person',environment=other_env)
  store.create_leave(1901500000,1901586400,'Andere Person')
  try:inspect();raise AssertionError('other employee matched')
  except LocalStoreError:pass
  own=cli('-action','create-leave','-starts-at','1901500000','-ends-at','1901586400','-reason','Andere Person')
  with sqlite3.connect(path) as db:db.execute("UPDATE linux_leave_sync_outbox SET wire_json='{}' WHERE seq=(SELECT min(seq) FROM linux_leave_sync_outbox)")
  try:inspect();raise AssertionError('prepared request accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:db.execute('UPDATE linux_leave_sync_outbox SET wire_json=NULL WHERE seq=(SELECT min(seq) FROM linux_leave_sync_outbox)')
  reviewed=inspect();assert reviewed['remote']['id']==own['id'];accept(reviewed)
  frozen_remote=cli('-action','create-leave','-starts-at','1901600000','-ends-at','1901686400','-reason','Vorbereiteter Urlaub')
  store.create_leave(1901600000,1901686400,'Vorbereiteter Urlaub')
  original_connect=leave_sync_module.connect_sync_server
  def offline_before_send(*args):
   normalized,actor,command=original_connect(*args)
   def wrapped(*params):
    if params[:2]==('-action','sync-leave-event'):raise LocalStoreError('simulated network interruption')
    return command(*params)
   return normalized,actor,wrapped
  leave_sync_module.connect_sync_server=offline_before_send
  try:
   try:sync();raise AssertionError('network interruption ignored')
   except LocalStoreError:pass
  finally:leave_sync_module.connect_sync_server=original_connect
  reviewed=inspect();assert reviewed['identityConflict'] and reviewed.get('preparedOperation')
  frozen=reviewed['preparedOperation']
  denied=subprocess.run([str(client),'-action','abandon-leave-event','-payload',json.dumps({'operation':frozen,'remote':reviewed['remote']})],env=other_env,text=True,capture_output=True)
  assert denied.returncode and '400' in denied.stderr
  with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_frozen_leave_audit BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:accept(reviewed);raise AssertionError('audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(path) as db:
   assert db.execute("SELECT external_id FROM sync_object_links WHERE object_type='leave' AND local_id=?",(reviewed['preview']['localId'],)).fetchone()[0]==reviewed['preview']['local']['id']
   db.execute('DROP TRIGGER fail_frozen_leave_audit')
  accept(reviewed)
  rejected=subprocess.run([str(client),'-action','sync-leave-event','-payload',json.dumps(frozen)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  process.terminate();process.wait(timeout=5);process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ssl.create_default_context(cafile=str(cert)),timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('restart failed')
  rejected=subprocess.run([str(client),'-action','sync-leave-event','-payload',json.dumps(frozen)],env=env,text=True,capture_output=True)
  assert rejected.returncode and '409' in rejected.stderr
  assert len([v for v in cli('-action','leave') if v['reason']=='Vorbereiteter Urlaub'])==1
  accepted=json.loads(json.dumps(frozen));accepted['operationId']='accepted-leave-operation';accepted['leave']['id']=accepted['clientId']+':leave:98765';accepted['leave']['reason']='Schon bestätigt'
  cli('-action','sync-leave-event','-payload',json.dumps(accepted))
  separate=cli('-action','create-leave','-starts-at',str(accepted['leave']['startsAt']),'-ends-at',str(accepted['leave']['endsAt']),'-reason','Schon bestätigt')
  refused=subprocess.run([str(client),'-action','abandon-leave-event','-payload',json.dumps({'operation':accepted,'remote':separate})],env=env,text=True,capture_output=True)
  assert refused.returncode and '409' in refused.stderr
  cli('-action','create-leave','-starts-at','1902000000','-ends-at','1902086400','-reason','Mehrdeutig')
  cli('-action','create-leave','-starts-at','1902000000','-ends-at','1902086400','-reason','Mehrdeutig')
  store.create_leave(1902000000,1902086400,'Mehrdeutig')
  try:inspect();raise AssertionError('ambiguous requests accepted')
  except LocalStoreError:pass
  store.close()
 finally:process.terminate();process.wait(timeout=5)
print('Independent leave mapping: authenticated identity, ambiguous matches and durable association OK')

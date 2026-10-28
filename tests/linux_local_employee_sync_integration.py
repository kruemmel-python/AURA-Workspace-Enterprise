#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees,inspect_employee_conflict,resolve_employee_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-employee-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  store.save_employee(0,'Ada','Developer','IT')
  lock=threading.Lock();sync=lambda:sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  assert sync()=={'sent':2,'received':0}
  # A failed durable queue write must roll back the employee change itself.
  with sqlite3.connect(db) as sql: sql.execute("CREATE TRIGGER fail_employee_outbox BEFORE INSERT ON linux_employee_sync_outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
  try:store.save_employee(0,'Rollback','Developer','IT');raise AssertionError('employee persisted without outbox')
  except LocalStoreError:pass
  try:store.save_employee_with_role(0,'Rollback mit Rolle','Developer','IT','employee');raise AssertionError('employee and role persisted without outbox')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM employees WHERE name IN ('Rollback','Rollback mit Rolle')").fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_employee_outbox')
  employees=cli('-action','sync-employees');assert {e['name'] for e in employees}=={'Admin','Ada'}
  ada=next(e for e in store.employees() if e['name']=='Ada'); remote=next(e for e in employees if e['name']=='Ada')
  # Audit failures must roll back each directory edit and its queued event.
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_employee_audit BEFORE INSERT ON audit_log WHEN NEW.action LIKE 'employee.%' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  for action in (lambda:store.save_employee(0,'Auditfehler','Developer','IT'),lambda:store.save_employee(ada['id'],'Ada','Developer','Fehler'),lambda:store.employee_presence(ada['id'],'remote'),lambda:store.archive_employee(ada['id'])):
   try:action();raise AssertionError('employee edit survived audit failure')
   except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM employees WHERE name='Auditfehler'").fetchone()[0]==0
   assert sql.execute('SELECT department,presence,active FROM employees WHERE id=?',(ada['id'],)).fetchone()==('IT','office',1)
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_employee_audit')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_employee_role_audit BEFORE INSERT ON audit_log WHEN NEW.action='employee.role.replaced' BEGIN SELECT RAISE(ABORT,'role audit failure'); END")
  try:store.save_employee_with_role(ada['id'],'Unzulässig','Developer','IT','employee');raise AssertionError('employee edit survived role audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT name FROM employees WHERE id=?',(ada['id'],)).fetchone()==('Ada',)
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_employee_role_audit')
  process.terminate();process.wait(timeout=5);store.employee_presence(ada['id'],'remote');store.save_employee_with_role(0,'Offline Mitarbeiter','Technik','IT','employee')
  store.save_employee_with_role(ada['id'],'Ada','Senior Developer','IT','project_manager')
  try:sync();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==3
  store.close();store.open();store.login('admin','Admin Passwort 2026');process=start();assert sync()=={'sent':3,'received':0}
  assert next(e for e in store.employees() if e['name']=='Offline Mitarbeiter')['systemRole']=='employee'
  assert any(e['name']=='Offline Mitarbeiter' for e in cli('-action','sync-employees'))
  assert next(e for e in store.employees() if e['id']==ada['id'])['systemRole']=='project_manager'
  assert next(e for e in cli('-action','sync-employees') if e['id']==remote['id'])['role']=='Senior Developer'
  assert next(e for e in cli('-action','sync-employees') if e['id']==remote['id'])['presence']=='remote'
  central=cli('-action','central-employee-create','-name','Grace','-role','Engineer','-department','Ops','-presence','office')
  assert sync()=={'sent':0,'received':1}; grace=next(e for e in store.employees() if e['name']=='Grace')
  cli('-action','central-employee-update','-id',central['id'],'-name','Grace','-role','Engineer','-department','DevOps','-presence','remote')
  assert sync()=={'sent':0,'received':1};assert next(e for e in store.employees() if e['id']==grace['id'])['department']=='DevOps'
  store.save_employee(grace['id'],'Grace','Architect','DevOps');assert sync()=={'sent':1,'received':0}
  cli('-action','employee-account-link','-id',remote['id'],'-account-id','9');assert sync()=={'sent':0,'received':0}
  store.employee_presence(ada['id'],'meeting');assert sync()=={'sent':1,'received':0}
  assert next(e for e in cli('-action','sync-employees') if e['id']==remote['id'])['accountEmployeeId']==9
  # Remote archival of an enabled local account must not log that account out.
  admin_remote=next(e for e in cli('-action','sync-employees') if e['name']=='Admin')
  cli('-action','central-employee-archive','-id',admin_remote['id'])
  try:sync();raise AssertionError('enabled local account was archived')
  except LocalStoreError as exc:assert 'aktives lokales Konto' in str(exc)
  assert any(e['name']=='Admin' for e in store.employees())
  archived=next(e for e in cli('-action','sync-employees') if e['id']==admin_remote['id'])
  restored={**archived,'active':True}
  cli('-action','sync-employee-event','-payload',json.dumps({'clientId':'test-recovery','operationId':'restore-admin','action':'update','employee':restored,'expected':archived}))
  assert sync()=={'sent':0,'received':0}
  cli('-action','central-employee-update','-id',remote['id'],'-name','Ada','-role','Developer','-department','Extern','-presence','office')
  store.save_employee(ada['id'],'Ada','Developer','Lokal')
  try:sync();raise AssertionError('conflict overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
  try:sync_employees(store,lock,lambda:True,client,url,'other',str(cert));raise AssertionError('changed server identity accepted')
  except LocalStoreError:pass
  assert next(e for e in store.employees() if e['id']==ada['id'])['department']=='Lokal'
  inspect=lambda:inspect_employee_conflict(store,lock,lambda:True,client,url,token,str(cert))
  resolve=lambda reviewed,server:resolve_employee_conflict(store,lock,lambda:True,client,url,token,str(cert),reviewed,server)
  reviewed=inspect();assert reviewed['preview']['local']['department']=='Lokal' and reviewed['remote']['department']=='Extern' and reviewed['remote']['accountEmployeeId']==9
  store.save_employee(ada['id'],'Ada','Developer','Lokal Zwei')
  try:resolve(reviewed,False);raise AssertionError('stale local review accepted')
  except LocalStoreError:pass
  reviewed=inspect();cli('-action','central-employee-update','-id',remote['id'],'-name','Ada','-role','Developer','-department','Extern Zwei','-presence','office')
  try:resolve(reviewed,False);raise AssertionError('stale server review accepted')
  except LocalStoreError:pass
  reviewed=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_employee_resolution BEFORE INSERT ON audit_log WHEN NEW.action='employees.sync.rebased' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
  try:resolve(reviewed,False);raise AssertionError('failed audit allowed resolution')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==2
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_resolutions').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_employee_resolution')
  resolve(reviewed,False)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
   assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_employee_sync_resolutions').fetchone()[0]))==2
  assert sync()=={'sent':1,'received':0}
  assert next(e for e in cli('-action','sync-employees') if e['id']==remote['id'])['department']=='Lokal Zwei'
  # A conflicting server archive cannot disable a local administrator account.
  admin=next(e for e in store.employees() if e['name']=='Admin')
  store.save_employee(admin['id'],'Admin','Administrator','Verwaltung')
  cli('-action','central-employee-archive','-id',admin_remote['id'])
  reviewed=inspect()
  try:resolve(reviewed,True);raise AssertionError('resolution archived active local account')
  except LocalStoreError as exc:assert 'lokalem Konto' in str(exc)
  assert any(e['id']==admin['id'] for e in store.employees())
  resolve(reviewed,False);assert sync()=={'sent':1,'received':0}
  store.save_employee(ada['id'],'Ada','Developer','Lokal Drei')
  store.save_employee(grace['id'],'Grace','Architect','Unabhängig')
  cli('-action','central-employee-update','-id',remote['id'],'-name','Ada','-role','Developer','-department','Server Wahl','-presence','office')
  reviewed=inspect();resolve(reviewed,True)
  assert next(e for e in store.employees() if e['id']==ada['id'])['department']=='Server Wahl'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
  assert sync()=={'sent':1,'received':0}
  assert next(e for e in cli('-action','sync-employees') if e['id']==central['id'])['department']=='Unabhängig'
  # A second workspace independently created the same employee. The reviewed
  # identity mapping preserves its local account and pending local leave.
  second_db=Path(tmp)/'second.db';second=LocalStore(second_db);second.create('Admin','admin2','Anderes Passwort 2026');second.open();second.login('admin2','Anderes Passwort 2026')
  second.create_leave(1900800000,1900900000,'Lokaler Urlaub bleibt')
  local_admin=second.session_employee();second_lock=threading.Lock()
  sync2=lambda:sync_employees(second,second_lock,lambda:True,client,url,token,str(cert))
  inspect2=lambda:inspect_employee_conflict(second,second_lock,lambda:True,client,url,token,str(cert))
  resolve2=lambda review,server:resolve_employee_conflict(second,second_lock,lambda:True,client,url,token,str(cert),review,server)
  try:sync2();raise AssertionError('duplicate employee silently accepted')
  except LocalStoreError as exc:assert '409' in str(exc)
  review=inspect2();assert review['identityConflict'] and review['remote']['id']==admin_remote['id'];original=review['operation']
  leave_id=second.leave()[0]['id']
  with sqlite3.connect(second_db) as sql:sql.execute("INSERT INTO sync_object_links VALUES('leave','external:leave:1',?)",(leave_id,))
  try:resolve2(review,False);raise AssertionError('linked leave reference ignored')
  except LocalStoreError:pass
  with sqlite3.connect(tmp+'/central.sqlite') as sql:assert sql.execute('SELECT count(*) FROM room_sync_receipts WHERE operation_id=?',(original['operationId'],)).fetchone()[0]==0
  with sqlite3.connect(second_db) as sql:
   sql.execute("DELETE FROM sync_object_links WHERE object_type='leave' AND local_id=?",(leave_id,))
   sql.execute('CREATE TABLE linux_demo_sync_shadow(id INTEGER,payload TEXT)')
   sql.execute('INSERT INTO linux_demo_sync_shadow VALUES(1,?)',(json.dumps({'employee':review['preview']['local']['id']}),))
  try:resolve2(review,False);raise AssertionError('existing linked reference ignored')
  except LocalStoreError:pass
  with sqlite3.connect(tmp+'/central.sqlite') as sql:assert sql.execute('SELECT count(*) FROM room_sync_receipts WHERE operation_id=?',(original['operationId'],)).fetchone()[0]==0
  with sqlite3.connect(second_db) as sql:sql.execute('DROP TABLE linux_demo_sync_shadow')
  second.save_room(0,'Besprechung Lokal','Berlin',4,'');local_room=second.rooms()[0]
  second.book(local_room['id'],1901000000,1901003600,1);booking=second.bookings(local_room['id'])[0]
  with sqlite3.connect(second_db) as sql:
   sql.execute('CREATE TABLE linux_booking_sync_shadow(booking_id INTEGER PRIMARY KEY,booking_json TEXT NOT NULL)')
   sql.execute('INSERT INTO linux_booking_sync_shadow VALUES(?,?)',(booking['id'],'{}'))
  try:resolve2(review,False);raise AssertionError('confirmed booking reference ignored')
  except LocalStoreError:pass
  with sqlite3.connect(tmp+'/central.sqlite') as sql:assert sql.execute('SELECT count(*) FROM room_sync_receipts WHERE operation_id=?',(original['operationId'],)).fetchone()[0]==0
  with sqlite3.connect(second_db) as sql:
   sql.execute('DROP TABLE linux_booking_sync_shadow')
   sql.execute("CREATE TRIGGER fail_employee_mapping BEFORE INSERT ON audit_log WHEN NEW.action='employees.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:resolve2(review,False);raise AssertionError('mapping without audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(second_db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
   assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(local_admin,)).fetchone()[0]==review['preview']['local']['id']
   sql.execute('DROP TRIGGER fail_employee_mapping')
  process.terminate();process.wait(timeout=5);process=start()
  resolve2(review,False)
  late=subprocess.run([str(client),'-action','sync-employee-event','-payload',json.dumps(original)],env=env,capture_output=True,text=True)
  assert late.returncode!=0 and '409' in late.stderr
  assert sync2()['sent'] in (0,1)
  assert second.session_employee()==local_admin and second.leave()[0]['reason']=='Lokaler Urlaub bleibt'
  assert second.bookings(local_room['id'])[0]['id']==booking['id']
  with sqlite3.connect(second_db) as sql:
   assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(local_admin,)).fetchone()[0]==admin_remote['id']
   assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_employee_sync_resolutions').fetchone()[0]))==1
  remote_new=cli('-action','central-employee-create','-name','Kim','-role','Technik','-department','Zentrale','-presence','office')
  second.save_employee(0,'Kim','Technik','Lokal');local_kim=next(v for v in second.employees() if v['name']=='Kim')
  try:sync2();raise AssertionError('second duplicate employee silently accepted')
  except LocalStoreError:pass
  review=inspect2();assert review['identityConflict'] and review['remote']['id']==remote_new['id']
  resolve2(review,True);assert next(v for v in second.employees() if v['id']==local_kim['id'])['department']=='Zentrale'
  assert sync2()['sent']==0
  second.close()
  print('Employee sync: TLS, offline retry, persistence, inbound, server account and conflicts OK')
 finally:
  store.close();process.terminate();process.wait(timeout=5)

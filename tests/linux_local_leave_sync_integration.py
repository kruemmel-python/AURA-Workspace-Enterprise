#!/usr/bin/python3
"""Test employee synchronization against a real TLS server and SQLite workspaces."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from leave_sync import sync_leave,inspect_leave_conflict,accept_server_leave,inspect_incoming_leave_conflict,accept_incoming_leave_conflict
from personal_presence_sync import sync_personal_presence
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-leave-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','leave.approve','leave.request']},'other':{'employeeId':8,'permissions':['leave.request']},'lead':{'employeeId':10,'permissions':['leave.approve']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  db=Path(tmp)/'local.db';admin=LocalStore(db);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
  admin.save_employee(0,'Ada','Developer','IT');ada=next(e for e in admin.employees() if e['name']=='Ada')
  lock=threading.Lock();employees=lambda:sync_employees(admin,lock,lambda:True,client,url,token,str(cert))
  employees();remote=cli('-action','sync-employees')
  for name,account_id in [('Admin',9),('Ada',8)]:
   entry=next(v for v in remote if v['name']==name);cli('-action','employee-account-link','-id',entry['id'],'-account-id',str(account_id))
  employees()
  with sqlite3.connect(db) as sql:
   sql.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'ada',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(ada['id'],))
   sql.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(ada['id'],))
  user=LocalStore(db);user.open();user.login('ada','Admin Passwort 2026')
  sync_admin=lambda:sync_leave(admin,lock,lambda:True,client,url,token,str(cert))
  sync_user=lambda:sync_leave(user,lock,lambda:True,client,url,'other',str(cert))
  try:sync_leave(user,lock,lambda:True,client,url,token,str(cert));raise AssertionError('wrong account accepted')
  except LocalStoreError:pass
  assert sync_user()=={'sent':0,'received':0,'unmapped':0}
  # The employee can send their own presence without directory-admin rights.
  # Unrelated administrator changes stay queued; a failed local acknowledgement
  # is retried after the server has already accepted the status.
  personal=lambda:sync_personal_presence(user,lock,lambda:True,client,url,'other',str(cert))
  admin.employee_presence(ada['id'],'away')
  try:personal();raise AssertionError('another person’s presence change was sent')
  except LocalStoreError as exc:assert 'anderen Person' in str(exc)
  assert employees()['sent']==1
  admin.save_employee(admin.session_employee(),'Admin','Administrator','Persönliche Testabteilung')
  user.employee_presence(ada['id'],'remote')
  try:sync_personal_presence(user,lock,lambda:True,client,url,token,str(cert));raise AssertionError('wrong server account accepted')
  except LocalStoreError:pass
  process.terminate();process.wait(timeout=5)
  try:personal();raise AssertionError('offline presence sync succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==2
  process=start()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_presence_ack BEFORE DELETE ON linux_employee_sync_outbox WHEN OLD.employee_id=%d BEGIN SELECT RAISE(ABORT,'ack failure'); END"%ada['id'])
  try:personal();raise AssertionError('ack failure not detected')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==2
   sql.execute('DROP TRIGGER fail_presence_ack')
  assert personal()=={'sent':1,'received':0}
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
  assert next(v for v in cli('-action','sync-employees') if v['name']=='Ada')['presence']=='remote'
  # A reassigned server account must never change another person's presence.
  user.employee_presence(ada['id'],'meeting')
  ada_remote=next(v for v in remote if v['name']=='Ada');admin_remote=next(v for v in remote if v['name']=='Admin')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','8')
  try:personal();raise AssertionError('remapped account accepted')
  except LocalStoreError as exc:assert 'anderen Mitarbeiter' in str(exc)
  assert next(v for v in cli('-action','sync-employees') if v['name']=='Admin')['presence']=='office'
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','9')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','8')
  assert personal()=={'sent':1,'received':0}
  # Receive a server-side change without affecting unrelated pending edits.
  user_env=env.copy();user_env['AURA_SYNC_TOKEN']='other'
  subprocess.run([str(client),'-action','set-my-presence','-status','meeting','-presence','away'],env=user_env,text=True,capture_output=True,check=True)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_presence_pull BEFORE INSERT ON audit_log WHEN NEW.action='employee.presence.received' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:personal();raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT presence FROM employees WHERE id=?',(ada['id'],)).fetchone()[0]=='meeting'
   sql.execute('DROP TRIGGER fail_presence_pull')
  assert personal()=={'sent':0,'received':1}
  assert next(v for v in user.employees() if v['isSelf'])['presence']=='away'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
  user.employee_presence(ada['id'],'meeting')
  subprocess.run([str(client),'-action','set-my-presence','-status','away','-presence','office'],env=user_env,text=True,capture_output=True,check=True)
  try:personal();raise AssertionError('conflicting server status overwritten')
  except LocalStoreError:pass
  assert next(v for v in user.employees() if v['isSelf'])['presence']=='meeting'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==2
  subprocess.run([str(client),'-action','set-my-presence','-status','office','-presence','away'],env=user_env,text=True,capture_output=True,check=True)
  assert personal()=={'sent':1,'received':0}
  assert employees()['sent']==1
  # A failing audit write must roll back both the request and its outbox entry.
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_leave_create_audit BEFORE INSERT ON audit_log WHEN NEW.action='leave_request.created' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:user.create_leave(1900100000,1900150000,'Nicht gespeichert');raise AssertionError('request survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM leave_requests WHERE reason='Nicht gespeichert'").fetchone()[0]==0
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_leave_create_audit')
  process.terminate();process.wait(timeout=5)
  user.create_leave(1900200000,1900300000,'Urlaub Ada');request=user.leave()[0]
  try:sync_user();raise AssertionError('offline sync succeeded')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_leave_decision_audit BEFORE INSERT ON audit_log WHEN NEW.action='leave_request.approved' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:admin.decide_leave(request['id'],True);raise AssertionError('decision survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM leave_requests WHERE id=?',(request['id'],)).fetchone()[0]=='pending'
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_leave_decision_audit')
  admin.decide_leave(request['id'],True)
  process=start()
  try:inspect_leave_conflict(user,lock,lambda:True,client,url,'other',str(cert));raise AssertionError('other actor operation could be removed')
  except LocalStoreError as exc:assert 'anderen Person' in str(exc)
  try:sync_admin();raise AssertionError('decision sent before request')
  except LocalStoreError as exc:assert 'Antragsteller' in str(exc)
  # A stale local account mapping must not attribute Ada's queued request to Admin.
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','8')
  try:sync_user();raise AssertionError('request accepted after account reassignment')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:
   frozen=json.loads(sql.execute("SELECT wire_json FROM linux_leave_sync_outbox WHERE action='request'").fetchone()[0])
   assert frozen['employeeExternalId']==ada_remote['id']
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','9')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','8')
  assert sync_user()=={'sent':1,'received':0,'unmapped':0}
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  try:sync_admin();raise AssertionError('decision accepted after approver unlinked')
  except LocalStoreError as exc:assert '409' in str(exc)
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','9')
  assert sync_admin()=={'sent':1,'received':0,'unmapped':0}
  assert sync_user()=={'sent':0,'received':0,'unmapped':0}
  result=cli('-action','leave');assert len(result)==1 and result[0]['status']=='approved' and result[0]['employeeId']==8 and result[0]['decidedBy']==9
  # A former approver account may not silently become a different decider.
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','9')
  process.terminate();process.wait(timeout=5);process=start()
  assert not cli('-action','leave')[0].get('deciderExternalId')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','9')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','8')
  # A request entered centrally by Ada is imported with local identity mapping.
  userenv=env.copy();userenv['AURA_SYNC_TOKEN']='other'
  created=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1900400000','-ends-at','1900500000','-reason','Zentral'],env=userenv,capture_output=True,text=True,check=True).stdout)
  # A stale local directory must not attach incoming requests to the wrong person.
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','8')
  process.terminate();process.wait(timeout=5);process=start()
  mismatch=sync_admin();assert mismatch['received']==0 and mismatch['unmapped']>=1
  assert not any(x['reason']=='Zentral' for x in json.loads(subprocess.run([str(client),'-action','leave'],env=userenv,capture_output=True,text=True,check=True).stdout))
  denied=subprocess.run([str(client),'-action','leave-decision','-id',created['id'],'-status','approved'],env=env,capture_output=True,text=True)
  assert denied.returncode!=0 and '409' in denied.stderr
  with sqlite3.connect(db) as sql:assert sql.execute("SELECT count(*) FROM leave_requests WHERE reason='Zentral'").fetchone()[0]==0
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',admin_remote['id'],'-account-id','9')
  cli('-action','employee-account-link','-id',ada_remote['id'],'-account-id','8')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_leave_receive_audit BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.received' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:sync_user();raise AssertionError('received leave survived audit failure')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute("SELECT count(*) FROM leave_requests WHERE reason='Zentral'").fetchone()[0]==0
   sql.execute('DROP TRIGGER fail_leave_receive_audit')
  assert sync_user()=={'sent':0,'received':1,'unmapped':0}
  local=next(v for v in user.leave() if v['reason']=='Zentral');assert local['employeeId']==ada['id']
  with sqlite3.connect(db) as sql:assert sql.execute("SELECT count(*) FROM audit_log WHERE action='leave.sync.received' AND subject=?",(str(local['id'])+':pending',)).fetchone()[0]==1
  cli('-action','leave-decision','-id',created['id'],'-status','rejected')
  assert sync_user()=={'sent':0,'received':1,'unmapped':0}
  assert next(v for v in user.leave() if v['id']==local['id'])['status']=='rejected'
  # Simulate a crash after the server accepted a request but before local acknowledgement.
  user.create_leave(1900600000,1900700000,'Wiederholung')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_leave_ack BEFORE DELETE ON linux_leave_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  try:sync_user();raise AssertionError('ack failure not detected')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_leave_ack')
   sql.execute("CREATE TRIGGER fail_leave_sent_audit BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.sent' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
  try:sync_user();raise AssertionError('sent audit failure accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_leave_sent_audit')
  assert sync_user()=={'sent':1,'received':0,'unmapped':0}
  assert len([v for v in cli('-action','leave') if v['reason']=='Wiederholung'])==1
  # A competing server decision must never be overwritten by the queued decision.
  local=next(v for v in admin.leave() if v['reason']=='Wiederholung');admin.decide_leave(local['id'],True)
  server_request=next(v for v in cli('-action','leave') if v['reason']=='Wiederholung')
  # A pending server request cannot safely replace an in-flight local decision.
  try:inspect_leave_conflict(admin,lock,lambda:True,client,url,token,str(cert));raise AssertionError('pending server state was accepted for conflict resolution')
  except LocalStoreError as exc:assert 'noch offen' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
  cli('-action','leave-decision','-id',server_request['id'],'-status','rejected')
  try:sync_admin();raise AssertionError('conflicting decision overwritten')
  except LocalStoreError as exc:assert '409' in str(exc)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
  assert next(v for v in cli('-action','leave') if v['id']==server_request['id'])['status']=='rejected'
  inspect=lambda:inspect_leave_conflict(admin,lock,lambda:True,client,url,token,str(cert))
  accept=lambda reviewed:accept_server_leave(admin,lock,lambda:True,client,url,token,str(cert),reviewed)
  reviewed=inspect();assert reviewed['preview']['local']['status']=='approved' and reviewed['remote']['status']=='rejected'
  modified=json.loads(json.dumps(reviewed));modified['remote']['status']='approved'
  try:accept(modified);raise AssertionError('stale review accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_leave_resolution BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:accept(reviewed);raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==1
   assert not sql.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_leave_sync_resolutions'").fetchone()
   sql.execute('DROP TRIGGER fail_leave_resolution')
  accept(reviewed)
  assert next(v for v in admin.leave() if v['id']==local['id'])['status']=='rejected'
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==0
   assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_leave_sync_resolutions').fetchone()[0]))==1
  assert sync_admin()=={'sent':0,'received':0,'unmapped':0}
  # An unsent request from one actor must not block the manager's decision
  # on a different request that is already known to the server.
  created=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1900800000','-ends-at','1900900000','-reason','Unabhängig'],env=userenv,capture_output=True,text=True,check=True).stdout)
  sync_user()
  independent=next(v for v in admin.leave() if v['reason']=='Unabhängig')
  user.create_leave(1901000000,1901100000,'Noch nicht gesendet')
  blocked=next(v for v in admin.leave() if v['reason']=='Noch nicht gesendet')
  admin.decide_leave(blocked['id'],True);admin.decide_leave(independent['id'],False)
  subprocess.run([str(client),'-action','create-leave','-starts-at','1901000000','-ends-at','1901050000','-reason','Unabhängig empfangen'],env=userenv,capture_output=True,text=True,check=True)
  try:sync_admin();raise AssertionError('blocked decision silently skipped')
  except LocalStoreError as exc:assert 'Antragsteller' in str(exc)
  assert any(v['reason']=='Unabhängig empfangen' for v in admin.leave())
  assert next(v for v in cli('-action','leave') if v['id']==created['id'])['status']=='rejected'
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]==2
  assert sync_user()['sent']==1
  assert sync_admin()['sent']==1
  # The Windows sender uses shared employee object IDs, which can differ
  # from both Linux database IDs and authenticated server account numbers.
  ada_external=next(v['id'] for v in remote if v['name']=='Ada')
  payload={'id':'windows:leave:71','employeeExternalId':ada_external,'deciderExternalId':'','startsAt':1901200000,'endsAt':1901300000,'reason':'Windows Antrag','status':'pending'}
  event={'topic':'desktop.leave.requested','payload':json.dumps(payload)}
  req=urllib.request.Request(url+'/api/v1/desktop-leave-events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer other','Content-Type':'application/json'})
  assert urllib.request.urlopen(req,context=ctx,timeout=5).status==202
  assert sync_user()['received']==1
  local=next(v for v in user.leave() if v['reason']=='Windows Antrag');assert local['employeeId']==ada['id']
  admin.decide_leave(local['id'],True);assert sync_admin()['sent']==1
  server_leave=next(v for v in cli('-action','leave') if v['id']=='windows:leave:71');assert server_leave['employeeId']==8 and server_leave['decidedBy']==9 and server_leave['status']=='approved'
  # A lost acknowledgement must not wedge the sender after a decision.
  assert urllib.request.urlopen(req,context=ctx,timeout=5).status==202
  user.create_leave(1901400000,1901500000,'Ubuntu Antrag')
  assert sync_user()['sent']==1
  request_on_server=next(v for v in cli('-action','leave') if v['reason']=='Ubuntu Antrag')
  manager_external=next(v['id'] for v in remote if v['name']=='Admin')
  payload={'id':request_on_server['id'],'employeeExternalId':ada_external,'deciderExternalId':manager_external,'startsAt':1901400000,'endsAt':1901500000,'reason':'Ubuntu Antrag','status':'rejected'}
  event={'topic':'desktop.leave.decided','payload':json.dumps(payload)}
  decision=urllib.request.Request(url+'/api/v1/desktop-leave-events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  assert urllib.request.urlopen(decision,context=ctx,timeout=5).status==202
  assert sync_user()['received']==1
  assert next(v for v in user.leave() if v['reason']=='Ubuntu Antrag')['status']=='rejected'
  # A historical desktop-local numeric ID is not a verified server account.
  legacy={'id':'legacy-pc:leave:42','employeeId':8,'startsAt':1901600000,'endsAt':1901700000,'reason':'Historisch unklar','status':'pending','decidedBy':0}
  event={'topic':'leave.requested','payload':json.dumps(legacy)}
  req=urllib.request.Request(url+'/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer other','Content-Type':'application/json'})
  assert urllib.request.urlopen(req,context=ctx,timeout=5).status==202
  assert not any(v['reason']=='Historisch unklar' for v in json.loads(subprocess.run([str(client),'-action','leave'],env=user_env,text=True,capture_output=True,check=True).stdout))
  assert sync_admin()['unmapped']==1
  assert not any(v['reason']=='Historisch unklar' for v in admin.leave())
  assert next(v for v in cli('-action','leave') if v['reason']=='Historisch unklar')['identityVerified'] is False
  denied=subprocess.run([str(client),'-action','leave-decision','-id','legacy-pc:leave:42','-status','approved'],env=env,text=True,capture_output=True)
  assert denied.returncode!=0 and '409' in denied.stderr
  # Simulate a historical local import that was previously attributed to the
  # wrong account. It must not silently move when Central is reconciled.
  wrong={**legacy,'employeeId':9}
  with sqlite3.connect(db) as sql:
   sql.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
   wrong_id=sql.execute("INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason,status,decided_by,active) VALUES(?,?,?,?,?,0,1)",(admin.session_employee(),legacy['startsAt'],legacy['endsAt'],legacy['reason'],'pending')).lastrowid
   sql.execute("INSERT INTO sync_object_links VALUES('leave',?,?)",(legacy['id'],wrong_id))
   sql.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?)',(wrong_id,json.dumps(wrong)))
   sql.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
  mapping={'expected':legacy,'employeeExternalId':ada_remote['id'],'deciderExternalId':''}
  cli('-action','legacy-leave-reconcile','-payload',json.dumps(mapping))
  try:sync_admin();raise AssertionError('historical wrong owner silently replaced')
  except LocalStoreError as exc:assert 'Mitarbeiterzuordnung' in str(exc)
  inspect_incoming=lambda:inspect_incoming_leave_conflict(admin,lock,lambda:True,client,url,token,str(cert))
  accept_incoming=lambda value:accept_incoming_leave_conflict(admin,lock,lambda:True,client,url,token,str(cert),value)
  try:inspect_incoming_leave_conflict(user,lock,lambda:True,client,url,'other',str(cert));raise AssertionError('unprivileged review accepted')
  except LocalStoreError:pass
  reviewed=inspect_incoming();assert reviewed['preview']['localEmployeeName']=='Admin' and reviewed['preview']['remoteEmployeeName']=='Ada'
  altered=json.loads(json.dumps(reviewed));altered['preview']['remote']['employeeId']=9
  try:accept_incoming(altered);raise AssertionError('changed review accepted')
  except LocalStoreError:pass
  cli('-action','leave-decision','-id',legacy['id'],'-status','approved')
  try:accept_incoming(reviewed);raise AssertionError('stale server decision accepted')
  except LocalStoreError:pass
  reviewed=inspect_incoming();assert reviewed['preview']['remote']['status']=='approved'
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER fail_incoming_leave BEFORE INSERT ON audit_log WHEN NEW.action='leave.sync.reconciled_incoming' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  try:accept_incoming(reviewed);raise AssertionError('failed audit accepted')
  except LocalStoreError:pass
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT employee_id FROM leave_requests WHERE id=?',(wrong_id,)).fetchone()[0]==admin.session_employee()
   sql.execute('DROP TRIGGER fail_incoming_leave')
  accept_incoming(reviewed)
  assert sync_user()['received']==0
  mapped=next(v for v in user.leave() if v['reason']=='Historisch unklar');assert mapped['id']==wrong_id and mapped['employeeId']==ada['id'] and mapped['status']=='approved'
  # A previously imported contradictory decision also requires review.
  decided={'id':'legacy-pc:leave:43','employeeId':77,'startsAt':1901800000,'endsAt':1901900000,'reason':'Historische Entscheidung','status':'approved','decidedBy':55}
  event={'topic':'leave.decided','payload':json.dumps(decided)}
  req=urllib.request.Request(url+'/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  assert urllib.request.urlopen(req,context=ctx,timeout=5).status==202
  cli('-action','legacy-leave-reconcile','-payload',json.dumps({'expected':decided,'employeeExternalId':ada_remote['id'],'deciderExternalId':admin_remote['id']}))
  wrong_decision={**decided,'employeeId':8,'decidedBy':9,'status':'rejected'}
  with sqlite3.connect(db) as sql:
   sql.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
   decided_id=sql.execute("INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason,status,decided_by,active) VALUES(?,?,?,?,?,?,1)",(ada['id'],decided['startsAt'],decided['endsAt'],decided['reason'],'rejected',admin.session_employee())).lastrowid
   sql.execute("INSERT INTO sync_object_links VALUES('leave',?,?)",(decided['id'],decided_id))
   sql.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?)',(decided_id,json.dumps(wrong_decision)))
   sql.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
  try:sync_admin();raise AssertionError('conflicting local decision silently replaced')
  except LocalStoreError as exc:assert 'Entscheidung' in str(exc)
  decision_review=inspect_incoming();assert decision_review['preview']['local']['status']=='rejected' and decision_review['preview']['remote']['status']=='approved'
  accept_incoming(decision_review)
  assert next(v for v in user.leave() if v['id']==decided_id)['status']=='approved'
  process.terminate();process.wait(timeout=5);process=start()
  assert next(v for v in cli('-action','leave') if v['reason']=='Historisch unklar')['identityVerified'] is True
  # A leave-only local and server role can decide without directory management.
  admin.save_employee(0,'Urlaubsleitung','Teamleitung','IT');lead=next(e for e in admin.employees() if e['name']=='Urlaubsleitung')
  admin.save_role(0,'leave_manager','Urlaubsfreigabe','Nur Urlaub','leave.approve');admin.assign_role(lead['id'],'leave_manager')
  employees();lead_remote=next(v for v in cli('-action','sync-employees') if v['name']=='Urlaubsleitung')
  cli('-action','employee-account-link','-id',lead_remote['id'],'-account-id','10');employees()
  with sqlite3.connect(db) as sql:sql.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'lead',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(lead['id'],))
  manager=LocalStore(db);manager.open();manager.login('lead','Admin Passwort 2026')
  user.create_leave(1902000000,1902100000,'Urlaub mit Einzelrecht');sync_user()
  entry=next(v for v in manager.leave() if v['reason']=='Urlaub mit Einzelrecht');manager.decide_leave(entry['id'],True)
  assert sync_leave(manager,lock,lambda:True,client,url,'lead',str(cert))['sent']==1
  assert next(v for v in cli('-action','leave') if v['reason']=='Urlaub mit Einzelrecht')['decidedBy']==10
  manager.close()
  user.close();admin.close();print('Leave sync: identity mapping, offline queue, ordered decisions and inbound updates: OK')
 finally:
  process.terminate();process.wait(timeout=5)

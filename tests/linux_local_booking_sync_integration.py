#!/usr/bin/python3
"""Exercise authenticated booking outbox against a real TLS server."""
import json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from room_sync import sync_rooms
from booking_sync import sync_bookings,inspect_booking_conflict,accept_server_booking_conflict,apply_snapshot,encode
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-booking-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':'manager','AURA_SYNC_IDENTITIES':json.dumps({'manager':{'employeeId':9,'permissions':['employees.manage','rooms.manage','sync.events.write']},'ada':{'employeeId':8,'permissions':['rooms.book']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 ctx=ssl.create_default_context(cafile=str(cert))
 def start():
  p=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if p.poll() is not None:raise RuntimeError(p.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();return p
   except OSError:time.sleep(.05)
  raise AssertionError('server did not start')
 def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,capture_output=True,text=True,check=True).stdout)
 def reject(fn):
  try:fn()
  except LocalStoreError:return
  raise AssertionError('unexpected success')
 lock=threading.Lock();ok=lambda:True;db=Path(tmp)/'local.db';p=start()
 try:
  admin=LocalStore(db);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
  admin.save_employee(0,'Ada','Entwicklerin','IT');ada=next(v for v in admin.employees() if v['name']=='Ada');admin.save_room(0,'Konferenz','Berlin',6,'')
  employees=lambda:sync_employees(admin,lock,ok,client,url,'manager',str(cert))
  employees();directory=cli('-action','sync-employees')
  for name,id in [('Admin',9),('Ada',8)]:cli('-action','employee-account-link','-id',next(v['id'] for v in directory if v['name']==name),'-account-id',str(id))
  employees();sync_rooms(admin,lock,ok,client,url,'manager',str(cert));room=admin.rooms()[0]['id']
  admin.create_account(ada['id'],'ada','Ada Passwort 2026');user=LocalStore(db);user.open();user.login('ada','Ada Passwort 2026');user.change_password('Ada Passwort 2026','Ada Eigenes Passwort 2026')
  syncadmin=lambda:sync_bookings(admin,lock,ok,client,url,'manager',str(cert))
  syncuser=lambda:sync_bookings(user,lock,ok,client,url,'ada',str(cert))
  admin.block_room(room,1900100000,1900103600,'Vor Einrichtung')
  assert syncadmin()=={'sent':1,'received':0,'unmapped':0};reject(lambda:sync_bookings(user,lock,ok,client,url,'manager',str(cert)))
  assert any(v['reason']=='Vor Einrichtung' and v['status']=='blocked' for v in cli('-action','sync-bookings'))
  p.terminate();p.wait(timeout=5);user.book(room,1900000000,1900003600,2);booking=next(v for v in user.bookings(room) if v['startsAt']==1900000000)
  reject(syncuser)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_audit BEFORE INSERT ON audit_log WHEN NEW.action='booking.cancelled' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:admin.cancel_booking(room,booking['id']))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM bookings WHERE id=?',(booking['id'],)).fetchone()[0]=='confirmed'
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER reject_booking_audit')
  admin.cancel_booking(room,booking['id'])
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT action,actor_id FROM linux_booking_sync_outbox ORDER BY seq').fetchall()==[('create',ada['id']),('cancel',admin.session_employee())]
  # An unrelated admin booking can be sent while Ada's earlier booking still
  # blocks the admin's cancellation of that specific booking.
  admin.book(room,1900900000,1900903600,1)
  p=start()
  remote_room=cli('-action','rooms')[0]['id']
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900950000','-ends-at','1900953600','-attendees','1')
  reject(syncadmin)
  assert any(v['startsAt']==1900900000 for v in cli('-action','sync-bookings'))
  assert any(v['startsAt']==1900950000 for v in admin.bookings(room))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT action,actor_id FROM linux_booking_sync_outbox ORDER BY seq').fetchall()==[('create',ada['id']),('cancel',admin.session_employee())]
   sql.execute("CREATE TRIGGER reject_booking_ack BEFORE DELETE ON linux_booking_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  reject(syncuser)
  with sqlite3.connect(db) as sql:
   sql.execute('DROP TRIGGER reject_booking_ack')
   sql.execute("CREATE TRIGGER reject_booking_sent_audit BEFORE INSERT ON audit_log WHEN NEW.action='booking.sync.sent' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(syncuser)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox WHERE actor_id=?',(ada['id'],)).fetchone()[0]==1
   sql.execute('DROP TRIGGER reject_booking_sent_audit')
  assert syncuser()=={'sent':1,'received':0,'unmapped':0};assert syncadmin()=={'sent':1,'received':0,'unmapped':0}
  result=cli('-action','sync-bookings');assert len(result)==4 and any(v['status']=='cancelled' and v['userId']==8 for v in result)
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==0
  remote_room=cli('-action','rooms')[0]['id']
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900010000','-ends-at','1900013600','-attendees','2')
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_receive_audit BEFORE INSERT ON audit_log WHEN NEW.action='booking.sync.received' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(syncadmin)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM bookings WHERE starts_at=1900010000').fetchone()[0]==0
   sql.execute('DROP TRIGGER reject_booking_receive_audit')
  assert syncadmin()=={'sent':0,'received':1,'unmapped':0}
  remote=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900010000);local=next(v for v in admin.bookings(room) if v['startsAt']==1900010000)
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',remote['clientId'],'-booking-id',str(remote['bookingId']),'-starts-at','1900020000','-ends-at','1900023600','-attendees','3')
  assert syncadmin()=={'sent':0,'received':1,'unmapped':0};assert next(v for v in admin.bookings(room) if v['id']==local['id'])['startsAt']==1900020000
  admin.reschedule(room,local['id'],1900030000,1900033600,2);assert syncadmin()=={'sent':1,'received':0,'unmapped':0}
  assert next(v for v in cli('-action','sync-bookings') if v['bookingId']==remote['bookingId'])['startsAt']==1900030000
  cli('-action','central-block-room','-room-key',remote_room,'-starts-at','1900040000','-ends-at','1900043600','-reason','Wartung')
  assert syncadmin()=={'sent':0,'received':1,'unmapped':0};assert next(v for v in admin.bookings(room) if v['startsAt']==1900040000)['reason']=='Wartung'
  # A reason-only change must also count as an incoming change and be visible.
  snapshot=cli('-action','sync-bookings')
  changed=json.loads(json.dumps(snapshot));next(v for v in changed if v['startsAt']==1900040000)['reason']='Neue Wartung'
  with sqlite3.connect(db) as sql:
   client_id=sql.execute('SELECT client_id FROM linux_booking_sync_config').fetchone()[0]
   assert apply_snapshot(sql,changed,client_id)==(1,0)
  assert next(v for v in admin.bookings(room) if v['startsAt']==1900040000)['reason']=='Neue Wartung'
  assert syncadmin()=={'sent':0,'received':1,'unmapped':0}
  reject(lambda:admin.book(room,1900040200,1900040300,1))
  def post(path,body):
   req=urllib.request.Request(url+path,data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer manager','Content-Type':'application/json'})
   return urllib.request.urlopen(req,context=ctx,timeout=5).status
  assert post('/bookings/reserve',{'clientId':'windows-old','reservationId':-99,'roomId':77,'roomKey':remote_room,'startsAt':1900060000,'endsAt':1900063600})==204
  assert post('/bookings/commit',{'clientId':'windows-old','reservationId':-99,'bookingId':77,'topic':'booking.created'})==202
  assert syncadmin()=={'sent':0,'received':1,'unmapped':1}
  legacy=next(v for v in admin.bookings(room) if v['startsAt']==1900060000);assert legacy['userId']==0
  reject(lambda:admin.reschedule(room,legacy['id'],1900070000,1900073600,1));reject(lambda:admin.book(room,1900060100,1900060200,1))
  legacy_remote=next(v for v in cli('-action','sync-bookings') if v['clientId']=='windows-old' and v['bookingId']==77)
  ada_key=next(v['id'] for v in cli('-action','sync-employees') if v['name']=='Ada')
  reconcile={'expected':legacy_remote,'employeeExternalId':ada_key,'attendees':2}
  unprivileged=env.copy();unprivileged['AURA_SYNC_TOKEN']='ada'
  denied=subprocess.run([str(client),'-action','legacy-booking-reconcile','-payload',json.dumps(reconcile)],env=unprivileged,capture_output=True,text=True)
  assert denied.returncode!=0 and '403' in denied.stderr
  stale=json.loads(json.dumps(reconcile));stale['expected']['endsAt']+=1
  denied=subprocess.run([str(client),'-action','legacy-booking-reconcile','-payload',json.dumps(stale)],env=env,capture_output=True,text=True)
  assert denied.returncode!=0 and '409' in denied.stderr
  resolved=cli('-action','legacy-booking-reconcile','-payload',json.dumps(reconcile));assert resolved['userId']==8 and resolved['attendees']==2 and resolved['roomId']==1
  assert cli('-action','legacy-booking-reconcile','-payload',json.dumps(reconcile))==resolved
  forged=subprocess.run([str(client),'-action','publish-event','-topic','central.booking.identity.reconciled','-payload','{}'],env=env,capture_output=True,text=True)
  assert forged.returncode!=0 and '403' in forged.stderr
  p.terminate();p.wait(timeout=5);p=start()
  assert cli('-action','legacy-booking-reconcile','-payload',json.dumps(reconcile))==resolved
  assert syncadmin()['received']==1
  mapped=next(v for v in admin.bookings(room) if v['id']==legacy['id']);assert mapped['userId']==ada['id'] and mapped['attendees']==2
  user.reschedule(room,legacy['id'],1900400000,1900403600,2);assert syncuser()['sent']==1
  assert next(v for v in cli('-action','sync-bookings') if v['clientId']=='windows-old')['startsAt']==1900400000
  reject(lambda:user.block_room(room,1900090000,1900093600,'Nicht erlaubt'))
  with sqlite3.connect(db) as sql:client_id=sql.execute('SELECT client_id FROM linux_booking_sync_config').fetchone()[0]
  bad={'clientId':client_id,'operationId':'unauthorized-block','action':'block','booking':{'clientId':client_id,'bookingId':98765,'roomId':1,'roomKey':remote_room,'userId':8,'attendees':1,'status':'blocked','reason':'Verboten','startsAt':1900110000,'endsAt':1900113600},'expected':None}
  adaenv=env.copy();adaenv['AURA_SYNC_TOKEN']='ada'
  result=subprocess.run([str(client),'-action','sync-booking-event','-payload',json.dumps(bad)],env=adaenv,capture_output=True,text=True)
  assert result.returncode!=0 and '403' in result.stderr
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_block_audit BEFORE INSERT ON audit_log WHEN NEW.action='room.blocked' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:admin.block_room(room,1900090000,1900093600,'Rollback'))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM bookings WHERE starts_at=1900090000').fetchone()[0]==0
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==0
   sql.execute('DROP TRIGGER reject_block_audit')
  p.terminate();p.wait(timeout=5);admin.block_room(room,1900090000,1900093600,'Lokale Wartung')
  assert next(v for v in admin.bookings(room) if v['startsAt']==1900090000)['reason']=='Lokale Wartung'
  reject(syncadmin);p=start()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_block_ack BEFORE DELETE ON linux_booking_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  reject(syncadmin)
  with sqlite3.connect(db) as sql:sql.execute('DROP TRIGGER reject_block_ack')
  assert syncadmin()=={'sent':1,'received':0,'unmapped':0}
  assert len([v for v in cli('-action','sync-bookings') if v['startsAt']==1900090000])==1
  blocked=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900090000)
  assert blocked['status']=='blocked' and blocked['reason']=='Lokale Wartung' and blocked['userId']==9
  # The server can move one booking and reuse its old slot. The feed is
  # ordered by client identity, so the new central booking may arrive first.
  admin.book(room,1900120000,1900123600,1);assert syncadmin()['sent']==1
  moved=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900120000)
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',moved['clientId'],'-booking-id',str(moved['bookingId']),'-starts-at','1900130000','-ends-at','1900133600','-attendees','1')
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900120000','-ends-at','1900123600','-attendees','1')
  assert syncadmin()['received']==2
  assert len([v for v in admin.bookings(room) if v['startsAt'] in (1900120000,1900130000) and v['status']=='confirmed'])==2
  user.book(room,1900050000,1900053600,1)
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900050100','-ends-at','1900050200','-attendees','1')
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',moved['clientId'],'-booking-id',str(moved['bookingId']),'-starts-at','1900140000','-ends-at','1900143600','-attendees','1')
  reject(syncadmin)
  assert any(v['startsAt']==1900130000 and v['status']=='confirmed' for v in admin.bookings(room))
  assert not any(v['startsAt']==1900140000 for v in admin.bookings(room))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM bookings WHERE starts_at=1900050100').fetchone()[0]==0
   assert sql.execute('SELECT suppressed FROM linux_booking_sync_guard').fetchone()[0]==0
  reject(syncuser)
  inspect=lambda:inspect_booking_conflict(user,lock,ok,client,url,'ada',str(cert))
  accept=lambda reviewed:accept_server_booking_conflict(user,lock,ok,client,url,'ada',str(cert),reviewed)
  reject(lambda:inspect_booking_conflict(admin,lock,ok,client,url,'manager',str(cert)))
  review=inspect();assert review['preview']['local']['startsAt']==1900050000 and len(review['conflicts'])==1
  changed=json.loads(json.dumps(review));changed['conflicts'][0]['startsAt']+=1;reject(lambda:accept(changed))
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900080000','-ends-at','1900083600','-attendees','1')
  reject(lambda:accept(review));review=inspect()
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_resolution BEFORE INSERT ON audit_log WHEN NEW.action='booking.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:accept(review))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==1
   assert sql.execute('SELECT status FROM bookings WHERE starts_at=1900050000').fetchone()[0]=='confirmed'
   assert not sql.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_booking_sync_resolutions'").fetchone()
   sql.execute('DROP TRIGGER reject_booking_resolution')
  accept(review)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==0
   assert sql.execute('SELECT status FROM bookings WHERE starts_at=1900050000').fetchone()[0]=='cancelled'
   assert sql.execute('SELECT status FROM bookings WHERE starts_at=1900050100').fetchone()[0]=='confirmed'
   assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_booking_sync_resolutions').fetchone()[0]))==1
  assert syncuser()['sent']==0
  conflict=review['conflicts'][0]
  cli('-action','central-cancel-booking','-room-key',remote_room,'-booking-client',conflict['clientId'],'-booking-id',str(conflict['bookingId']))
  adaenv=env.copy();adaenv['AURA_SYNC_TOKEN']='ada';original=encode(review['preview']['creation'])
  def repeat(action):return subprocess.run([str(client),'-action',action,'-payload',original],env=adaenv,capture_output=True,text=True)
  assert repeat('abandon-booking-event').returncode==0
  denied=repeat('sync-booking-event');assert denied.returncode!=0 and '409' in denied.stderr
  p.terminate();p.wait(timeout=5);p=start()
  assert repeat('abandon-booking-event').returncode==0
  denied=repeat('sync-booking-event');assert denied.returncode!=0 and '409' in denied.stderr
  # A centrally changed, already linked booking can be resolved by accepting
  # the reviewed server version; failed local commits must leave the queue intact.
  syncadmin()
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900160000','-ends-at','1900163600','-attendees','1')
  syncadmin()
  target=next(v for v in admin.bookings(room) if v['startsAt']==1900160000)
  remote=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900160000)
  admin.reschedule(room,target['id'],1900170000,1900173600,1)
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',remote['clientId'],'-booking-id',str(remote['bookingId']),'-starts-at','1900180000','-ends-at','1900183600','-attendees','2')
  reject(syncadmin)
  inspect_admin=lambda:inspect_booking_conflict(admin,lock,ok,client,url,'manager',str(cert))
  accept_admin=lambda reviewed:accept_server_booking_conflict(admin,lock,ok,client,url,'manager',str(cert),reviewed)
  reviewed=inspect_admin();assert reviewed['preview']['action']=='reschedule' and reviewed['conflicts'][0]['startsAt']==1900180000
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_resolution2 BEFORE INSERT ON audit_log WHEN NEW.action='booking.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:accept_admin(reviewed))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT starts_at FROM bookings WHERE id=?',(target['id'],)).fetchone()[0]==1900170000
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER reject_booking_resolution2')
  accept_admin(reviewed)
  assert next(v for v in admin.bookings(room) if v['id']==target['id'])['startsAt']==1900180000
  original_update=encode(reviewed['preview']['creation'])
  denied=subprocess.run([str(client),'-action','sync-booking-event','-payload',original_update],env=env,capture_output=True,text=True)
  assert denied.returncode!=0 and '409' in denied.stderr
  assert syncadmin()['sent']==0
  # The first pending create can conflict even after the local user moves it.
  user.book(room,1900200000,1900203600,1)
  later=next(v for v in user.bookings(room) if v['startsAt']==1900200000)
  user.reschedule(room,later['id'],1900210000,1900213600,1)
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900200100','-ends-at','1900200200','-attendees','1')
  reject(syncuser)
  review=inspect();assert review['preview']['local']['startsAt']==1900210000 and review['preview']['creation']['booking']['startsAt']==1900200000 and len(review['preview']['pending'])==2
  accept(review)
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM bookings WHERE id=?',(later['id'],)).fetchone()[0]=='cancelled'
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox WHERE booking_id=?',(later['id'],)).fetchone()[0]==0
  # A stale local cancellation can likewise be discarded in favour of a
  # centrally moved reservation, preserving the same linked booking.
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900220000','-ends-at','1900223600','-attendees','1')
  syncadmin()
  target=next(v for v in admin.bookings(room) if v['startsAt']==1900220000)
  remote=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900220000)
  admin.cancel_booking(room,target['id'])
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',remote['clientId'],'-booking-id',str(remote['bookingId']),'-starts-at','1900230000','-ends-at','1900233600','-attendees','1')
  reject(syncadmin)
  reviewed=inspect_admin();assert reviewed['preview']['action']=='cancel'
  accept_admin(reviewed)
  restored=next(v for v in admin.bookings(room) if v['id']==target['id']);assert restored['startsAt']==1900230000 and restored['status']=='confirmed'
  # A detailed Windows reservation retains a verified account and can be
  # edited from Ubuntu using the original remote booking identity.
  assert syncadmin()['sent']==0
  ada_key=next(v['id'] for v in cli('-action','sync-employees') if v['name']=='Ada')
  def ada_post(path,body):
   req=urllib.request.Request(url+path,data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer ada','Content-Type':'application/json'})
   return urllib.request.urlopen(req,context=ctx,timeout=5).status
  reserve={'clientId':'windows-details','reservationId':-800,'bookingId':0,'roomId':77,'roomKey':remote_room,'startsAt':1900800000,'endsAt':1900803600,'employeeKey':ada_key,'attendees':3}
  assert ada_post('/bookings/reserve',reserve)==204
  assert ada_post('/bookings/commit',{'clientId':'windows-details','reservationId':-800,'bookingId':80,'topic':'booking.created'})==202
  assert syncuser()['received']==1
  windows_booking=next(v for v in user.bookings(room) if v['startsAt']==1900800000)
  assert windows_booking['userId']==ada['id'] and windows_booking['attendees']==3
  user.reschedule(room,windows_booking['id'],1900810000,1900813600,2)
  assert syncuser()['sent']==1
  moved=next(v for v in cli('-action','sync-bookings') if v['clientId']=='windows-details' and v['bookingId']==80)
  assert moved['startsAt']==1900810000 and moved['userId']==8 and moved['attendees']==2
  user.cancel_booking(room,windows_booking['id']);assert syncuser()['sent']==1
  p.terminate();p.wait(timeout=5);p=start()
  cancelled=next(v for v in cli('-action','sync-bookings') if v['clientId']=='windows-details' and v['bookingId']==80)
  assert cancelled['status']=='cancelled' and cancelled['userId']==8 and cancelled['attendees']==2
  user.close();admin.close();print('Booking sync: actor attribution, offline ordering, rollback, conflict resolution and acknowledgement retry: OK')
 finally:
  p.terminate();p.wait(timeout=5)

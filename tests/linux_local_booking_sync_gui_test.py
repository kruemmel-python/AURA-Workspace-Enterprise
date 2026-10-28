#!/usr/bin/python3
"""Exercise authenticated booking outbox against a real TLS server."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from datetime import datetime
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from room_sync import sync_rooms
from booking_sync import sync_bookings
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-booking-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':'manager','AURA_SYNC_IDENTITIES':json.dumps({'manager':{'employeeId':9,'permissions':['employees.manage','rooms.manage']},'ada':{'employeeId':8,'permissions':['rooms.book']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  assert syncadmin()=={'sent':0,'received':0,'unmapped':0};reject(lambda:sync_bookings(user,lock,ok,client,url,'manager',str(cert)))
  p.terminate();p.wait(timeout=5);user.book(room,1900000000,1900003600,2);booking=user.bookings(room)[0]
  reject(syncuser)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_audit BEFORE INSERT ON audit_log WHEN NEW.action='booking.cancelled' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:admin.cancel_booking(room,booking['id']))
  with sqlite3.connect(db) as sql:
   assert sql.execute('SELECT status FROM bookings WHERE id=?',(booking['id'],)).fetchone()[0]=='confirmed'
   assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER reject_booking_audit')
  admin.cancel_booking(room,booking['id'])
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT action,actor_id FROM linux_booking_sync_outbox ORDER BY seq').fetchall()==[('create',ada['id']),('cancel',admin.session_employee())]
  p=start();reject(syncadmin)
  with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER reject_booking_ack BEFORE DELETE ON linux_booking_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  reject(syncuser)
  with sqlite3.connect(db) as sql:sql.execute('DROP TRIGGER reject_booking_ack')
  assert syncuser()=={'sent':1,'received':0,'unmapped':0};assert syncadmin()=={'sent':1,'received':0,'unmapped':0}
  result=cli('-action','sync-bookings');assert len(result)==1 and result[0]['status']=='cancelled' and result[0]['userId']==8
  with sqlite3.connect(db) as sql:assert sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]==0
  remote_room=cli('-action','rooms')[0]['id']
  cli('-action','central-block-room','-room-key',remote_room,'-starts-at','1900020000','-ends-at','1900023600','-reason','GUI Wartung')
  def post(path,body):
   req=urllib.request.Request(url+path,data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer manager','Content-Type':'application/json'})
   return urllib.request.urlopen(req,context=ctx,timeout=5).status
  admin_key=next(v['id'] for v in cli('-action','sync-employees') if v['name']=='Admin')
  assert post('/bookings/reserve',{'clientId':'windows-gui-test','reservationId':-88,'bookingId':0,'roomId':55,'roomKey':remote_room,'startsAt':1900800000,'endsAt':1900803600,'employeeKey':admin_key,'attendees':3})==204
  assert post('/bookings/commit',{'clientId':'windows-gui-test','reservationId':-88,'bookingId':88,'topic':'booking.created'})==202
  assert post('/bookings/reserve',{'clientId':'windows-legacy-gui','reservationId':-89,'bookingId':0,'roomId':55,'roomKey':remote_room,'startsAt':1900750000,'endsAt':1900753600})==204
  assert post('/bookings/commit',{'clientId':'windows-legacy-gui','reservationId':-89,'bookingId':89,'topic':'booking.created'})==202
  user.close();admin.close();os.environ['AURA_LOCAL_DB']=str(db)
  spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  w=mod.AuraWindow();w.show_all()
  def children(widget,cls):
   result=[widget] if isinstance(widget,cls) else []
   if isinstance(widget,mod.Gtk.Container):
    for c in widget.get_children():result.extend(children(c,cls))
   return result
  def wait(test):
   end=time.monotonic()+20
   while time.monotonic()<end:
    while mod.Gtk.events_pending():mod.Gtk.main_iteration()
    if test():return
    time.sleep(.02)
   raise AssertionError('GUI timeout')
  def fill(title,values):
   def submit():
    ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()==title]
    if not ds:return True
    entries=children(ds[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
    for e,v in zip(entries,values):e.set_text(v)
    ds[-1].response(mod.Gtk.ResponseType.OK);return False
   mod.GLib.idle_add(submit)
  def reply(title,response):
   def action():
    ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')==title]
    if not ds:return True
    if title=='Buchungen senden?':assert 'Überschneidungen' in ds[-1].get_property('secondary-text')
    ds[-1].response(response);return False
   return action
  fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode)
  local_room=w.local.rooms()[0]
  with w.local_lock:w.local.book(local_room['id'],1900010000,1900013600,2)
  w.show_bookings(local_room);wait(lambda:any(v.get_label()=='Buchungen synchronisieren' for v in children(w.content,mod.Gtk.Button)))
  fill('Buchungen zum Server senden',[url,'manager',str(cert)]);mod.GLib.timeout_add(300,reply('Buchungen senden?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(600,reply('Buchungsübertragung abgeschlossen',mod.Gtk.ResponseType.CLOSE))
  next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Buchungen synchronisieren').clicked();wait(lambda:not w.room_sync_running)
  result=cli('-action','sync-bookings');assert len(result)==5 and any(v['userId']==9 and v['status']=='confirmed' for v in result)
  assert any(v['status']=='blocked' and v.get('reason')=='GUI Wartung' for v in w.local.bookings(local_room['id']))
  legacy=next(v for v in w.local.bookings(room) if v['startsAt']==1900750000);assert legacy['userId']==0
  def select_person():
   dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Buchungszuordnung klären']
   if not dialogs:return True
   assert children(dialogs[-1].get_content_area(),mod.Gtk.ComboBoxText)[0].set_active_id(admin_key)
   children(dialogs[-1].get_content_area(),mod.Gtk.SpinButton)[0].set_value(2)
   dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
  fill('Buchungszuordnung prüfen',[url,'manager',str(cert)])
  mod.GLib.timeout_add(300,select_person);mod.GLib.timeout_add(600,reply('Buchungszuordnung bestätigen?',mod.Gtk.ResponseType.OK))
  w.reconcile_legacy_booking(local_room,legacy);wait(lambda:not w.room_sync_running)
  mapped=next(v for v in w.local.bookings(room) if v['id']==legacy['id']);assert mapped['userId']==w.local.session_employee() and mapped['attendees']==2
  wait(lambda:any('Buchungen automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
  w.booking_auto_interval=1;next(v for v in children(w.content,mod.Gtk.CheckButton) if 'Buchungen automatisch' in v.get_label()).set_active(True)
  wait(lambda:not w.room_sync_running);assert w.booking_auto_enabled
  with w.local_lock:
   w.local.save_room(0,'Neuer Raum','Berlin',4,'')
   new_room=next(v for v in w.local.rooms() if v['name']=='Neuer Raum')['id']
   w.local.book(new_room,1901100000,1901103600,1)
   w.local.book(room,1901200000,1901203600,1)
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1901300000','-ends-at','1901303600','-attendees','1')
  wait(lambda:'Raumabgleich' in w.booking_auto_error and not w.room_sync_running)
  assert w.booking_auto_enabled and any(v['startsAt']==1901300000 for v in w.local.bookings(room))
  assert any(v['startsAt']==1901200000 for v in cli('-action','sync-bookings'))
  w.room_sync_running=True
  try:sync_rooms(w.local,w.local_lock,lambda:True,client,url,'manager',str(cert))
  finally:w.room_sync_running=False
  wait(lambda:not w.room_sync_running and any(v['startsAt']==1901100000 for v in cli('-action','sync-bookings')))
  assert w.booking_auto_enabled
  windows_booking=next(v for v in w.local.bookings(room) if v['startsAt']==1900800000)
  assert windows_booking['userId']==w.local.session_employee() and windows_booking['attendees']==3
  start_text=datetime.fromtimestamp(1900810000).strftime('%Y-%m-%d %H:%M');end_text=datetime.fromtimestamp(1900813600).strftime('%Y-%m-%d %H:%M')
  expected_start=int(datetime.strptime(start_text,'%Y-%m-%d %H:%M').timestamp())
  fill('Buchung ändern',[start_text,end_text,'2']);w.booking_form(local_room,windows_booking)
  wait(lambda:any(v['id']==windows_booking['id'] and v['startsAt']==expected_start for v in w.local.bookings(room)))
  wait(lambda:any(v.get('clientId')=='windows-gui-test' and v['startsAt']==expected_start and v['attendees']==2 for v in cli('-action','sync-bookings')) and not w.room_sync_running)
  fill('Sperrzeit anlegen',['2031-04-10 10:00','2031-04-10 11:00','GUI Lokale Wartung']);w.block_room(local_room)
  wait(lambda:any(v.get('reason')=='GUI Lokale Wartung' for v in w.local.bookings(room)))
  wait(lambda:any(v.get('reason')=='GUI Lokale Wartung' for v in cli('-action','sync-bookings')) and not w.room_sync_running)
  p.terminate();p.wait(timeout=5)
  with w.local_lock:w.local.book(room,1900030000,1900033600,1)
  wait(lambda:'vorübergehend' in w.booking_auto_error and not w.room_sync_running);assert w.booking_auto_enabled
  p=start()
  def pending():
   with sqlite3.connect(db) as sql:return sql.execute('SELECT count(*) FROM linux_booking_sync_outbox').fetchone()[0]
  wait(lambda:pending()==0 and not w.room_sync_running);assert any(v['startsAt']==1900030000 for v in cli('-action','sync-bookings'))
  w.room_sync_running=True
  with w.local_lock:w.local.book(room,1900040000,1900043600,1)
  cli('-action','central-book','-room-key',remote_room,'-starts-at','1900040100','-ends-at','1900040200','-attendees','1')
  w.room_sync_running=False
  wait(lambda:not w.booking_auto_enabled and not w.room_sync_running);assert pending()==1 and '409' in w.booking_auto_error
  wait(lambda:any(v.get_label()=='Buchungskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
  def resolve(response):
   fill('Buchungskonflikt prüfen',[url,'manager',str(cert)]);mod.GLib.timeout_add(300,reply('Buchungskonflikt entscheiden',response))
   next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Buchungskonflikt prüfen').clicked();wait(lambda:not w.room_sync_running)
  resolve(mod.Gtk.ResponseType.CANCEL);assert pending()==1
  mod.GLib.timeout_add(500,reply('Lokale Buchung stornieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(800,reply('Servertermine übernommen',mod.Gtk.ResponseType.CLOSE))
  resolve(mod.Gtk.ResponseType.OK);assert pending()==0
  records=w.local.bookings(room);assert any(v['startsAt']==1900040000 and v['status']=='cancelled' for v in records) and any(v['startsAt']==1900040100 and v['status']=='confirmed' for v in records)
  # Resolve a stale edit using the GUI, including the distinct confirmation.
  target=next(v for v in w.local.bookings(room) if v['startsAt']==1900030000)
  remote=next(v for v in cli('-action','sync-bookings') if v['startsAt']==1900030000)
  with w.local_lock:w.local.reschedule(room,target['id'],1900060000,1900063600,1)
  cli('-action','central-reschedule','-room-key',remote_room,'-booking-client',remote['clientId'],'-booking-id',str(remote['bookingId']),'-starts-at','1900070000','-ends-at','1900073600','-attendees','1')
  reject(lambda:sync_bookings(w.local,w.local_lock,ok,client,url,'manager',str(cert)))
  wait(lambda:any(v.get_label()=='Buchungskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
  mod.GLib.timeout_add(500,reply('Serverbuchung lokal übernehmen?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(800,reply('Servertermine übernommen',mod.Gtk.ResponseType.CLOSE))
  resolve(mod.Gtk.ResponseType.OK);assert pending()==0
  assert next(v for v in w.local.bookings(room) if v['id']==target['id'])['startsAt']==1900070000
  wait(lambda:any('Buchungen automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
  w.room_sync_running=True
  next(v for v in children(w.content,mod.Gtk.CheckButton) if 'Buchungen automatisch' in v.get_label()).set_active(True);assert w.booking_auto_enabled
  fill('Serververbindung',[url,'manager',str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.booking_auto_enabled and not w.booking_auto_source
  w.room_sync_running=False
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
  with w.local_lock:
   if w.local:w.local.close()
  print('GTK booking bidirectional synchronization: OK')
 finally:
  p.terminate();p.wait(timeout=5)

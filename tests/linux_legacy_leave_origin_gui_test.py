#!/usr/bin/python3
"""Confirm an old account-based leave origin through GTK and a real server."""
import importlib.util,json,os,socket,sqlite3,ssl,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-leave-origin-') as tmp:
 cert,key=Path(tmp)/'cert.pem',Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 with sqlite3.connect(tmp+'/central.sqlite') as db:
  db.execute('CREATE TABLE central_events(id INTEGER PRIMARY KEY AUTOINCREMENT,topic TEXT NOT NULL,payload TEXT NOT NULL,occurred_at TEXT NOT NULL)')
  old={'id':'legacy-account:leave:1','employeeId':8,'startsAt':1900000000,'endsAt':1900003600,'reason':'Historischer Urlaub','status':'pending','identityKind':'server-account-v1'}
  db.execute('INSERT INTO central_events(topic,payload,occurred_at) VALUES(?,?,?)',('leave.requested',json.dumps(old),'2024-01-01T00:00:00Z'))
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='origin-manager'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','leave.approve']},'employee':{'employeeId':8,'permissions':['leave.request']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 server=subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 try:
  ctx=ssl.create_default_context(cafile=str(cert))
  for _ in range(100):
   if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not start')
  def cli(*args):return json.loads(subprocess.run([str(root/'KR_BUILD/linux-x64/aura-workspace-enterprise'),*args],env=env,capture_output=True,text=True,check=True).stdout)
  employee=cli('-action','central-employee-create','-name','Ada','-role','Team','-department','IT','-presence','office')
  cli('-action','employee-account-link','-id',employee['id'],'-account-id','8')
  other=cli('-action','central-employee-create','-name','Mira','-role','Leitung','-department','IT','-presence','office')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','9')
  os.environ.update(env)
  spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  w=mod.AuraWindow();w.show_all()
  def children(widget,cls):
   result=[widget] if isinstance(widget,cls) else []
   if isinstance(widget,mod.Gtk.Container):
    for child in widget.get_children():result.extend(children(child,cls))
   return result
  def text(widget):return ' '.join(v.get_text() for v in children(widget,mod.Gtk.Label))
  def wait(test):
   end=time.monotonic()+15
   while time.monotonic()<end:
    while mod.Gtk.events_pending():mod.Gtk.main_iteration()
    if test():return
    time.sleep(.02)
   raise AssertionError('GUI timeout: '+text(w.content))
  w.select(3);wait(lambda:'Historischer Urlaub' in text(w.content))
  cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Historischer Urlaub' in text(v)]
  assert len(cards)==1 and 'Ursprüngliche Zuordnung ungeklärt' in text(cards[0])
  assert not any(v.get_label() in ('Genehmigen','Ablehnen') for v in children(cards[0],mod.Gtk.Button))
  selected=[other['id']]
  def choose():
   dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Urlaubszuordnung klären']
   if not dialogs:return True
   combo=children(dialogs[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id(selected[0]);dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
  def confirm():
   dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Urlaubszuordnung bestätigen?']
   if not dialogs:return True
   assert ('Mira' if selected[0]==other['id'] else 'Ada') in dialogs[-1].get_property('secondary-text');dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
  errors=[];prior_message=w.message;w.message=lambda value:errors.append(value)
  mod.GLib.idle_add(choose);mod.GLib.idle_add(confirm)
  next(v for v in children(cards[0],mod.Gtk.Button) if v.get_label()=='Zuordnung klären').clicked()
  wait(lambda:bool(errors));assert '409' in errors[-1]
  assert cli('-action','leave')[0]['identityOriginVerified'] is False
  w.message=prior_message;selected[0]=employee['id']
  mod.GLib.idle_add(choose);mod.GLib.idle_add(confirm)
  next(v for v in children(cards[0],mod.Gtk.Button) if v.get_label()=='Zuordnung klären').clicked()
  wait(lambda:cli('-action','leave')[0].get('identityOriginVerified') is True)
  wait(lambda:'Ursprüngliche Zuordnung ungeklärt' not in text(w.content))
  cli('-action','employee-account-link','-id',employee['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','8')
  w.refresh();wait(lambda:'Antragsteller-Zuordnung geändert oder nicht verfügbar' in text(w.content))
  card=next(v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Historischer Urlaub' in text(v))
  assert 'Mira' not in text(card)
  assert not any(v.get_label() in ('Genehmigen','Ablehnen') for v in children(card,mod.Gtk.Button))
  cli('-action','employee-account-link','-id',other['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',employee['id'],'-account-id','8')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','9')
  cli('-action','leave-decision','-id',old['id'],'-status','approved')
  carol=cli('-action','central-employee-create','-name','Carol','-role','Team','-department','IT','-presence','office')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',carol['id'],'-account-id','9')
  w.refresh();wait(lambda:'Entscheidend: Zuordnung geändert oder nicht verfügbar' in text(w.content))
  card=next(v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Historischer Urlaub' in text(v))
  assert 'Carol' not in text(card) and 'Ada' in text(card)
  cli('-action','employee-account-link','-id',carol['id'],'-account-id','0')
  cli('-action','employee-account-link','-id',other['id'],'-account-id','9')
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
  server.terminate();server.wait(timeout=5)
  server=subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not restart')
  assert cli('-action','leave')[0].get('identityOriginVerified') is True
 finally:
  server.terminate();server.wait(timeout=5)
print('GTK verified legacy leave origin confirmation: OK')

#!/usr/bin/python3
"""Exercise assignment synchronization via the GTK administration controls."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-ticket-gui-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage','tasks.manage','tickets.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 try:
  ctx=ssl.create_default_context(cafile=str(cert))
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not start')
  def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,text=True,capture_output=True,check=True).stdout)
  os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None);os.environ.pop('AURA_SYNC_CA_CERT',None)
  spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  import threading
  from employee_sync import sync_employees
  from project_sync import sync_projects
  from task_sync import sync_tasks
  store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock()
  sync_employees(store,lock,lambda:True,client,url,token,str(cert));staff=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',staff['id'],'-account-id','9');sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  actor=store.session_employee();store.save_project(0,'GUI Projekt','IT','','planned',actor);store.save_task(0,'GUI Aufgabe','IT','Mittel',actor);project=store.projects()[0]['id'];task=store.tasks()[0]['id'];store.assign_project(task,project);store.save_ticket(0,'GUI Ticket','IT','Mittel',actor)
  sync_projects(store,lock,lambda:True,client,url,token,str(cert));sync_tasks(store,lock,lambda:True,client,url,token,str(cert));store.close()
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
  def respond(title,response):
   def submit():
    ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')==title]
    if not ds:return True
    ds[-1].response(response);return False
   return submit
  fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode);w.select(5)
  def sync():
   fill('Ticketabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Lokale Tickets synchronisieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Ticketabgleich abgeschlossen',mod.Gtk.ResponseType.CLOSE))
   [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Tickets synchronisieren'][0].clicked();wait(lambda:not w.ticket_sync_running)
  sync();remote=cli('-action','sync-tickets')[0];assert remote['title']=='GUI Ticket'
  ticket=next(v['id'] for v in w.local.tickets() if v['title']=='GUI Ticket');w.local.save_ticket(ticket,'Lokal','IT','Hoch',actor)
  cli('-action','update-ticket','-id',remote['id'],'-title','Server','-category','IT','-priority','Mittel','-assignee-id','9')
  fill('Ticketkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Ticketkonflikt entscheiden',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Lokales Ticket vorgemerkt',mod.Gtk.ResponseType.CLOSE))
  [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Ticketkonflikt prüfen'][0].clicked();wait(lambda:not w.ticket_sync_running);sync()
  assert cli('-action','sync-tickets')[0]['title']=='Lokal'
  fill('Ticketkommentare abgleichen',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Ticketkommentare synchronisieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Ticketkommentare abgeglichen',mod.Gtk.ResponseType.CLOSE))
  [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Ticketkommentare synchronisieren'][0].clicked();wait(lambda:not w.ticket_sync_running)
  w.ticket_auto_interval=1;w.ticket_comment_auto_interval=1
  comment_auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Ticketkommentare automatisch' in v.get_label()][0];comment_auto.set_active(True);wait(lambda:not w.ticket_sync_running)
  w.local.save_ticket(0,'Noch offen','IT','Mittel',actor);pending=next(v['id'] for v in w.local.tickets() if v['title']=='Noch offen');w.local.add_comment('ticket',pending,'Kommentar wartet')
  wait(lambda:'warten auf den Abgleich' in w.ticket_comment_auto_error and not w.ticket_sync_running);assert w.ticket_comment_auto_enabled
  ticket_auto=[v for v in children(w.content,mod.Gtk.CheckButton) if v.get_label().startswith('Tickets automatisch')][0];ticket_auto.set_active(True)
  wait(lambda:not w.ticket_sync_running and len(cli('-action','sync-ticket-comments'))==1);assert w.ticket_auto_enabled and w.ticket_comment_auto_enabled
  with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:
   legacy=sql.execute("INSERT INTO tickets(title,category,priority,status,assignee_id,active) VALUES('Altes Ticket','IT','Mittel','open',0,1)").lastrowid
  w.local.save_ticket(ticket,'Unabhängig gesendet','IT','Hoch',actor)
  cli('-action','create-ticket','-title','Unabhängig empfangen','-category','IT','-priority','Mittel','-assignee-id','9')
  wait(lambda:'zuständigen Person' in w.ticket_auto_error and not w.ticket_sync_running)
  assert w.ticket_auto_enabled and any(v['title']=='Unabhängig empfangen' for v in w.local.tickets())
  assert next(v for v in cli('-action','sync-tickets') if v['id']==remote['id'])['title']=='Unabhängig gesendet'
  w.local.save_ticket(legacy,'Altes Ticket zugeordnet','IT','Mittel',actor)
  wait(lambda:not w.ticket_sync_running and any(v['title']=='Altes Ticket zugeordnet' for v in cli('-action','sync-tickets')));assert w.ticket_auto_enabled
  process.terminate();process.wait(timeout=5);w.local.save_ticket(ticket,'Offline Ticket','IT','Hoch',actor);w.local.add_comment('ticket',ticket,'Offline Kommentar')
  wait(lambda:'vorübergehend' in w.ticket_auto_error and 'vorübergehend' in w.ticket_comment_auto_error and not w.ticket_sync_running)
  process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not restart')
  wait(lambda:not w.ticket_sync_running and len(cli('-action','sync-ticket-comments'))==2 and any(x['title']=='Offline Ticket' for x in cli('-action','sync-tickets')))
  assert w.ticket_auto_enabled and w.ticket_comment_auto_enabled
  ticket_auto=[v for v in children(w.content,mod.Gtk.CheckButton) if v.get_label().startswith('Tickets automatisch')][0];ticket_auto.set_active(False)
  w.local.save_ticket(ticket,'Lokaler Konflikt','IT','Hoch',actor);cli('-action','update-ticket','-id',remote['id'],'-title','Serverkonflikt','-category','IT','-priority','Mittel','-assignee-id','9')
  ticket_auto.set_active(True);wait(lambda:not w.ticket_auto_enabled and not w.ticket_sync_running);assert '409' in w.ticket_auto_error
  fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode)
  assert not w.ticket_auto_enabled and not w.ticket_auto_source and not w.ticket_comment_auto_enabled and not w.ticket_comment_auto_source
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy();print('GTK ticket synchronization, conflicts and automatic offline recovery: OK')
 finally:process.terminate();process.wait(timeout=5)

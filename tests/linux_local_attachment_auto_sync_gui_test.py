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
  from ticket_sync import sync_tickets
  store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');lock=threading.Lock()
  sync_employees(store,lock,lambda:True,client,url,token,str(cert));staff=cli('-action','sync-employees')[0];cli('-action','employee-account-link','-id',staff['id'],'-account-id','9');sync_employees(store,lock,lambda:True,client,url,token,str(cert))
  actor=store.session_employee();store.save_project(0,'GUI Projekt','IT','','planned',actor);store.save_task(0,'GUI Aufgabe','IT','Mittel',actor);project=store.projects()[0]['id'];task=store.tasks()[0]['id'];store.assign_project(task,project);store.save_ticket(0,'GUI Ticket','IT','Mittel',actor);ticket=store.tickets()[0]['id'];source=Path(tmp)/'beleg.txt';source.write_text('GUI Anhang');store.import_attachment('ticket',ticket,source);store.import_attachment('task',task,source)
  sync_projects(store,lock,lambda:True,client,url,token,str(cert));sync_tasks(store,lock,lambda:True,client,url,token,str(cert));sync_tickets(store,lock,lambda:True,client,url,token,str(cert));store.close()
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
  def send(kind,label):
   fill(label+' abgleichen',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond(label+' synchronisieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond(label+' abgeglichen',mod.Gtk.ResponseType.CLOSE))
   [v for v in children(w.content,mod.Gtk.Button) if v.get_label()==label+' synchronisieren'][0].clicked()
   wait(lambda:not (w.task_sync_running if kind=='task' else w.ticket_sync_running))
   result=cli('-action','sync-attachments','-subject-type',kind);assert len(result)==1 and result[0]['originalName']=='beleg.txt' and result[0]['uploadedBy']==staff['id']
  send('task','Aufgabenanhänge');send('ticket','Ticketanhänge')
  for kind in ('task','ticket'):w.attachment_auto[kind]['interval']=1
  def check(label):return next(v for v in children(w.content,mod.Gtk.CheckButton) if v.get_label().startswith(label))
  check('Aufgabenanhänge automatisch').set_active(True);check('Ticketanhänge automatisch').set_active(True)
  wait(lambda:not w.task_sync_running and not w.ticket_sync_running)
  w.local.save_task(0,'Neue Aufgabe','IT','Mittel',actor);new_task=next(x['id'] for x in w.local.tasks() if x['title']=='Neue Aufgabe')
  w.local.save_ticket(0,'Neues Ticket','IT','Mittel',actor);new_ticket=next(x['id'] for x in w.local.tickets() if x['title']=='Neues Ticket')
  w.local.import_attachment('task',new_task,source);w.local.import_attachment('ticket',new_ticket,source)
  wait(lambda:all('warten auf den Abgleich' in w.attachment_auto[k]['error'] for k in ('task','ticket')) and not w.task_sync_running and not w.ticket_sync_running)
  w.task_auto_interval=1;w.ticket_auto_interval=1;check('Aufgaben automatisch').set_active(True);check('Tickets automatisch').set_active(True)
  wait(lambda:not w.task_sync_running and not w.ticket_sync_running and len(cli('-action','sync-attachments','-subject-type','task'))==2 and len(cli('-action','sync-attachments','-subject-type','ticket'))==2)
  check('Aufgaben automatisch').set_active(False);check('Tickets automatisch').set_active(False)
  process.terminate();process.wait(timeout=5)
  offline=Path(tmp)/'offline.txt';offline.write_text('Offline')
  w.local.import_attachment('task',task,offline);w.local.import_attachment('ticket',ticket,offline)
  wait(lambda:all('vorübergehend' in w.attachment_auto[k]['error'] for k in ('task','ticket')) and not w.task_sync_running and not w.ticket_sync_running)
  process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not restart')
  wait(lambda:not w.task_sync_running and not w.ticket_sync_running and len(cli('-action','sync-attachments','-subject-type','task'))==3 and len(cli('-action','sync-attachments','-subject-type','ticket'))==3)
  broken=Path(tmp)/'defekt.txt';broken.write_text('korrekt');w.local.import_attachment('task',task,broken)
  item=next(x for x in w.local.attachments('task',task) if x['name']=='defekt.txt');Path(item['storedPath']).write_text('manipul')
  wait(lambda:not w.attachment_auto['task']['enabled'] and not w.task_sync_running);assert 'Integritätsprüfung' in w.attachment_auto['task']['error']
  fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode)
  assert all(not w.attachment_auto[k]['enabled'] and not w.attachment_auto[k]['source'] and not w.attachment_auto[k]['retry'] for k in ('task','ticket'))
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy();print('GTK automatic attachments: waiting, offline retry, integrity stop and session cleanup OK')
 finally:process.terminate();process.wait(timeout=5)

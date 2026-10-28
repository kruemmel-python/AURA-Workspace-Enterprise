#!/usr/bin/python3
"""Exercise assignment synchronization via the GTK administration controls."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-ticket-gui-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage','tasks.manage','tickets.manage']},'other':{'employeeId':8,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  actor=store.session_employee();store.save_project(0,'GUI Projekt','IT','','planned',actor);store.save_task(0,'GUI Aufgabe','IT','Mittel',actor);project=store.projects()[0]['id'];task=store.tasks()[0]['id'];store.assign_project(task,project);store.save_ticket(0,'GUI Ticket','IT','Mittel',actor);store.save_organization(0,'GUI Organisation','GUI',actor)
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
   fill('Organisationsabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Organisationseinheiten synchronisieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Organisationsabgleich abgeschlossen',mod.Gtk.ResponseType.CLOSE))
   [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Organisationseinheiten synchronisieren'][0].clicked();wait(lambda:not w.organization_sync_running)
  sync();remote=cli('-action','sync-organizations')[0];assert remote['name']=='GUI Organisation'
  unit=next(v['id'] for v in w.local.organizations() if v['code']=='GUI');w.local.save_organization(unit,'Lokal','GUI',actor)
  cli('-action','central-organization-update','-id',remote['id'],'-name','Server','-code','GUI','-manager-id','9')
  fill('Organisationskonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Organisationskonflikt entscheiden',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Lokale Organisation vorgemerkt',mod.Gtk.ResponseType.CLOSE))
  [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Organisationskonflikt prüfen'][0].clicked();wait(lambda:not w.organization_sync_running);sync()
  assert cli('-action','sync-organizations')[0]['name']=='Lokal'
  w.organization_auto_interval=1;w.employee_auto_interval=1
  def check(label):return next(v for v in children(w.content,mod.Gtk.CheckButton) if v.get_label().startswith(label))
  check('Organisationseinheiten automatisch').set_active(True);wait(lambda:not w.organization_sync_running)
  w.local.save_employee(0,'Neue Leitung','IT','IT');manager=next(v['id'] for v in w.local.employees() if v['name']=='Neue Leitung')
  w.local.save_organization(0,'Neue Einheit','NEU',manager)
  wait(lambda:'wartet auf den Mitarbeiterabgleich' in w.organization_auto_error and not w.organization_sync_running);assert w.organization_auto_enabled
  w.employee_sync_running=True
  try:
   sync_employees(w.local,w.local_lock,lambda:w.local_mode,client,url,token,str(cert))
   person=next(v for v in cli('-action','sync-employees') if v['name']=='Neue Leitung');cli('-action','employee-account-link','-id',person['id'],'-account-id','8')
   sync_employees(w.local,w.local_lock,lambda:w.local_mode,client,url,token,str(cert))
  finally:w.employee_sync_running=False
  check('Mitarbeiter automatisch').set_active(True)
  wait(lambda:not w.organization_sync_running and not w.employee_sync_running and any(v['code']=='NEU' and v['managerId']==8 for v in cli('-action','sync-organizations')))
  w.local.save_employee(manager,'Neue Leitung','IT','Support');w.local.save_organization(unit,'Auto Lokal','GUI',actor)
  wait(lambda:not w.organization_sync_running and not w.employee_sync_running and any(v['name']=='Auto Lokal' for v in cli('-action','sync-organizations')) and any(v['department']=='Support' for v in cli('-action','sync-employees') if v['id']==person['id']))
  check('Mitarbeiter automatisch').set_active(False)
  process.terminate();process.wait(timeout=5);w.local.save_organization(unit,'Offline','GUI',actor)
  wait(lambda:'vorübergehend' in w.organization_auto_error and not w.organization_sync_running);assert w.organization_auto_enabled
  process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not restart')
  wait(lambda:not w.organization_sync_running and any(v['name']=='Offline' for v in cli('-action','sync-organizations')))
  check('Organisationseinheiten automatisch').set_active(False);w.local.save_organization(unit,'Lokal Konflikt','GUI',actor)
  cli('-action','central-organization-update','-id',remote['id'],'-name','Server Konflikt','-code','GUI','-manager-id','9')
  check('Organisationseinheiten automatisch').set_active(True);wait(lambda:not w.organization_auto_enabled and not w.organization_sync_running);assert '409' in w.organization_auto_error
  fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.organization_auto_source and not w.organization_auto_retry and not w.employee_auto_retry
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy();print('GTK organization sync: manager wait, auto recovery, conflict and session cleanup OK')
 finally:process.terminate();process.wait(timeout=5)

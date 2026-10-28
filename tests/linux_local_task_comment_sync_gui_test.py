#!/usr/bin/python3
"""Exercise assignment synchronization via the GTK administration controls."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-comment-gui-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage','tasks.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
  actor=store.session_employee();store.save_project(0,'GUI Projekt','IT','','planned',actor);store.save_task(0,'GUI Aufgabe','IT','Mittel',actor);project=store.projects()[0]['id'];task=store.tasks()[0]['id'];store.assign_project(task,project);store.add_comment('task',task,'Hallo GTK')
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
  fill('Aufgabenkommentare abgleichen',[url,token,str(cert)]);mod.GLib.timeout_add(300,respond('Aufgabenkommentare synchronisieren?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(700,respond('Aufgabenkommentare abgeglichen',mod.Gtk.ResponseType.CLOSE))
  [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Aufgabenkommentare synchronisieren'][0].clicked();wait(lambda:not w.task_sync_running)
  comments=cli('-action','sync-task-comments');assert len(comments)==1 and comments[0]['body']=='Hallo GTK' and comments[0]['authorId']==staff['id'] and comments[0]['importedBy']==9
  w.comment_auto_interval=1
  auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Aufgabenkommentare automatisch' in v.get_label()][0];auto.set_active(True);wait(lambda:not w.task_sync_running);assert w.comment_auto_enabled
  w.local.save_task(0,'Neue Aufgabe','IT','Mittel',actor);new_task=next(v['id'] for v in w.local.tasks() if v['title']=='Neue Aufgabe');w.local.add_comment('task',new_task,'Wartet')
  wait(lambda:'warten auf den Abgleich' in w.comment_auto_error and not w.task_sync_running);assert w.comment_auto_enabled
  sync_tasks(w.local,w.local_lock,lambda:w.local_mode,client,url,token,str(cert))
  wait(lambda:not w.task_sync_running and len(cli('-action','sync-task-comments'))==2);assert w.comment_auto_enabled
  process.terminate();process.wait(timeout=5);w.local.add_comment('task',task,'Offline GTK')
  wait(lambda:'vorübergehend' in w.comment_auto_error and not w.task_sync_running);assert w.comment_auto_enabled
  process=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
   except OSError:time.sleep(.05)
  else:raise AssertionError('server did not restart')
  wait(lambda:not w.task_sync_running and len(cli('-action','sync-task-comments'))==3);assert w.comment_auto_enabled
  # A modified immutable comment must stop the timer instead of hiding the inconsistency.
  with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:sql.execute("UPDATE comments SET body='Manipuliert' WHERE body='Hallo GTK'")
  wait(lambda:not w.comment_auto_enabled and not w.task_sync_running);assert 'verändert' in w.comment_auto_error
  w.comment_auto_enabled=True;w.comment_auto_session=w.local;w.comment_auto_source=mod.GLib.timeout_add_seconds(60,w.comment_auto_tick)
  fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.comment_auto_enabled and not w.comment_auto_source
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy();print('GTK task comment synchronization, pending tasks and automatic offline recovery: OK')
 finally:process.terminate();process.wait(timeout=5)

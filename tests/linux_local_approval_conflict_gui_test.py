#!/usr/bin/python3
"""Personal approval exchange across two independent Linux workspaces."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from approval_sync import sync_approvals,inspect_approval_conflict,accept_server_approval
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-approval-sync-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':'alice-token','AURA_SYNC_IDENTITIES':json.dumps({'alice-token':{'employeeId':9,'permissions':['employees.manage','tasks.manage']},'bob-token':{'employeeId':8,'permissions':['employees.manage','tasks.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
 lock=threading.Lock();ok=lambda:True
 def employees(s,t):return sync_employees(s,lock,ok,client,url,t,str(cert))
 def tasks(s,t):return sync_tasks(s,lock,ok,client,url,t,str(cert))
 def approvals(s,t):return sync_approvals(s,lock,ok,client,url,t,str(cert))
 process=start()
 try:
  a=LocalStore(Path(tmp)/'a.db');a.create('Alice','alice','Alice Passwort 2026');a.open();a.login('alice','Alice Passwort 2026')
  a.save_employee(0,'Bob','Projektleitung','IT');bob_a=next(v for v in a.employees() if v['name']=='Bob')
  employees(a,'alice-token')
  for name,account in [('Alice',9),('Bob',8)]:
   item=next(v for v in cli('-action','sync-employees') if v['name']==name);cli('-action','employee-account-link','-id',item['id'],'-account-id',str(account))
  employees(a,'alice-token');tasks(a,'alice-token')
  a.save_task(0,'Prüfung Linux','IT','Hoch',a.session_employee(),0,0);tasks(a,'alice-token')
  task=next(v for v in a.tasks() if v['title']=='Prüfung Linux')
  assert approvals(a,'alice-token')=={'sent':0,'received':0,'unmapped':0}
  a.create_approval(task['id'],bob_a['id'])
  with sqlite3.connect(Path(tmp)/'a.db') as sql:sql.execute("CREATE TRIGGER fail_approval_ack BEFORE DELETE ON linux_approval_sync_outbox BEGIN SELECT RAISE(ABORT,'ack failure'); END")
  reject(lambda:approvals(a,'alice-token'))
  with sqlite3.connect(Path(tmp)/'a.db') as sql:
   assert sql.execute('SELECT count(*) FROM linux_approval_sync_outbox').fetchone()[0]==1
   sql.execute('DROP TRIGGER fail_approval_ack')
  assert approvals(a,'alice-token')=={'sent':1,'received':0,'unmapped':0}
  assert len(cli('-action','sync-approvals'))==1
  b=LocalStore(Path(tmp)/'b.db');b.create('Arbeitsplatz B','adminb','Admin Passwort 2026');b.open();b.login('adminb','Admin Passwort 2026')
  employees(b,'bob-token');tasks(b,'bob-token')
  bob_b=next(v for v in b.employees() if v['name']=='Bob');b.save_role(0,'task_reader','Aufgaben lesen','','tasks.read');b.assign_role(bob_b['id'],'task_reader');b.create_account(bob_b['id'],'bob','Bob Passwort 2026')
  bob=LocalStore(Path(tmp)/'b.db');bob.open();bob.login('bob','Bob Passwort 2026');bob.change_password('Bob Passwort 2026','Bob Eigenes Passwort 2026')
  reject(lambda:approvals(bob,'alice-token'))
  assert approvals(bob,'bob-token')=={'sent':0,'received':1,'unmapped':0}
  item=bob.approvals()[0];assert item['taskTitle']=='Prüfung Linux' and item['requesterName']=='Alice'
  process.terminate();process.wait(timeout=5)
  bob.decide_approval(item['id'],True);reject(lambda:approvals(bob,'bob-token'))
  with sqlite3.connect(Path(tmp)/'b.db') as sql:assert sql.execute('SELECT count(*) FROM linux_approval_sync_outbox').fetchone()[0]==1
  process=start();assert approvals(bob,'bob-token')=={'sent':1,'received':0,'unmapped':0}
  assert approvals(a,'alice-token')=={'sent':0,'received':1,'unmapped':0}
  remote=cli('-action','sync-approvals');assert len(remote)==1 and remote[0]['status']=='approved' and remote[0]['decidedBy']=='8'
  with sqlite3.connect(Path(tmp)/'a.db') as sql:assert sql.execute('SELECT status FROM task_approvals').fetchone()[0]=='approved'
  a.create_approval(task['id'],bob_a['id']);assert approvals(a,'alice-token')['sent']==1
  assert approvals(bob,'bob-token')['received']==1
  pending=next(v for v in bob.approvals() if v['status']=='pending');bob.decide_approval(pending['id'],True)
  remote=next(v for v in cli('-action','sync-approvals') if v['status']=='pending')
  bobenv=env.copy();bobenv['AURA_SYNC_TOKEN']='bob-token'
  payload={'clientId':'other-device','operationId':'conflict-decision','action':'decide','approval':{**remote,'status':'rejected','decidedBy':'8'},'expected':remote}
  subprocess.run([str(client),'-action','sync-approval-event','-payload',json.dumps(payload)],env=bobenv,capture_output=True,text=True,check=True)
  reject(lambda:approvals(bob,'bob-token'))
  inspect=lambda:inspect_approval_conflict(bob,lock,ok,client,url,'bob-token',str(cert))
  accept=lambda value:accept_server_approval(bob,lock,ok,client,url,'bob-token',str(cert),value)
  reviewed=inspect();assert reviewed['preview']['local']['status']=='approved' and reviewed['remote']['status']=='rejected'
  stale=json.loads(json.dumps(reviewed));stale['remote']['status']='approved';reject(lambda:accept(stale))
  with sqlite3.connect(Path(tmp)/'b.db') as sql:sql.execute("CREATE TRIGGER fail_approval_resolution BEFORE INSERT ON audit_log WHEN NEW.action='approval.sync.accepted_server' BEGIN SELECT RAISE(ABORT,'audit failure'); END")
  reject(lambda:accept(reviewed))
  with sqlite3.connect(Path(tmp)/'b.db') as sql:
   assert sql.execute('SELECT count(*) FROM linux_approval_sync_outbox').fetchone()[0]==1
   assert not sql.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_approval_sync_resolutions'").fetchone()
   sql.execute('DROP TRIGGER fail_approval_resolution')
  accept(reviewed);assert next(v for v in bob.approvals() if v['id']==pending['id'])['status']=='rejected'
  with sqlite3.connect(Path(tmp)/'b.db') as sql:
   assert sql.execute('SELECT count(*) FROM linux_approval_sync_outbox').fetchone()[0]==0
   assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_approval_sync_resolutions').fetchone()[0]))==1
  assert approvals(bob,'bob-token')['sent']==0
  a.create_approval(task['id'],bob_a['id']);approvals(a,'alice-token');approvals(bob,'bob-token')
  pending=next(v for v in bob.approvals() if v['status']=='pending');bob.decide_approval(pending['id'],True)
  remote=next(v for v in cli('-action','sync-approvals') if v['status']=='pending')
  payload={'clientId':'gui-other-device','operationId':'gui-conflict','action':'decide','approval':{**remote,'status':'rejected','decidedBy':'8'},'expected':remote}
  subprocess.run([str(client),'-action','sync-approval-event','-payload',json.dumps(payload)],env=bobenv,capture_output=True,text=True,check=True)
  bob.close();b.close();a.close()
  os.environ['AURA_LOCAL_DB']=tmp+'/b.db'
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
    if title=='Freigabekonflikt entscheiden':assert 'approved' in ds[-1].get_property('secondary-text') and 'rejected' in ds[-1].get_property('secondary-text')
    ds[-1].response(response);return False
   return action
  fill('Lokale Anmeldung',['bob','Bob Eigenes Passwort 2026']);w.local_login();wait(lambda:w.local_mode)
  w.show_approvals();wait(lambda:any(v.get_label()=='Freigabekonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
  def conflict(response):
   fill('Freigabekonflikt prüfen',[url,'bob-token',str(cert)]);mod.GLib.timeout_add(300,reply('Freigabekonflikt entscheiden',response))
   next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Freigabekonflikt prüfen').clicked();wait(lambda:not w.task_sync_running)
  conflict(mod.Gtk.ResponseType.CANCEL);assert next(v for v in w.local.approvals() if v['id']==pending['id'])['status']=='approved'
  mod.GLib.timeout_add(500,reply('Serverfreigabe lokal übernehmen?',mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(800,reply('Serverfreigabe übernommen',mod.Gtk.ResponseType.CLOSE))
  conflict(mod.Gtk.ResponseType.OK);assert next(v for v in w.local.approvals() if v['id']==pending['id'])['status']=='rejected'
  w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
  with w.local_lock:
   if w.local:w.local.close()
  print('GTK approval conflict review, cancel and acceptance: OK')
 finally:
  process.terminate();process.wait(timeout=5)

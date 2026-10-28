#!/usr/bin/python3
"""Restricted employee contributions survive offline and reach another workspace."""
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from employee_sync import sync_employees
from task_sync import sync_tasks
from ticket_sync import sync_tickets
from task_comment_sync import sync_task_comments,sync_personal_task_comments
from ticket_comment_sync import sync_ticket_comments,sync_personal_ticket_comments
from attachment_sync import sync_attachments,sync_personal_attachments,inspect_attachment_conflict
server=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-collaborator-e2e-') as tmp:
 cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='admin-token'
 env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage','tickets.manage']},'worker-token':{'employeeId':8,'permissions':['tasks.read','tickets.read','collaboration.comment']},'reader-token':{'employeeId':8,'permissions':['tasks.read','tickets.read']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
 context=ssl.create_default_context(cafile=str(cert))
 def start():
  p=subprocess.Popen([str(server),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  for _ in range(100):
   if p.poll() is not None:raise RuntimeError(p.stderr.read().decode())
   try:urllib.request.urlopen(url+'/health',context=context,timeout=1).close();return p
   except OSError:time.sleep(.05)
  raise AssertionError('server did not start')
 def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,text=True,capture_output=True,check=True).stdout)
 process=start()
 try:
  first=Path(tmp)/'first';first.mkdir();admin=LocalStore(first/'workspace.db');admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
  admin.save_employee(0,'Mitarbeiterin','Team','IT');worker_id=next(x['id'] for x in admin.employees() if x['name']=='Mitarbeiterin')
  admin.save_role(0,'collaborator','Zusammenarbeit','','tasks.read,tickets.read,collaboration.comment');admin.assign_role(worker_id,'collaborator');admin.create_account(worker_id,'mitarbeiterin','Temporär Passwort 2026')
  admin.save_role(0,'reader','Lesen','','tasks.read,tickets.read')
  lock=threading.Lock();current=lambda:True
  sync_employees(admin,lock,current,client,url,token,str(cert))
  employees=cli('-action','sync-employees')
  for name,number in [('Admin',9),('Mitarbeiterin',8)]:cli('-action','employee-account-link','-id',next(e['id'] for e in employees if e['name']==name),'-account-id',str(number))
  sync_employees(admin,lock,current,client,url,token,str(cert))
  admin.save_task(0,'Teamaufgabe','IT','Mittel',worker_id);task=next(x['id'] for x in admin.tasks() if x['title']=='Teamaufgabe')
  admin.save_ticket(0,'Teamticket','IT','Mittel',worker_id);ticket=next(x['id'] for x in admin.tickets() if x['title']=='Teamticket')
  sync_tasks(admin,lock,current,client,url,token,str(cert));sync_tickets(admin,lock,current,client,url,token,str(cert))
  def sync_all(store,mutex):
   sync_task_comments(store,mutex,current,client,url,token,str(cert));sync_ticket_comments(store,mutex,current,client,url,token,str(cert))
   for kind in ('task','ticket'):sync_attachments(store,mutex,current,client,url,token,str(cert),kind)
  sync_all(admin,lock)
  worker=LocalStore(first/'workspace.db');worker.open();assert worker.login('mitarbeiterin','Temporär Passwort 2026');worker.change_password('Temporär Passwort 2026','Eigenes Passwort 2026')
  path=Path(tmp)/'notiz.txt';path.write_text('Offline-Beitrag',encoding='utf-8')
  process.terminate();process.wait(timeout=5)
  for kind,item in [('task',task),('ticket',ticket)]:worker.add_comment(kind,item,'Offline-Kommentar '+kind);worker.import_attachment(kind,item,path)
  try:worker.save_task(task,'Nicht erlaubt','IT','Mittel',worker_id);raise AssertionError('worker changed task')
  except LocalStoreError:pass
  process=start()
  personal=lambda:sync_personal_task_comments(worker,lock,current,client,url,'worker-token',str(cert))
  with sqlite3.connect(first/'workspace.db') as sql:sql.execute("CREATE TRIGGER fail_personal_comment_ack BEFORE DELETE ON linux_task_comment_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:personal();raise AssertionError('failed acknowledgement accepted')
  except LocalStoreError:pass
  try:sync_task_comments(admin,lock,current,client,url,token,str(cert));raise AssertionError('administrator stole personal operation')
  except LocalStoreError as exc:assert 'persönlicher Kommentarabgleich' in str(exc)
  process.terminate();process.wait(timeout=5);process=start()
  with sqlite3.connect(first/'workspace.db') as sql:sql.execute('DROP TRIGGER fail_personal_comment_ack')
  assert personal()=={'sent':1}
  assert len(cli('-action','sync-task-comments'))==1
  task_remote=next(v['id'] for v in cli('-action','sync-tasks') if v['title']=='Teamaufgabe')
  cli('-action','add-comment','-subject-type','task','-subject-id',task_remote,'-body','Antwort der Leitung')
  worker.add_comment('task',task,'Eigener Kommentar bleibt wartend')
  admin.assign_role(worker_id,'reader');assert not worker.identity()['canCollaborate']
  assert sync_personal_task_comments(worker,lock,current,client,url,'reader-token',str(cert),task)=={'sent':0,'received':1}
  assert len(cli('-action','sync-task-comments'))==2
  admin.assign_role(worker_id,'collaborator')
  cli('-action','add-comment','-subject-type','task','-subject-id',task_remote,'-body','Zweite Antwort')
  worker.add_comment('task',task,'Kommentar über GTK')
  spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  window=mod.AuraWindow();window.local=worker;window.local_mode=True;window.local_lock=lock;window.show_all()
  window.show_comments('task',next(v for v in worker.tasks() if v['id']==task))
  def children(widget,cls):
   found=[widget] if isinstance(widget,cls) else []
   if isinstance(widget,mod.Gtk.Container):
    for child in widget.get_children():found.extend(children(child,cls))
   return found
  def wait(test):
   end=time.monotonic()+15
   while time.monotonic()<end:
    while mod.Gtk.events_pending():mod.Gtk.main_iteration()
    if test():return
    time.sleep(.02)
   raise AssertionError('GTK personal comment sync timed out')
  wait(lambda:any(v.get_label()=='Kommentare persönlich abgleichen' for v in children(window.content,mod.Gtk.Button)))
  messages=[];window.message=lambda value:messages.append(value);window.form=lambda *_args:[url,'worker-token',str(cert)]
  next(v for v in children(window.content,mod.Gtk.Button) if v.get_label()=='Kommentare persönlich abgleichen').clicked()
  wait(lambda:bool(messages));assert '2 eigene Aufgabenkommentare übertragen, 1 Kommentare übernommen' in messages[-1],messages
  assert any(v['body']=='Zweite Antwort' and v['author']=='Admin' for v in worker.comments('task',task))
  worker_remote=next(e['id'] for e in employees if e['name']=='Mitarbeiterin');admin_remote=next(e['id'] for e in employees if e['name']=='Admin')
  cli('-action','employee-account-link','-id',worker_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','8')
  try:sync_personal_task_comments(worker,lock,current,client,url,'worker-token',str(cert),task);raise AssertionError('remapped account read accepted')
  except LocalStoreError as exc:assert '409' in str(exc)
  cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','9');cli('-action','employee-account-link','-id',worker_remote,'-account-id','8')
  new_author=cli('-action','central-employee-create','-name','Anderer Autor','-role','Team','-department','IT','-presence','office')
  cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',new_author['id'],'-account-id','9')
  try:sync_personal_task_comments(worker,lock,current,client,url,'worker-token',str(cert),task);raise AssertionError('remapped author accepted')
  except LocalStoreError as exc:assert 'Kommentarautors' in str(exc)
  cli('-action','employee-account-link','-id',new_author['id'],'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','9')
  assert len(cli('-action','sync-task-comments'))==5
  personal_ticket=lambda:sync_personal_ticket_comments(worker,lock,current,client,url,'worker-token',str(cert))
  with sqlite3.connect(first/'workspace.db') as sql:sql.execute("CREATE TRIGGER fail_personal_ticket_ack BEFORE DELETE ON linux_ticket_comment_sync_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
  try:personal_ticket();raise AssertionError('ticket acknowledgement failure accepted')
  except LocalStoreError:pass
  try:sync_ticket_comments(admin,lock,current,client,url,token,str(cert));raise AssertionError('administrator stole personal ticket operation')
  except LocalStoreError as exc:assert 'persönlicher Kommentarabgleich' in str(exc)
  process.terminate();process.wait(timeout=5);process=start()
  with sqlite3.connect(first/'workspace.db') as sql:sql.execute('DROP TRIGGER fail_personal_ticket_ack')
  assert personal_ticket()=={'sent':1}
  ticket_remote=next(v['id'] for v in cli('-action','sync-tickets') if v['title']=='Teamticket')
  cli('-action','add-comment','-subject-type','ticket','-subject-id',ticket_remote,'-body','Ticketantwort der Leitung')
  worker.add_comment('ticket',ticket,'Eigenes Ticket bleibt wartend')
  admin.assign_role(worker_id,'reader');assert not worker.identity()['canCollaborate']
  assert sync_personal_ticket_comments(worker,lock,current,client,url,'reader-token',str(cert),ticket)=={'sent':0,'received':1}
  assert len(cli('-action','sync-ticket-comments'))==2
  admin.assign_role(worker_id,'collaborator')
  cli('-action','add-comment','-subject-type','ticket','-subject-id',ticket_remote,'-body','Zweite Ticketantwort')
  worker.add_comment('ticket',ticket,'Ticket über GTK')
  window.show_comments('ticket',next(v for v in worker.tickets() if v['id']==ticket));messages.clear()
  wait(lambda:any(v.get_label()=='Kommentare persönlich abgleichen' for v in children(window.content,mod.Gtk.Button)))
  next(v for v in children(window.content,mod.Gtk.Button) if v.get_label()=='Kommentare persönlich abgleichen').clicked()
  wait(lambda:bool(messages));assert '2 eigene Ticketkommentare übertragen, 1 Kommentare übernommen' in messages[-1],messages
  assert any(v['body']=='Zweite Ticketantwort' and v['author']=='Admin' for v in worker.comments('ticket',ticket))
  for kind,item in [('task',task),('ticket',ticket)]:
   prefix='linux_'+kind+'_attachment_sync'
   if kind=='task':
    stored=Path(worker.attachments(kind,item)[0]['storedPath']);original=stored.read_bytes();stored.write_bytes(b'Verandert')
    try:sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind);raise AssertionError('corrupt attachment uploaded')
    except LocalStoreError:pass
    assert cli('-action','sync-attachments','-subject-type',kind)==[]
    stored.write_bytes(original)
   with sqlite3.connect(first/'workspace.db') as sql:sql.execute('CREATE TRIGGER fail_personal_attachment_ack BEFORE DELETE ON '+prefix+"_outbox BEGIN SELECT RAISE(ABORT,'ack'); END")
   try:sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind);raise AssertionError('attachment acknowledgement failure accepted')
   except LocalStoreError:pass
   try:sync_attachments(admin,lock,current,client,url,token,str(cert),kind);raise AssertionError('administrator stole personal attachment operation')
   except LocalStoreError as exc:assert 'persönlicher Anhangsabgleich' in str(exc)
   process.terminate();process.wait(timeout=5);process=start()
   with sqlite3.connect(first/'workspace.db') as sql:sql.execute('DROP TRIGGER fail_personal_attachment_ack')
   assert sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind)=={'sent':1}
   shared=Path(tmp)/(kind+'-leitung.txt');shared.write_text('Anhang der Leitung',encoding='utf-8')
   remote_subject=task_remote if kind=='task' else ticket_remote
   cli('-action','subject-attachment-upload','-subject-type',kind,'-subject-id',remote_subject,'-file',str(shared))
   cli('-action','employee-account-link','-id',worker_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','8')
   try:sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind,item);raise AssertionError('remapped attachment viewer accepted')
   except LocalStoreError as exc:assert '409' in str(exc)
   cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','9');cli('-action','employee-account-link','-id',worker_remote,'-account-id','8')
   cli('-action','employee-account-link','-id',admin_remote,'-account-id','0');cli('-action','employee-account-link','-id',new_author['id'],'-account-id','9')
   try:sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind,item);raise AssertionError('remapped attachment author accepted')
   except LocalStoreError as exc:assert 'Anhangsautors' in str(exc)
   assert not any(v['name']==kind+'-leitung.txt' for v in worker.attachments(kind,item))
   cli('-action','employee-account-link','-id',new_author['id'],'-account-id','0');cli('-action','employee-account-link','-id',admin_remote,'-account-id','9')
   extra=Path(tmp)/(kind+'-gtk.txt');extra.write_text('Persönlicher GTK Anhang',encoding='utf-8');worker.import_attachment(kind,item,extra)
   admin.assign_role(worker_id,'reader');assert not worker.identity()['canCollaborate']
   assert sync_personal_attachments(worker,lock,current,client,url,'reader-token',str(cert),kind,item)=={'sent':0,'received':1}
   assert not any(v['originalName']==kind+'-gtk.txt' for v in cli('-action','sync-attachments','-subject-type',kind))
   admin.assign_role(worker_id,'collaborator')
   second_shared=Path(tmp)/(kind+'-zweiter.txt');second_shared.write_text('Zweiter Anhang',encoding='utf-8')
   cli('-action','subject-attachment-upload','-subject-type',kind,'-subject-id',remote_subject,'-file',str(second_shared))
   centrally_archived=Path(tmp)/(kind+'-zentral.txt');centrally_archived.write_text('Später zentral archiviert',encoding='utf-8')
   central_attachment=cli('-action','subject-attachment-upload','-subject-type',kind,'-subject-id',remote_subject,'-file',str(centrally_archived))
   never_seen=Path(tmp)/(kind+'-unbekannt.txt');never_seen.write_text('Nie lokal angezeigt',encoding='utf-8')
   secret_attachment=cli('-action','subject-attachment-upload','-subject-type',kind,'-subject-id',remote_subject,'-file',str(never_seen))
   cli('-action','subject-attachment-archive','-subject-type',kind,'-subject-id',remote_subject,'-id',secret_attachment['id'])
   source=worker.tasks() if kind=='task' else worker.tickets();window.show_attachments(kind,next(v for v in source if v['id']==item));messages.clear()
   wait(lambda:any(v.get_label()=='Anhänge persönlich abgleichen' for v in children(window.content,mod.Gtk.Button)))
   next(v for v in children(window.content,mod.Gtk.Button) if v.get_label()=='Anhänge persönlich abgleichen').clicked()
   wait(lambda:bool(messages));assert '1 eigene Anhangsänderungen übertragen, 2 Anhänge übernommen' in messages[-1],messages
   received=next(v for v in worker.attachments(kind,item) if v['name']==kind+'-leitung.txt');assert Path(worker.verify_attachment(kind,item,received['id'])).read_text()=='Anhang der Leitung'
   assert not any(v['name']==kind+'-unbekannt.txt' for v in worker.attachments(kind,item))
   cli('-action','subject-attachment-archive','-subject-type',kind,'-subject-id',remote_subject,'-id',central_attachment['id'])
   admin.assign_role(worker_id,'reader')
   assert sync_personal_attachments(worker,lock,current,client,url,'reader-token',str(cert),kind,item)=={'sent':0,'received':1}
   assert not any(v['name']==kind+'-zentral.txt' for v in worker.attachments(kind,item))
   archived_local=next(v for v in admin.attachments(kind,item,True) if v['name']==kind+'-zentral.txt');assert not archived_local['active']
   archived_path=Path(admin.verify_attachment(kind,item,archived_local['id'],True));assert archived_path.read_text()=='Später zentral archiviert'
   cli('-action','subject-attachment-restore','-subject-type',kind,'-subject-id',remote_subject,'-payload',json.dumps({**central_attachment,'active':False}))
   archived_path.write_text('Beschädigt',encoding='utf-8')
   try:sync_personal_attachments(worker,lock,current,client,url,'reader-token',str(cert),kind,item);raise AssertionError('corrupt restored attachment accepted')
   except LocalStoreError:pass
   assert not any(v['name']==kind+'-zentral.txt' for v in worker.attachments(kind,item))
   archived_path.write_text('Später zentral archiviert',encoding='utf-8')
   window.form=lambda *_args:[url,'reader-token',str(cert)]
   source=worker.tasks() if kind=='task' else worker.tickets();window.show_attachments(kind,next(v for v in source if v['id']==item));messages.clear()
   wait(lambda:any(v.get_label()=='Anhänge persönlich abgleichen' for v in children(window.content,mod.Gtk.Button)))
   next(v for v in children(window.content,mod.Gtk.Button) if v.get_label()=='Anhänge persönlich abgleichen').clicked()
   wait(lambda:bool(messages));assert '0 eigene Anhangsänderungen übertragen, 1 Anhänge übernommen' in messages[-1],messages
   restored=next(v for v in worker.attachments(kind,item) if v['name']==kind+'-zentral.txt')
   assert restored['id']==archived_local['id'] and Path(worker.verify_attachment(kind,item,restored['id'])).read_text()=='Später zentral archiviert'
   window.form=lambda *_args:[url,'worker-token',str(cert)]
   admin.assign_role(worker_id,'collaborator')
   worker.archive_attachment(kind,item,received['id'])
   with sqlite3.connect(first/'workspace.db') as sql:
    assert sql.execute('SELECT actor_id FROM '+prefix+'_archive_actor').fetchone()==(worker_id,)
    sql.execute('CREATE TRIGGER fail_personal_archive_ack BEFORE DELETE ON '+prefix+"_outbox WHEN OLD.action='archive' BEGIN SELECT RAISE(ABORT,'ack'); END")
   try:sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind);raise AssertionError('archive acknowledgement failure accepted')
   except LocalStoreError:pass
   try:sync_attachments(admin,lock,current,client,url,token,str(cert),kind);raise AssertionError('administrator stole personal archive')
   except LocalStoreError as exc:assert 'persönlicher Anhangsabgleich' in str(exc)
   try:inspect_attachment_conflict(admin,lock,current,client,url,token,str(cert),kind);raise AssertionError('administrator inspected claimed operation')
   except LocalStoreError as exc:assert 'persönlicher Anhangsabgleich' in str(exc)
   process.terminate();process.wait(timeout=5);process=start()
   with sqlite3.connect(first/'workspace.db') as sql:sql.execute('DROP TRIGGER fail_personal_archive_ack')
   assert sync_personal_attachments(worker,lock,current,client,url,'worker-token',str(cert),kind)=={'sent':1}
   assert not next(v for v in cli('-action','sync-attachments','-subject-type',kind) if v['originalName']==kind+'-leitung.txt')['active']
   another=next(v for v in worker.attachments(kind,item) if v['name']==kind+'-zweiter.txt');worker.archive_attachment(kind,item,another['id'])
   window.show_attachments(kind,next(v for v in source if v['id']==item));messages.clear()
   wait(lambda:any(v.get_label()=='Anhänge persönlich abgleichen' for v in children(window.content,mod.Gtk.Button)))
   next(v for v in children(window.content,mod.Gtk.Button) if v.get_label()=='Anhänge persönlich abgleichen').clicked()
   wait(lambda:bool(messages));assert '1 eigene Anhangsänderungen übertragen' in messages[-1],messages
   assert not next(v for v in cli('-action','sync-attachments','-subject-type',kind) if v['originalName']==kind+'-zweiter.txt')['active']
  window.disconnect_by_func(mod.Gtk.main_quit);window.destroy()
  assert len(cli('-action','sync-ticket-comments'))==5
  sync_all(admin,lock)
  second=Path(tmp)/'second';second.mkdir();peer=LocalStore(second/'workspace.db');peer.create('Peer','peer','Peer Passwort 2026');peer.open();peer.login('peer','Peer Passwort 2026');peerlock=threading.Lock()
  sync_employees(peer,peerlock,current,client,url,token,str(cert));sync_tasks(peer,peerlock,current,client,url,token,str(cert));sync_tickets(peer,peerlock,current,client,url,token,str(cert));sync_all(peer,peerlock)
  peer_task=next(x['id'] for x in peer.tasks() if x['title']=='Teamaufgabe');peer_ticket=next(x['id'] for x in peer.tickets() if x['title']=='Teamticket')
  for kind,item in [('task',peer_task),('ticket',peer_ticket)]:
   comment=next(x for x in peer.comments(kind,item) if x['body']=='Offline-Kommentar '+kind);assert comment['author']=='Mitarbeiterin'
   attachment=next(x for x in peer.attachments(kind,item) if x['name']=='notiz.txt');assert Path(peer.verify_attachment(kind,item,attachment['id'])).read_text()=='Offline-Beitrag'
   gtk_attachment=next(x for x in peer.attachments(kind,item) if x['name']==kind+'-gtk.txt');assert Path(peer.verify_attachment(kind,item,gtk_attachment['id'])).read_text()=='Persönlicher GTK Anhang'
  peer.close();worker.close();admin.close()
  print('Restricted employee offline task and ticket comments/attachments reach second workspace with author and verified files: OK')
 finally:process.terminate();process.wait(timeout=5)

#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-dependency-conflict-gui-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='sync-manager'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','tasks.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
    process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        ctx=ssl.create_default_context(cafile=str(cert))
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not start')
        os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None);os.environ.pop('AURA_SYNC_CA_CERT',None)
        spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_task(0,'GUI Aufgabe','IT','Mittel',setup.session_employee(),1900000000,7)
        initial_task=next(v for v in setup.tasks() if v['title']=='GUI Aufgabe');setup.save_subtask(initial_task['id'],0,'GUI Unteraufgabe','open',setup.session_employee(),0)
        setup.save_task(0,'GUI Voraussetzung','IT','Mittel',setup.session_employee(),0,0);prereq=next(v for v in setup.tasks() if v['title']=='GUI Voraussetzung');setup.dependency_change(initial_task['id'],prereq['id'],True)
        from employee_sync import sync_employees
        import threading
        sync_employees(setup,threading.Lock(),lambda:True,client,url,token,str(cert))
        staff=json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout)
        subprocess.run([str(client),'-action','employee-account-link','-id',staff[0]['id'],'-account-id','9'],env=env,text=True,capture_output=True,check=True)
        sync_employees(setup,threading.Lock(),lambda:True,client,url,token,str(cert));setup.close()
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
        fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode);w.select(5)
        def confirm():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokale Aufgaben synchronisieren?']
            if not ds:return True
            assert 'Unteraufgaben' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finish():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Aufgabenabgleich abgeschlossen']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        fill('Aufgabenabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        buttons=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Aufgaben synchronisieren'];assert len(buttons)==1
        buttons[0].clicked();wait(lambda:not w.task_sync_running)
        tasks=json.loads(subprocess.run([str(client),'-action','sync-tasks'],env=env,text=True,capture_output=True,check=True).stdout)
        main=next(v for v in tasks if v['title']=='GUI Aufgabe');prereq=next(v for v in tasks if v['title']=='GUI Voraussetzung')
        edges=json.loads(subprocess.run([str(client),'-action','sync-dependencies'],env=env,text=True,capture_output=True,check=True).stdout)
        assert edges==[{'taskId':main['id'],'dependencyId':prereq['id']}]
        local_main=next(v for v in w.local.tasks() if v['title']=='GUI Aufgabe');local_pre=next(v for v in w.local.tasks() if v['title']=='GUI Voraussetzung')
        w.local.dependency_change(local_main['id'],local_pre['id'],False)
        subprocess.run([str(client),'-action','dependency-remove','-task-id',main['id'],'-dependency-id',prereq['id']],env=env,text=True,capture_output=True,check=True)
        def choose(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Voraussetzungskonflikt entscheiden']
                if not ds:return True
                detail=ds[-1].get_property('secondary-text');assert 'GUI Aufgabe' in detail and 'GUI Voraussetzung' in detail and 'Server:' in detail;ds[-1].response(response);return False
            return reply
        def done(title):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')==title]
                if not ds:return True
                ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
            return reply
        def review(response):
            fill('Voraussetzungskonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose(response))
            [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Voraussetzungskonflikt prüfen'][0].clicked()
        review(mod.Gtk.ResponseType.CANCEL);wait(lambda:not w.task_sync_running)
        mod.GLib.timeout_add(1000,done('Lokale Voraussetzung vorgemerkt'));review(mod.Gtk.ResponseType.OK);wait(lambda:not w.task_sync_running)
        from task_sync import sync_tasks
        assert sync_tasks(w.local,w.local_lock,lambda:True,client,url,token,str(cert))=={'sent':0,'received':0}
        w.local.dependency_change(local_main['id'],local_pre['id'],True)
        subprocess.run([str(client),'-action','dependency-add','-task-id',prereq['id'],'-dependency-id',main['id']],env=env,text=True,capture_output=True,check=True)
        def confirm_server():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Servervoraussetzung lokal übernehmen?']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,done('Servervoraussetzung übernommen'));review(mod.Gtk.ResponseType.YES);wait(lambda:not w.task_sync_running)
        assert not w.local.dependencies(local_main['id'])
        assert sync_tasks(w.local,w.local_lock,lambda:True,client,url,token,str(cert))=={'sent':0,'received':1}
        assert w.local.dependencies(local_pre['id'])[0]['id']==local_main['id']
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK prerequisite conflict decisions: OK')

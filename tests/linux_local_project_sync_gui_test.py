#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-project-sync-gui-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='sync-manager'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','projects.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_project(0,'GUI Projekt','IT','Beschreibung','planned',setup.session_employee())
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
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokale Projekte synchronisieren?']
            if not ds:return True
            assert 'Zuordnungen' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finish():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Projektabgleich abgeschlossen']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        def sync():
            fill('Projektabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
            [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Projekte synchronisieren'][0].clicked();wait(lambda:not w.project_sync_running)
        sync();projects=json.loads(subprocess.run([str(client),'-action','sync-projects'],env=env,text=True,capture_output=True,check=True).stdout)
        assert len(projects)==1 and projects[0]['name']=='GUI Projekt';remote=projects[0];local=next(v for v in w.local.projects() if v['name']=='GUI Projekt');employee=w.local.session_employee()
        def central(name):
            subprocess.run([str(client),'-action','update-project','-id',remote['id'],'-name',name,'-department','IT','-owner-id','9','-description','Server','-status','active'],env=env,text=True,capture_output=True,check=True)
        def choose(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Projektkonflikt entscheiden']
                if not ds:return True
                detail=ds[-1].get_property('secondary-text');assert 'Lokal' in detail and 'Server' in detail;ds[-1].response(response);return False
            return reply
        def done(title):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')==title]
                if not ds:return True
                ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
            return reply
        def review(response):
            fill('Projektkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose(response))
            [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Projektkonflikt prüfen'][0].clicked()
        w.local.save_project(local['id'],'Lokal','IT','L','active',employee);central('Server')
        review(mod.Gtk.ResponseType.CANCEL);wait(lambda:not w.project_sync_running)
        mod.GLib.timeout_add(1000,done('Lokales Projekt vorgemerkt'));review(mod.Gtk.ResponseType.OK);wait(lambda:not w.project_sync_running);sync()
        assert json.loads(subprocess.run([str(client),'-action','sync-projects'],env=env,text=True,capture_output=True,check=True).stdout)[0]['name']=='Lokal'
        w.local.save_project(local['id'],'Lokal Zwei','IT','L','active',employee);central('Server Zwei')
        def confirm_server():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serverprojekt lokal übernehmen?']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,done('Serverprojekt übernommen'));review(mod.Gtk.ResponseType.YES);wait(lambda:not w.project_sync_running)
        assert next(v for v in w.local.projects() if v['id']==local['id'])['name']=='Server Zwei'
        w.project_auto_interval=1;auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Projekte automatisch' in v.get_label()][0];auto.set_active(True);wait(lambda:not w.project_sync_running);assert w.project_auto_enabled
        with w.local_lock:
            w.local.save_project(0,'Ohne Leitung','IT','','planned',0)
            unassigned=next(v for v in w.local.projects() if v['name']=='Ohne Leitung')
            w.local.save_project(local['id'],'Unabhängig gesendet','IT','L','active',employee)
        subprocess.run([str(client),'-action','create-project','-name','Unabhängig empfangen','-department','IT','-owner-id','9','-description','Server'],env=env,text=True,capture_output=True,check=True)
        wait(lambda:'Projektleitung' in w.project_auto_error and not w.project_sync_running);assert w.project_auto_enabled
        assert any(v['name']=='Unabhängig empfangen' for v in w.local.projects())
        assert next(v for v in json.loads(subprocess.run([str(client),'-action','sync-projects'],env=env,text=True,capture_output=True,check=True).stdout) if v['id']==remote['id'])['name']=='Unabhängig gesendet'
        with w.local_lock:w.local.save_project(unassigned['id'],'Nun mit Leitung','IT','','planned',employee)
        def assigned_sent():return any(v['name']=='Nun mit Leitung' for v in json.loads(subprocess.run([str(client),'-action','sync-projects'],env=env,text=True,capture_output=True,check=True).stdout))
        wait(lambda:assigned_sent() and not w.project_sync_running);assert w.project_auto_enabled
        process.terminate();process.wait(timeout=5);w.local.save_project(local['id'],'Offline','IT','L','active',employee)
        wait(lambda:'vorübergehend' in w.project_auto_error and not w.project_sync_running);assert w.project_auto_enabled
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        def remote_name():return next(v for v in json.loads(subprocess.run([str(client),'-action','sync-projects'],env=env,text=True,capture_output=True,check=True).stdout) if v['id']==remote['id'])['name']
        wait(lambda:not w.project_sync_running and remote_name()=='Offline');assert w.project_auto_enabled
        central('Server Konflikt');w.local.save_project(local['id'],'Lokal Konflikt','IT','L','active',employee)
        wait(lambda:not w.project_auto_enabled and not w.project_sync_running);assert '409' in w.project_auto_error
        fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.project_auto_enabled and not w.project_auto_source
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK project synchronization, conflict decisions and automatic sync: OK')

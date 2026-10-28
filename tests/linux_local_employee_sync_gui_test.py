#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-employee-sync-gui-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='sync-manager'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_employee(0,'GUI Mitarbeiter','Entwicklerin','IT');setup.close()
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
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokale Mitarbeiter synchronisieren?']
            if not ds:return True
            assert 'Urlaub' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finish():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible()]
            if not ds:return True
            text=ds[-1].get_property('text') or ''
            if text!='Mitarbeiterabgleich abgeschlossen':return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        fill('Mitarbeiterübertragung zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        buttons=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Mitarbeiter synchronisieren'];assert len(buttons)==1
        buttons[0].clicked();wait(lambda:not w.employee_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        staff=json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout)
        assert {v['name'] for v in staff}=={'Admin','GUI Mitarbeiter'}
        remote=next(v for v in staff if v['name']=='GUI Mitarbeiter');local=next(v for v in w.local.employees() if v['name']=='GUI Mitarbeiter')
        w.local.save_employee(local['id'],'GUI Mitarbeiter','Entwicklerin','Lokal')
        subprocess.run([str(client),'-action','central-employee-update','-id',remote['id'],'-name','GUI Mitarbeiter','-role','Entwicklerin','-department','Server','-presence','office'],env=env,text=True,capture_output=True,check=True)
        def choose(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Mitarbeiterkonflikt entscheiden']
                if not ds:return True
                assert 'Lokal' in ds[-1].get_property('secondary-text') and 'Server' in ds[-1].get_property('secondary-text')
                ds[-1].response(response);return False
            return reply
        def done(title):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')==title]
                if not ds:return True
                ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
            return reply
        def open_conflict(response):
            fill('Mitarbeiterkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose(response))
            buttons=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Mitarbeiterkonflikt prüfen'];assert len(buttons)==1;buttons[0].clicked()
        open_conflict(mod.Gtk.ResponseType.CANCEL);wait(lambda:not w.employee_sync_running)
        mod.GLib.timeout_add(1000,done('Lokaler Mitarbeiter vorgemerkt'));open_conflict(mod.Gtk.ResponseType.OK);wait(lambda:not w.employee_sync_running)
        fill('Mitarbeiterübertragung zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Mitarbeiter synchronisieren'][0].clicked();wait(lambda:not w.employee_sync_running)
        current=json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout)
        assert next(v for v in current if v['id']==remote['id'])['department']=='Lokal'
        w.local.save_employee(local['id'],'GUI Mitarbeiter','Entwicklerin','Lokal Zweit')
        subprocess.run([str(client),'-action','central-employee-update','-id',remote['id'],'-name','GUI Mitarbeiter','-role','Entwicklerin','-department','Server Zweit','-presence','office'],env=env,text=True,capture_output=True,check=True)
        def accept_confirm():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Servermitarbeiter lokal übernehmen?']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.timeout_add(700,accept_confirm);mod.GLib.timeout_add(1000,done('Servermitarbeiter übernommen'));open_conflict(mod.Gtk.ResponseType.YES);wait(lambda:not w.employee_sync_running)
        assert next(v for v in w.local.employees() if v['id']==local['id'])['department']=='Server Zweit'
        # Same names do not prove identity: the dialog requires a second,
        # explicit confirmation before linking two independent records.
        remote_dupe=json.loads(subprocess.run([str(client),'-action','central-employee-create','-name','Doppel','-role','Technik','-department','Server','-presence','office'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.save_employee(0,'Doppel','Technik','Lokal');local_dupe=next(v for v in w.local.employees() if v['name']=='Doppel')
        def identity_confirm(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Mitarbeiteridentität bestätigen?']
                if not ds:return True
                assert 'unabhängig geprüft' in ds[-1].get_property('secondary-text');ds[-1].response(response);return False
            return reply
        mod.GLib.timeout_add(700,identity_confirm(mod.Gtk.ResponseType.CANCEL));open_conflict(mod.Gtk.ResponseType.OK);wait(lambda:not w.employee_sync_running)
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]==1
        mod.GLib.timeout_add(700,identity_confirm(mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(900,accept_confirm);mod.GLib.timeout_add(1300,done('Servermitarbeiter übernommen'))
        open_conflict(mod.Gtk.ResponseType.YES);wait(lambda:not w.employee_sync_running)
        assert next(v for v in w.local.employees() if v['id']==local_dupe['id'])['department']=='Server'
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(local_dupe['id'],)).fetchone()[0]==remote_dupe['id']
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Auditprotokoll'][0].clicked()
        wait(lambda:len(children(w.content,mod.Gtk.Expander))>=3)
        history=children(w.content,mod.Gtk.Expander)
        assert any('Serverstand übernommen' in v.get_label() for v in history)
        assert any('Lokaler Stand vorgemerkt' in v.get_label() for v in history)
        assert any('Server Zweit' in ' '.join(x.get_text() for x in children(v,mod.Gtk.Label)) for v in history)
        w.select(5)
        w.employee_auto_interval=1
        auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Mitarbeiter automatisch' in v.get_label()];assert len(auto)==1
        auto[0].set_active(True);wait(lambda:not w.employee_sync_running);assert w.employee_auto_enabled
        w.local.save_employee(local['id'],'GUI Mitarbeiter','Entwicklerin','Auto')
        def pending():
            with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:return sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]
        wait(lambda:pending()==0 and not w.employee_sync_running)
        current=json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout)
        assert next(v for v in current if v['id']==remote['id'])['department']=='Auto'
        process.terminate();process.wait(timeout=5);w.local.save_employee(local['id'],'GUI Mitarbeiter','Entwicklerin','Offline')
        wait(lambda:'vorübergehend' in w.employee_auto_error and not w.employee_sync_running);assert pending()==1 and w.employee_auto_enabled
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        wait(lambda:pending()==0 and not w.employee_sync_running);assert w.employee_auto_enabled
        subprocess.run([str(client),'-action','central-employee-update','-id',remote['id'],'-name','GUI Mitarbeiter','-role','Entwicklerin','-department','Server Konflikt','-presence','office'],env=env,text=True,capture_output=True,check=True)
        w.local.save_employee(local['id'],'GUI Mitarbeiter','Entwicklerin','Lokal Konflikt')
        wait(lambda:not w.employee_auto_enabled and not w.employee_sync_running);assert pending()==1 and '409' in w.employee_auto_error
        mod.GLib.timeout_add(700,accept_confirm);mod.GLib.timeout_add(1000,done('Servermitarbeiter übernommen'));open_conflict(mod.Gtk.ResponseType.YES);wait(lambda:not w.employee_sync_running)
        auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Mitarbeiter automatisch' in v.get_label()];auto[0].set_active(True);wait(lambda:not w.employee_sync_running);assert w.employee_auto_enabled
        fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.employee_auto_enabled and w.employee_auto_source==0
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK employee synchronization: OK')

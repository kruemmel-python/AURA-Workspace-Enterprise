#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-room-sync-gui-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='sync-manager'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['rooms.manage']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_room(0,'GUI Raum','Berlin',5,'');setup.close()
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
        def dismiss_setup_warning():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible()]
            if not ds:return True
            assert 'zuerst' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        no_setup=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Räume automatisch' in v.get_label()][0]
        mod.GLib.idle_add(dismiss_setup_warning);no_setup.set_active(True);assert not no_setup.get_active() and not w.room_auto_enabled and w.room_auto_source==0
        def confirm():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokale Räume synchronisieren?']
            if not ds:return True
            assert 'Urlaub' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finish():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Raumabgleich abgeschlossen']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        subprocess.run([str(client),'-action','create-room','-name','Server Raum','-location','Kiel','-capacity','3'],env=env,text=True,capture_output=True,check=True)
        fill('Raumübertragung zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        buttons=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Räume synchronisieren'];assert len(buttons)==1;buttons[0].clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        rooms=json.loads(subprocess.run([str(client),'-action','rooms'],env=env,text=True,capture_output=True,check=True).stdout);assert {v['name'] for v in rooms}=={'GUI Raum','Server Raum'};assert {v['name'] for v in w.local.rooms()}=={'GUI Raum','Server Raum'};assert w.local.next_room_sync()['pending']==0
        gui_room=next(v for v in rooms if v['name']=='GUI Raum');local_room=next(v for v in w.local.rooms() if v['name']=='GUI Raum')
        w.local.save_room(local_room['id'],'GUI Raum','Bonn',6,'Lokal')
        subprocess.run([str(client),'-action','update-room','-id',gui_room['id'],'-name','GUI Raum','-location','Hamburg','-capacity','5','-equipment','Server'],env=env,text=True,capture_output=True,check=True)
        def conflict_dialog(response,local="Bonn",remote="Hamburg"):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Raumkonflikt entscheiden']
                if not ds:return True
                detail=ds[-1].get_property('secondary-text');assert local in detail and remote in detail and 'Serverstand' in detail
                ds[-1].response(response);return False
            return reply
        def conflict_done():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokaler Raumstand vorgemerkt']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        def conflict_button():
            found=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Raumkonflikt prüfen'];assert len(found)==1;return found[0]
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,conflict_dialog(mod.Gtk.ResponseType.CANCEL));conflict_button().clicked();wait(lambda:not w.room_sync_running);assert w.local.next_room_sync()['pending']==1
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,conflict_dialog(mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(1000,conflict_done);conflict_button().clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        fill('Raumübertragung zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish);buttons[0].clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        rooms=json.loads(subprocess.run([str(client),'-action','rooms'],env=env,text=True,capture_output=True,check=True).stdout);updated=next(v for v in rooms if v['id']==gui_room['id']);assert updated['location']=='Bonn' and updated['capacity']==6
        w.local.save_room(local_room['id'],'GUI Raum','Lokal Zweitstand',7,'Lokal')
        subprocess.run([str(client),'-action','update-room','-id',gui_room['id'],'-name','GUI Raum','-location','Server Neu','-capacity','5','-equipment','Server'],env=env,text=True,capture_output=True,check=True)
        def server_confirm(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serverstand lokal übernehmen?']
                if not ds:return True
                assert 'Warteschlange entfernt' in ds[-1].get_property('secondary-text');ds[-1].response(response);return False
            return reply
        def server_done():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serverstand übernommen']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,conflict_dialog(mod.Gtk.ResponseType.YES,'Lokal Zweitstand','Server Neu'));mod.GLib.timeout_add(700,server_confirm(mod.Gtk.ResponseType.CANCEL));conflict_button().clicked();wait(lambda:not w.room_sync_running)
        assert w.local.next_room_sync()['pending']==1 and next(v for v in w.local.rooms() if v['id']==local_room['id'])['location']=='Lokal Zweitstand'
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,conflict_dialog(mod.Gtk.ResponseType.YES,'Lokal Zweitstand','Server Neu'));mod.GLib.timeout_add(700,server_confirm(mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(1100,server_done);conflict_button().clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        assert w.local.next_room_sync()['pending']==0 and next(v for v in w.local.rooms() if v['id']==local_room['id'])['location']=='Server Neu'
        # Two independently created rooms can be explicitly identified as one.
        created=json.loads(subprocess.run([str(client),'-action','create-room','-name','Gleichraum','-location','Zentrale','-capacity','5','-equipment','Server'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.save_room(0,'Gleichraum','Lokal',4,'Monitor');same=next(v for v in w.local.rooms() if v['name']=='Gleichraum')
        try:mod.sync_rooms(w.local,w.local_lock,lambda:True,client,url,token,str(cert));raise AssertionError('duplicate identity accepted')
        except mod.LocalStoreError:pass
        def identity_dialog():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Raumkonflikt entscheiden']
            if not ds:return True
            detail=ds[-1].get_property('secondary-text');assert 'unterschiedliche Kennungen' in detail and 'Lokal' in detail and 'Zentrale' in detail
            ds[-1].response(mod.Gtk.ResponseType.YES);return False
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,identity_dialog);mod.GLib.timeout_add(700,server_confirm(mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(1100,server_done)
        conflict_button().clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        assert w.local.next_room_sync()['pending']==0 and next(v for v in w.local.rooms() if v['id']==same['id'])['location']=='Zentrale'
        history=w.local.room_resolution_history();assert len(history)>=3 and any('Zentrale' in item['remote'] and item['choice']=='server' and item['identity_mapping']==1 and item['actor_employee_id']==w.local.session_employee() for item in history)
        assert any(item['choice']=='local' and item['identity_mapping']==0 for item in history) and any(item['choice']=='server' and item['identity_mapping']==0 for item in history)
        w.show_audit();wait(lambda:any(v.get_label()=='Raum-Synchronisationsentscheidungen' for v in children(w.content,mod.Gtk.Label)))
        expanders=children(w.content,mod.Gtk.Expander);assert any('Raum '+str(same['id']) in e.get_label() and 'Serverstand übernommen' in e.get_label() and 'Identität zugeordnet' in e.get_label() and 'Zentrale' in ' '.join(v.get_text() for v in children(e,mod.Gtk.Label)) for e in expanders)
        w.select(5);wait(lambda:any(v.get_label()=='Raumkonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
        assert mod.sync_rooms(w.local,w.local_lock,lambda:True,client,url,token,str(cert))['sent']==0
        w.room_auto_interval=1
        auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Räume automatisch' in v.get_label()];assert len(auto)==1;auto[0].set_active(True);wait(lambda:not w.room_sync_running);assert w.room_auto_enabled and w.room_auto_source
        w.local.save_room(local_room['id'],'GUI Raum','Auto Bonn',8,'Lokal')
        wait(lambda:w.local.next_room_sync()['pending']==0 and not w.room_sync_running)
        rooms=json.loads(subprocess.run([str(client),'-action','rooms'],env=env,text=True,capture_output=True,check=True).stdout);assert next(v for v in rooms if v['id']==gui_room['id'])['location']=='Auto Bonn'
        subprocess.run([str(client),'-action','update-room','-id',gui_room['id'],'-name','GUI Raum','-location','Server Live','-capacity','8','-equipment','Lokal'],env=env,text=True,capture_output=True,check=True)
        wait(lambda:next(v for v in w.local.rooms() if v['id']==local_room['id'])['location']=='Server Live' and not w.room_sync_running)
        process.terminate();process.wait(timeout=5);w.local.save_room(local_room['id'],'GUI Raum','Offline Kiel',8,'Lokal');wait(lambda:'vorübergehend' in w.room_auto_error and not w.room_sync_running)
        assert w.room_auto_enabled and w.local.next_room_sync()['pending']==1 and 'vorübergehend' in w.room_auto_error
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        wait(lambda:w.local.next_room_sync()['pending']==0 and not w.room_sync_running);assert w.room_auto_enabled
        rooms=json.loads(subprocess.run([str(client),'-action','rooms'],env=env,text=True,capture_output=True,check=True).stdout);assert next(v for v in rooms if v['id']==gui_room['id'])['location']=='Offline Kiel'
        subprocess.run([str(client),'-action','update-room','-id',gui_room['id'],'-name','GUI Raum','-location','Fremdänderung','-capacity','5','-equipment','Server'],env=env,text=True,capture_output=True,check=True)
        w.local.save_room(local_room['id'],'GUI Raum','Lokaler Konflikt',9,'Lokal');wait(lambda:not w.room_auto_enabled and not w.room_sync_running)
        assert not w.room_auto_enabled and w.local.next_room_sync()['pending']==1 and '409' in w.room_auto_error
        checks=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Räume automatisch' in v.get_label()];assert len(checks)==1 and not checks[0].get_active()
        fill('Raumkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,conflict_dialog(mod.Gtk.ResponseType.YES,'Lokaler Konflikt','Fremdänderung'));mod.GLib.timeout_add(700,server_confirm(mod.Gtk.ResponseType.OK));mod.GLib.timeout_add(1100,server_done);conflict_button().clicked();wait(lambda:not w.room_sync_running);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
        checks=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Räume automatisch' in v.get_label()];checks[0].set_active(True);wait(lambda:not w.room_sync_running);assert w.room_auto_enabled
        fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.room_auto_enabled and w.room_auto_source==0 and w.local is None
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK room conflict decisions and automatic sync: offline retry and conflict stop: OK')

#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-task-sync-gui-') as tmp:
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
        assert len(tasks)==1 and tasks[0]['title']=='GUI Aufgabe' and tasks[0]['recurrenceDays']==7
        subtasks=json.loads(subprocess.run([str(client),'-action','sync-subtasks'],env=env,text=True,capture_output=True,check=True).stdout);assert len(subtasks)==1 and subtasks[0]['title']=='GUI Unteraufgabe'
        remote=tasks[0];local=next(v for v in w.local.tasks() if v['title']=='GUI Aufgabe');employee=w.local.session_employee()
        w.local.save_task(local['id'],'Lokal','IT','Mittel',employee,1900000000,7)
        subprocess.run([str(client),'-action','update-task','-id',remote['id'],'-title','Server','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900000000','-recurrence','7'],env=env,text=True,capture_output=True,check=True)
        def choose(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Aufgabenkonflikt entscheiden']
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
            fill('Aufgabenkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose(response))
            [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Aufgabenkonflikt prüfen'][0].clicked()
        review(mod.Gtk.ResponseType.CANCEL);wait(lambda:not w.task_sync_running)
        mod.GLib.timeout_add(1000,done('Lokale Aufgabe vorgemerkt'));review(mod.Gtk.ResponseType.OK);wait(lambda:not w.task_sync_running)
        fill('Aufgabenabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Aufgaben synchronisieren'][0].clicked();wait(lambda:not w.task_sync_running)
        assert json.loads(subprocess.run([str(client),'-action','sync-tasks'],env=env,text=True,capture_output=True,check=True).stdout)[0]['title']=='Lokal'
        w.local.save_task(local['id'],'Lokal Zwei','IT','Mittel',employee,1900000000,7)
        subprocess.run([str(client),'-action','update-task','-id',remote['id'],'-title','Server Zwei','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900000000','-recurrence','7'],env=env,text=True,capture_output=True,check=True)
        def confirm_server():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serveraufgabe lokal übernehmen?']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,done('Serveraufgabe übernommen'));review(mod.Gtk.ResponseType.YES);wait(lambda:not w.task_sync_running)
        assert next(v for v in w.local.tasks() if v['id']==local['id'])['title']=='Server Zwei'
        identity_remote=json.loads(subprocess.run([str(client),'-action','create-task','-title','Identische Aufgabe','-department','Server','-priority','Mittel','-assignee-id','9'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.save_task(0,'Identische Aufgabe','Lokal','Hoch',employee,0,0)
        identity_local=next(v for v in w.local.tasks() if v['title']=='Identische Aufgabe')
        def choose_identity():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Aufgabenkonflikt entscheiden']
            if not ds:return True
            detail=ds[-1].get_property('secondary-text');assert 'Unabhängig angelegte Aufgaben' in detail and 'Server' in detail and 'Lokal' in detail
            ds[-1].response(mod.Gtk.ResponseType.YES);return False
        fill('Aufgabenkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose_identity);mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,done('Serveraufgabe übernommen'))
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Aufgabenkonflikt prüfen'][0].clicked();wait(lambda:not w.task_sync_running)
        assert next(v for v in w.local.tasks() if v['id']==identity_local['id'])['department']=='Server'
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(identity_local['id'],)).fetchone()[0]==identity_remote['id']
        frozen_remote=json.loads(subprocess.run([str(client),'-action','create-task','-title','Vorbereitete GUI Aufgabe','-department','Server','-priority','Mittel','-assignee-id','9'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.save_task(0,'Vorbereitete GUI Aufgabe','Lokal','Hoch',employee,0,0)
        import task_sync as task_sync_module
        original_connect=task_sync_module.connect_sync_server
        def intercept(*args):
            normalized,actor,command=original_connect(*args)
            def blocked(*params):
                if params[:2]==('-action','sync-task-event'):raise mod.LocalStoreError('Simulierter Netzwerkausfall')
                return command(*params)
            return normalized,actor,blocked
        task_sync_module.connect_sync_server=intercept
        try:
            try:task_sync_module.sync_tasks(w.local,w.local_lock,lambda:w.local_mode,client,url,token,str(cert));raise AssertionError('prepared sync did not fail')
            except mod.LocalStoreError:pass
        finally:task_sync_module.connect_sync_server=original_connect
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:original=json.loads(sql.execute("SELECT wire_json FROM linux_task_sync_outbox WHERE json_extract(task_json,'$.title')='Vorbereitete GUI Aufgabe'").fetchone()[0])
        fill('Aufgabenkonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose_identity);mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,done('Serveraufgabe übernommen'))
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Aufgabenkonflikt prüfen'][0].clicked();wait(lambda:not w.task_sync_running)
        selected=next(v for v in w.local.tasks() if v['title']=='Vorbereitete GUI Aufgabe');assert selected['department']=='Server'
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(selected['id'],)).fetchone()[0]==frozen_remote['id']
        replay=subprocess.run([str(client),'-action','sync-task-event','-payload',json.dumps(original)],env=env,text=True,capture_output=True)
        assert replay.returncode and '409' in replay.stderr
        w.task_auto_interval=1;auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Aufgaben automatisch' in v.get_label()][0]
        auto.set_active(True);wait(lambda:not w.task_sync_running);assert w.task_auto_enabled
        with w.local_lock:
            w.local.save_task(0,'Ohne Person','IT','Mittel',0,0,0)
            unassigned=next(v for v in w.local.tasks() if v['title']=='Ohne Person')
            w.local.save_task(local['id'],'Unabhängig gesendet','IT','Mittel',employee,1900000000,7)
        subprocess.run([str(client),'-action','create-task','-title','Unabhängig empfangen','-department','IT','-priority','Mittel','-assignee-id','9'],env=env,text=True,capture_output=True,check=True)
        wait(lambda:'zuständigen Person' in w.task_auto_error and not w.task_sync_running)
        assert w.task_auto_enabled
        assert any(v['title']=='Unabhängig empfangen' for v in w.local.tasks())
        assert next(v for v in json.loads(subprocess.run([str(client),'-action','sync-tasks'],env=env,text=True,capture_output=True,check=True).stdout) if v['id']==remote['id'])['title']=='Unabhängig gesendet'
        with w.local_lock:w.local.save_task(unassigned['id'],'Nun zugeordnet','IT','Mittel',employee,0,0)
        def assigned_sent():
            return any(v['title']=='Nun zugeordnet' for v in json.loads(subprocess.run([str(client),'-action','sync-tasks'],env=env,text=True,capture_output=True,check=True).stdout))
        wait(lambda:assigned_sent() and not w.task_sync_running);assert w.task_auto_enabled
        process.terminate();process.wait(timeout=5);w.local.save_task(local['id'],'Offline Aufgabe','IT','Mittel',employee,1900000000,7)
        wait(lambda:'vorübergehend' in w.task_auto_error and not w.task_sync_running);assert w.task_auto_enabled
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        def server_title():
            return next(v for v in json.loads(subprocess.run([str(client),'-action','sync-tasks'],env=env,text=True,capture_output=True,check=True).stdout) if v['id']==remote['id'])['title']
        wait(lambda:not w.task_sync_running and server_title()=='Offline Aufgabe');assert w.task_auto_enabled
        subprocess.run([str(client),'-action','update-task','-id',remote['id'],'-title','Server Konflikt','-department','IT','-priority','Mittel','-assignee-id','9','-due','1900000000','-recurrence','7'],env=env,text=True,capture_output=True,check=True)
        w.local.save_task(local['id'],'Lokal Konflikt','IT','Mittel',employee,1900000000,7)
        wait(lambda:not w.task_auto_enabled and not w.task_sync_running);assert '409' in w.task_auto_error
        fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.task_auto_enabled and not w.task_auto_source
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK task sync: OK')

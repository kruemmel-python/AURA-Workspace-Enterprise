#!/usr/bin/python3
import importlib.util,json,os,socket,ssl,sqlite3,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-leave-sync-gui-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='sync-manager'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['employees.manage','leave.request','leave.approve']},'ada-token':{'employeeId':8,'permissions':['leave.request']}}),'AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
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
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.create_leave(1900000000,1900100000,'GUI Urlaub');setup.save_employee(0,'Ada','Entwicklerin','IT')
        from employee_sync import sync_employees
        import threading
        sync_employees(setup,threading.Lock(),lambda:True,client,url,token,str(cert))
        staff=json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout)
        admin=next(v for v in staff if v['name']=='Admin')
        subprocess.run([str(client),'-action','employee-account-link','-id',admin['id'],'-account-id','9'],env=env,text=True,capture_output=True,check=True)
        ada=next(v for v in staff if v['name']=='Ada')
        subprocess.run([str(client),'-action','employee-account-link','-id',ada['id'],'-account-id','8'],env=env,text=True,capture_output=True,check=True)
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
        fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode);w.select(3);wait(lambda:any(isinstance(v,mod.Gtk.Button) and v.get_label()=='Urlaub synchronisieren' for v in children(w.content,mod.Gtk.Button)))
        # Exercise the personal presence button against the actual server.
        with w.local_lock:w.local.employee_presence(w.local.session_employee(),'remote')
        notices=[];original_message=w.message;w.message=lambda message:notices.append(message)
        fill('Eigene Präsenz synchronisieren',[url,token,str(cert)])
        next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Eigene Präsenz synchronisieren').clicked()
        wait(lambda:not w.employee_sync_running and bool(notices));assert '1 eigene Präsenzänderungen' in notices[-1],notices
        w.message=original_message
        assert next(v for v in json.loads(subprocess.run([str(client),'-action','sync-employees'],env=env,text=True,capture_output=True,check=True).stdout) if v['id']==admin['id'])['presence']=='remote'
        wait(lambda:any(v.get_label()=='Urlaub synchronisieren' for v in children(w.content,mod.Gtk.Button)))
        subprocess.run([str(client),'-action','set-my-presence','-status','remote','-presence','away'],env=env,text=True,capture_output=True,check=True)
        notices=[];w.message=lambda message:notices.append(message)
        fill('Eigene Präsenz synchronisieren',[url,token,str(cert)])
        next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Eigene Präsenz synchronisieren').clicked()
        wait(lambda:not w.employee_sync_running and bool(notices));assert '1 Serverstatus übernommen' in notices[-1],notices
        w.message=original_message
        assert next(v for v in w.local.employees() if v['isSelf'])['presence']=='away'
        wait(lambda:any(v.get_label()=='Urlaub synchronisieren' for v in children(w.content,mod.Gtk.Button)))
        # Automatic personal synchronization retries an offline server, then
        # stops on a real concurrent edit without discarding either change.
        wait(lambda:any(isinstance(v,mod.Gtk.CheckButton) and 'Eigene Präsenz automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
        w.presence_auto_interval=1
        next(v for v in children(w.content,mod.Gtk.CheckButton) if 'Eigene Präsenz automatisch' in v.get_label()).set_active(True)
        wait(lambda:not w.employee_sync_running);assert w.presence_auto_enabled
        process.terminate();process.wait(timeout=5)
        with w.local_lock:w.local.employee_presence(w.local.session_employee(),'office')
        wait(lambda:'vorübergehend' in w.presence_auto_error and not w.employee_sync_running);assert w.presence_auto_enabled
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        def presence_pending():
            with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:return sql.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]
        wait(lambda:presence_pending()==0 and not w.employee_sync_running)
        assert json.loads(subprocess.run([str(client),'-action','my-presence'],env=env,text=True,capture_output=True,check=True).stdout)['presence']=='office'
        w.employee_sync_running=True
        with w.local_lock:w.local.employee_presence(w.local.session_employee(),'remote')
        subprocess.run([str(client),'-action','set-my-presence','-status','office','-presence','meeting'],env=env,text=True,capture_output=True,check=True)
        w.employee_sync_running=False
        wait(lambda:not w.presence_auto_enabled and not w.employee_sync_running);assert presence_pending()==1 and 'Serverstatus' in w.presence_auto_error
        subprocess.run([str(client),'-action','set-my-presence','-status','meeting','-presence','office'],env=env,text=True,capture_output=True,check=True)
        wait(lambda:any(v.get_label()=='Eigene Präsenz synchronisieren' for v in children(w.content,mod.Gtk.Button)))
        notices=[];w.message=lambda message:notices.append(message);fill('Eigene Präsenz synchronisieren',[url,token,str(cert)])
        next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Eigene Präsenz synchronisieren').clicked()
        wait(lambda:not w.employee_sync_running and bool(notices));assert presence_pending()==0
        w.message=original_message
        wait(lambda:any(v.get_label()=='Urlaub synchronisieren' for v in children(w.content,mod.Gtk.Button)))
        def confirm():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Urlaub synchronisieren?']
            if not ds:return True
            assert 'persönlichen Serverkonto' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finish():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Urlaubsabgleich abgeschlossen']
            if not ds:return True
            assert '1 Änderungen übertragen' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        fill('Urlaubsabgleich zum Server',[url,token,str(cert)]);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish)
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Urlaub synchronisieren'][0].clicked();wait(lambda:not w.leave_sync_running)
        leaves=json.loads(subprocess.run([str(client),'-action','leave'],env=env,text=True,capture_output=True,check=True).stdout)
        assert len(leaves)==1 and leaves[0]['reason']=='GUI Urlaub' and leaves[0]['employeeId']==9
        assert next(v for v in w.local.leave() if v['reason']=='GUI Urlaub')['syncState']=='confirmed'
        # A previously imported, wrongly attributed local request needs an
        # explicit review after the server identity is reconciled.
        legacy={'id':'old-client:leave:10','employeeId':77,'startsAt':1900800000,'endsAt':1900900000,'reason':'GUI Altantrag','status':'pending','decidedBy':0}
        event={'topic':'leave.requested','payload':json.dumps(legacy)}
        req=urllib.request.Request(url+'/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer ada-token','Content-Type':'application/json'})
        assert urllib.request.urlopen(req,context=ctx,timeout=5).status==202
        mapping={'expected':legacy,'employeeExternalId':ada['id'],'deciderExternalId':''}
        subprocess.run([str(client),'-action','legacy-leave-reconcile','-payload',json.dumps(mapping)],env=env,text=True,capture_output=True,check=True)
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:
            sql.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
            wrong_id=sql.execute("INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason,status,decided_by,active) VALUES(?,?,?,?,?,0,1)",(w.local.session_employee(),legacy['startsAt'],legacy['endsAt'],legacy['reason'],'pending')).lastrowid
            sql.execute("INSERT INTO sync_object_links VALUES('leave',?,?)",(legacy['id'],wrong_id))
            sql.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?)',(wrong_id,json.dumps({**legacy,'employeeId':9})))
            sql.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
        def approve_local_mapping():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Lokale Urlaubszuordnung ersetzen?']
            if not ds:return True
            content=ds[-1].get_property('secondary-text');assert 'Admin' in content and 'Ada' in content and 'GUI Altantrag' in content
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        messages=[];prior_message=w.message;w.message=lambda value:messages.append(value)
        wait(lambda:any(v.get_label()=='Lokale Urlaubszuordnung prüfen' for v in children(w.content,mod.Gtk.Button)))
        fill('Lokale Urlaubszuordnung prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,approve_local_mapping)
        next(v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Lokale Urlaubszuordnung prüfen').clicked()
        wait(lambda:not w.leave_sync_running and bool(messages));assert 'übernommen und protokolliert' in messages[-1],messages
        w.message=prior_message
        assert next(v for v in w.local.leave() if v['id']==wrong_id)['employeeId']==next(v['id'] for v in w.local.employees() if v['name']=='Ada')
        # A competing decision is shown and can be deliberately accepted.
        ada_env=env.copy();ada_env['AURA_SYNC_TOKEN']='ada-token'
        remote=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1900200000','-ends-at','1900300000','-reason','Konflikturlaub'],env=ada_env,text=True,capture_output=True,check=True).stdout)
        from leave_sync import sync_leave
        assert sync_leave(w.local,w.local_lock,lambda:True,client,url,token,str(cert))['received']==1
        local=next(v for v in w.local.leave() if v['reason']=='Konflikturlaub');w.local.decide_leave(local['id'],True)
        subprocess.run([str(client),'-action','leave-decision','-id',remote['id'],'-status','rejected'],env=env,text=True,capture_output=True,check=True)
        def choose(response):
            def reply():
                ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Urlaubskonflikt entscheiden']
                if not ds:return True
                assert 'approved' in ds[-1].get_property('secondary-text') and 'rejected' in ds[-1].get_property('secondary-text');ds[-1].response(response);return False
            return reply
        def confirm_server():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serverantrag lokal übernehmen?']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        def finished_server():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Serverantrag übernommen']
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
        def conflict(response):
            wait(lambda:any(v.get_label()=='Urlaubskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
            fill('Urlaubskonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose(response))
            [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Urlaubskonflikt prüfen'][0].clicked()
        wait(lambda:any(v.get_label()=='Urlaubskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
        conflict(mod.Gtk.ResponseType.CANCEL);wait(lambda:not w.leave_sync_running)
        assert next(v for v in w.local.leave() if v['id']==local['id'])['status']=='approved'
        mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,finished_server);conflict(mod.Gtk.ResponseType.OK);wait(lambda:not w.leave_sync_running)
        assert next(v for v in w.local.leave() if v['id']==local['id'])['status']=='rejected'
        independent=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1902400000','-ends-at','1902500000','-reason','Eigener doppelter Antrag'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.create_leave(1902400000,1902500000,'Eigener doppelter Antrag')
        local_independent=next(v for v in w.local.leave() if v['reason']=='Eigener doppelter Antrag')
        def choose_identity():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Urlaubskonflikt entscheiden']
            if not ds:return True
            assert 'Unabhängig angelegte Anträge' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        wait(lambda:any(v.get_label()=='Urlaubskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
        fill('Urlaubskonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose_identity);mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,finished_server)
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Urlaubskonflikt prüfen'][0].clicked();wait(lambda:not w.leave_sync_running)
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='leave' AND local_id=?",(local_independent['id'],)).fetchone()[0]==independent['id']
        prepared=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1902600000','-ends-at','1902700000','-reason','Vorbereiteter GUI Urlaub'],env=env,text=True,capture_output=True,check=True).stdout)
        w.local.create_leave(1902600000,1902700000,'Vorbereiteter GUI Urlaub')
        import leave_sync as leave_sync_module
        original_connect=leave_sync_module.connect_sync_server
        def intercept(*args):
            normalized,actor,command=original_connect(*args)
            def blocked(*params):
                if params[:2]==('-action','sync-leave-event'):raise mod.LocalStoreError('Simulierter Netzwerkausfall')
                return command(*params)
            return normalized,actor,blocked
        leave_sync_module.connect_sync_server=intercept
        try:
            try:leave_sync_module.sync_leave(w.local,w.local_lock,lambda:w.local_mode,client,url,token,str(cert));raise AssertionError('prepared leave sync did not fail')
            except mod.LocalStoreError:pass
        finally:leave_sync_module.connect_sync_server=original_connect
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:original=json.loads(sql.execute("SELECT wire_json FROM linux_leave_sync_outbox WHERE json_extract(leave_json,'$.reason')='Vorbereiteter GUI Urlaub'").fetchone()[0])
        wait(lambda:any(v.get_label()=='Urlaubskonflikt prüfen' for v in children(w.content,mod.Gtk.Button)))
        fill('Urlaubskonflikt prüfen',[url,token,str(cert)]);mod.GLib.timeout_add(500,choose_identity);mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,finished_server)
        [v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Urlaubskonflikt prüfen'][0].clicked();wait(lambda:not w.leave_sync_running)
        selected=next(v for v in w.local.leave() if v['reason']=='Vorbereiteter GUI Urlaub')
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='leave' AND local_id=?",(selected['id'],)).fetchone()[0]==prepared['id']
        replay=subprocess.run([str(client),'-action','sync-leave-event','-payload',json.dumps(original)],env=env,text=True,capture_output=True)
        assert replay.returncode and '409' in replay.stderr
        wait(lambda:any(isinstance(v,mod.Gtk.CheckButton) and 'Urlaub automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
        w.leave_auto_interval=1;auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Urlaub automatisch' in v.get_label()][0]
        auto.set_active(True);wait(lambda:not w.leave_sync_running);assert w.leave_auto_enabled
        # The approver's automatic sync must wait for the applicant's older
        # operation and resume once that person has sent the request.
        ada_local=next(v for v in w.local.employees() if v['name']=='Ada')
        with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:
            sql.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'ada',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(ada_local['id'],))
        ada_session=mod.LocalStore(os.environ['AURA_LOCAL_DB']);ada_session.open();ada_session.login('ada','Admin Passwort 2026')
        with w.local_lock:
            ada_session.create_leave(1902200000,1902300000,'Wartender Antrag')
            entry=next(v for v in w.local.leave() if v['reason']=='Wartender Antrag');w.local.decide_leave(entry['id'],True)
        wait(lambda:'antragstellende Person' in w.leave_auto_error and not w.leave_sync_running)
        assert w.leave_auto_enabled
        from leave_sync import sync_leave
        sync_leave(ada_session,w.local_lock,lambda:True,client,url,'ada-token',str(cert))
        def decision_sent():
            with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:return sql.execute('SELECT count(*) FROM linux_leave_sync_outbox WHERE leave_id=?',(entry['id'],)).fetchone()[0]==0
        wait(lambda:decision_sent() and not w.leave_sync_running)
        assert w.leave_auto_enabled
        server_leaves=json.loads(subprocess.run([str(client),'-action','leave'],env=env,text=True,capture_output=True,check=True).stdout)
        assert next(v for v in server_leaves if v['reason']=='Wartender Antrag')['status']=='approved'
        ada_session.close()
        process.terminate();process.wait(timeout=5);w.local.create_leave(1900400000,1900500000,'Offline Urlaub')
        assert next(v for v in w.local.leave() if v['reason']=='Offline Urlaub')['syncState']=='pending'
        w.select(3);wait(lambda:'Offline Urlaub' in ' '.join(x.get_text() for x in children(w.content,mod.Gtk.Label)))
        assert 'Übertragung ausstehend' in ' '.join(x.get_text() for x in children(w.content,mod.Gtk.Label))
        wait(lambda:'vorübergehend' in w.leave_auto_error and not w.leave_sync_running);assert w.leave_auto_enabled
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server did not restart')
        def pending():
            with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:return sql.execute('SELECT count(*) FROM linux_leave_sync_outbox').fetchone()[0]
        wait(lambda:pending()==0 and not w.leave_sync_running);assert w.leave_auto_enabled
        current=json.loads(subprocess.run([str(client),'-action','leave'],env=env,text=True,capture_output=True,check=True).stdout)
        assert any(v['reason']=='Offline Urlaub' for v in current)
        other=json.loads(subprocess.run([str(client),'-action','create-leave','-starts-at','1900600000','-ends-at','1900700000','-reason','Auto Konflikt'],env=ada_env,text=True,capture_output=True,check=True).stdout)
        def imported():
            with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as sql:return sql.execute("SELECT id FROM leave_requests WHERE reason='Auto Konflikt'").fetchone()
        wait(lambda:imported() and not w.leave_sync_running)
        with w.local_lock:w.local.decide_leave(imported()[0],True)
        subprocess.run([str(client),'-action','leave-decision','-id',other['id'],'-status','rejected'],env=env,text=True,capture_output=True,check=True)
        wait(lambda:not w.leave_auto_enabled and not w.leave_sync_running);assert pending()==1 and '409' in w.leave_auto_error
        mod.GLib.timeout_add(700,confirm_server);mod.GLib.timeout_add(1000,finished_server);conflict(mod.Gtk.ResponseType.OK);wait(lambda:not w.leave_sync_running)
        wait(lambda:any(isinstance(v,mod.Gtk.CheckButton) and 'Urlaub automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
        auto=[v for v in children(w.content,mod.Gtk.CheckButton) if 'Urlaub automatisch' in v.get_label()][0];auto.set_active(True);wait(lambda:not w.leave_sync_running);assert w.leave_auto_enabled
        wait(lambda:any(isinstance(v,mod.Gtk.CheckButton) and 'Eigene Präsenz automatisch' in v.get_label() for v in children(w.content,mod.Gtk.CheckButton)))
        next(v for v in children(w.content,mod.Gtk.CheckButton) if 'Eigene Präsenz automatisch' in v.get_label()).set_active(True)
        wait(lambda:not w.employee_sync_running);assert w.presence_auto_enabled
        fill('Serververbindung',[url,token,str(cert)]);w.connection();wait(lambda:not w.local_mode);assert not w.leave_auto_enabled and not w.leave_auto_source
        assert not w.presence_auto_enabled and not w.presence_auto_source and w.presence_auto_credentials is None
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
        with w.local_lock:
            if w.local:w.local.close()
    finally:process.terminate();process.wait(timeout=5)
print('GTK leave synchronization: OK')

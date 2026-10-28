#!/usr/bin/python3
"""Verify room controls and booking ownership in real GTK sessions."""
import importlib.util,json,os,socket,sqlite3,subprocess,tempfile,time,urllib.request
from datetime import datetime
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-booking-permissions-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite'
    for key in ('AURA_SYNC_URL','AURA_SYNC_TOKEN','AURA_SYNC_CA_CERT'):os.environ.pop(key,None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    admin=mod.LocalStore(os.environ['AURA_LOCAL_DB']);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    admin.save_room(0,'Konferenz','Berlin',5,'');room=admin.rooms()[0];admin.save_employee(0,'Mitarbeiter','Entwicklung','IT')
    employee=next(x for x in admin.employees() if x['name']=='Mitarbeiter');person=employee['id'];admin_id=admin.session_employee()
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'employee',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(person,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(person,))
    user=mod.LocalStore(os.environ['AURA_LOCAL_DB']);user.open();user.login('employee','Admin Passwort 2026')
    now=int(time.time());admin.book(room['id'],now+600,now+1200,1);user.book(room['id'],now+1800,now+2400,1)
    w=mod.AuraWindow();w.local=user;w.local_mode=True;w.show_all()
    def children(widget):
        result=[widget]
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children():result.extend(children(child))
        return result
    def labels(widget):return [x.get_text() for x in children(widget) if isinstance(x,mod.Gtk.Label)]
    def buttons(widget):return [x.get_label() for x in children(widget) if isinstance(x,mod.Gtk.Button)]
    def wait(predicate):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if predicate():return
            time.sleep(.02)
        raise AssertionError('GTK booking permissions timeout: '+repr(labels(w.content)))
    def card_for(employee_id):
        for child in w.content.get_children():
            if isinstance(child,mod.Gtk.Box) and any('Mitarbeiter '+str(employee_id) in text for text in labels(child)):
                return child
        return None
    w.show_bookings(room);wait(lambda:card_for(person) is not None and card_for(admin_id) is not None)
    assert 'Umbuchen' in buttons(card_for(person)) and 'Stornieren' in buttons(card_for(person))
    assert 'Umbuchen' not in buttons(card_for(admin_id)) and 'Stornieren' not in buttons(card_for(admin_id))
    assert '+ Sperrzeit' not in buttons(w.content) and '+ Buchung' in buttons(w.content)
    w.select(1);wait(lambda:any(x.get_text()=='Konferenz' for x in children(w.content) if isinstance(x,mod.Gtk.Label)))
    assert 'CSV importieren' not in buttons(w.content) and 'CSV exportieren' not in buttons(w.content) and 'Sperren' not in buttons(w.content)
    w.local=admin;w.show_bookings(room);wait(lambda:card_for(person) is not None and 'Umbuchen' in buttons(card_for(person)))
    assert '+ Sperrzeit' in buttons(w.content)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'http://127.0.0.1:{port}'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_JOURNAL':tmp+'/server.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_URL':url,'AURA_SYNC_TOKEN':'manager','AURA_SYNC_IDENTITIES':json.dumps({'manager':{'employeeId':1,'permissions':['rooms.manage']},'worker':{'employeeId':2,'permissions':['rooms.read','rooms.book']},'reader':{'employeeId':3,'permissions':['rooms.read']}})})
    for key in ('AURA_SYNC_REQUIRE_IDENTITIES','AURA_SYNC_REQUIRE_TLS','AURA_SYNC_TLS_CERT','AURA_SYNC_TLS_KEY','AURA_SYNC_CA_CERT'):env.pop(key,None)
    server=subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server failed to start')
        client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
        def cli(token,*args):
            runenv=env.copy();runenv['AURA_SYNC_TOKEN']=token
            return json.loads(subprocess.run([str(client),*args],env=runenv,check=True,capture_output=True,text=True).stdout)
        central=cli('manager','-action','create-room','-name','Zentral','-location','Berlin','-capacity','5')
        for token,start in [('manager',now+600),('worker',now+1800)]:
            cli(token,'-action','central-book','-room-key',central['id'],'-starts-at',str(start),'-ends-at',str(start+600))
        cli('manager','-action','central-book','-room-key',central['id'],'-starts-at',str(now-120),'-ends-at',str(now+300))
        w.local=None;w.local_mode=False;w.url=url;w.token='worker';w.ca='';w.show_bookings(central)
        wait(lambda:card_for(1) is not None and card_for(2) is not None)
        assert 'Umbuchen' not in buttons(card_for(1)) and 'Umbuchen' in buttons(card_for(2))
        assert '+ Sperrzeit' not in buttons(w.content) and '+ Buchung' in buttons(w.content)
        w.select(1);wait(lambda:any(x.get_text()=='Zentral' for x in children(w.content) if isinstance(x,mod.Gtk.Label)))
        assert any(x.endswith(' · Belegt') for x in labels(w.content)) and 'Buchen' in buttons(w.content)
        def book_direct(start,end):
            def fill():
                dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Raum buchen']
                if not dialogs:return True
                entries=[x for x in children(dialogs[-1].get_content_area()) if isinstance(x,mod.Gtk.Entry)]
                for entry,value in zip(entries,[datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M'),datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M'),'1','1','7']):entry.set_text(value)
                dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
            mod.GLib.idle_add(fill);next(x for x in children(w.content) if isinstance(x,mod.Gtk.Button) and x.get_label()=='Buchen').clicked()
        book_direct(now+172800,now+176400);wait(lambda:any(x=='Buchungen · Zentral' for x in labels(w.content)))
        assert any(b['userId']==2 and b['startsAt']>now+170000 for b in cli('worker','-action','central-bookings','-room-key',central['id']))
        w.select(1);wait(lambda:'Buchen' in buttons(w.content));notices=[];w.message=lambda value:notices.append(str(value))
        book_direct(now,now+120);wait(lambda:bool(notices));assert '409' in notices[-1] and 'conflict' in notices[-1].lower(),notices
        assert not any(b['userId']==2 and b['startsAt']<now+300 for b in cli('worker','-action','central-bookings','-room-key',central['id']))
        w.token='reader';w.select(1);wait(lambda:any(x.get_text()=='Zentral' for x in children(w.content) if isinstance(x,mod.Gtk.Label)))
        assert 'Buchen' not in buttons(w.content) and '+ Raum anlegen' not in buttons(w.content)
        w.show_bookings(central);wait(lambda:card_for(1) is not None and card_for(2) is not None)
        assert '+ Buchung' not in buttons(w.content) and 'Umbuchen' not in buttons(w.content)
    finally:
        server.terminate();server.wait(timeout=5);w.disconnect_by_func(mod.Gtk.main_quit);w.destroy();user.close();admin.close()
print('GTK booking controls: ownership, management and read-only permissions in local and central modes: OK')

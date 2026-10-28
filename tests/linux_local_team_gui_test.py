#!/usr/bin/python3
import importlib.util,os,sqlite3,tempfile,time
from datetime import datetime, timedelta
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-team-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Team Passwort 2026')
    w=mod.AuraWindow();w.show_all()
    def children(widget,cls):
        result=[widget] if isinstance(widget,cls) else []
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children():result.extend(children(child,cls))
        return result
    def text(widget):return ' '.join(x.get_text() for x in children(widget,mod.Gtk.Label))
    def wait(test):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if test():return
            time.sleep(.02)
        raise AssertionError('GUI timeout: '+text(w.content))
    def fill(title,values,system_role=None):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
            for e,v in zip(entries,values):e.set_text(v)
            if system_role is not None:
                combo=children(dialogs[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id(system_role)
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def confirm():
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
            if not dialogs:return True
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def click(name):
        found=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(name,len(found));found[0].clicked()
    def card_click(needle,name):
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x) and any(b.get_label()==name for b in children(x,mod.Gtk.Button))];assert len(cards)==1,(needle,len(cards))
        found=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(needle,name,len(found));found[0].clicked()
    fill('Lokale Anmeldung',['admin','Team Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(3);wait(lambda:'Mitarbeiter & Präsenz' in text(w.content) and 'Admin' in text(w.content))
    w.resize(850,550);wait(lambda:w.get_size()[0]<=900 and w.get_size()[1]<=600)
    assert all(any(v.get_label()==name for v in children(w.content,mod.Gtk.Button)) for name in ('+ Mitarbeiter','+ Urlaubsantrag','Urlaubskonflikt prüfen','Lokale Urlaubszuordnung prüfen','Urlaub synchronisieren'))
    fill('Mitarbeiter anlegen',['Sam','Techniker','IT'],'employee');click('+ Mitarbeiter');wait(lambda:'Sam' in text(w.content))
    assert next(x for x in w.local.employees() if x['name']=='Sam')['systemRole']=='employee'
    fill('Mitarbeiter bearbeiten',['Sam Weiß','Ingenieur','IT']);card_click('Sam','Bearbeiten');wait(lambda:'Sam Weiß' in text(w.content))
    assert next(x for x in w.local.employees() if x['name']=='Sam Weiß')['systemRole']=='employee'
    fill('Mitarbeiter bearbeiten',['Sam Weiß','Ingenieur','IT'],'project_manager');card_click('Sam Weiß','Bearbeiten');wait(lambda:'Systemrolle project_manager' in text(w.content))
    sam=next(x for x in w.local.employees() if x['name']=='Sam Weiß')
    try:w.local.save_employee_with_role(sam['id'],'Darf nicht gespeichert werden','Ingenieur','IT','not-a-role');raise AssertionError('Ungültige Rolle angenommen')
    except mod.LocalStoreError:pass
    assert next(x for x in w.local.employees() if x['id']==sam['id'])['name']=='Sam Weiß'
    fill('Mitarbeiter bearbeiten',['Sam Weiß','Ingenieur','IT'],'employee');card_click('Sam Weiß','Bearbeiten');wait(lambda:'Systemrolle employee' in text(w.content))
    try:w.local.save_employee_with_role(0,'Fehlversuch','Technik','IT','not-a-role');raise AssertionError('Ungültige Rolle angenommen')
    except mod.LocalStoreError:pass
    assert not any(x['name']=='Fehlversuch' for x in w.local.employees())
    card_click('Sam Weiß','Präsenz weiter');wait(lambda:'remote' in text(w.content))
    emp=next(x for x in w.local.employees() if x['name']=='Sam Weiß')
    w.local.save_employee(0,'Urlaubsleitung','Teamleitung','IT')
    lead=next(x for x in w.local.employees() if x['name']=='Urlaubsleitung')
    w.local.save_role(0,'leave_manager','Urlaubsfreigabe','Nur Urlaubsentscheidungen','leave.approve')
    w.local.assign_role(lead['id'],'leave_manager')
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'employee',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(emp['id'],))
        db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(emp['id'],))
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'lead',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(lead['id'],))
    fill('Lokale Anmeldung',['employee','Team Passwort 2026']);w.local_login();wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    try:w.local.save_employee_with_role(emp['id'],'Manipuliert','Ingenieur','IT','admin');raise AssertionError('Rollenänderung ohne Berechtigung angenommen')
    except mod.LocalStoreError:pass
    assert next(x for x in w.local.employees() if x['id']==emp['id'])['name']=='Sam Weiß'
    wait(lambda:w.presence_button.get_visible() and w.presence_button.get_sensitive() and w.presence_button.get_label()=='● Home-Office')
    assert w.user_name.get_text()=='Sam Weiß' and w.user_role.get_text()=='Ingenieur'
    w.presence_button.clicked();wait(lambda:w.presence_button.get_sensitive() and w.presence_button.get_label()=='● Im Meeting')
    assert next(x for x in w.local.employees() if x['isSelf'])['presence']=='meeting'
    w.select(0);wait(lambda:'Office Board & Quick Actions' in text(w.content))
    assert 'Audit:' not in text(w.content) and 'Audit wird geladen' not in text(w.content)
    w.select(3);wait(lambda:'Sam Weiß' in text(w.content))
    def fill_leave():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Urlaubsantrag']
        if not dialogs:return True
        entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);day=(datetime.now().date()+timedelta(days=1)).isoformat()
        assert [v.get_text() for v in entries]==[day+' 08:00',day+' 16:00','Erholungsurlaub']
        for entry,value in zip(entries,['2027-06-01 09:00','2027-06-03 17:00','Sommerurlaub']):entry.set_text(value)
        dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(fill_leave);click('+ Urlaubsantrag');wait(lambda:'Sommerurlaub' in text(w.content))
    fill('Urlaubsantrag',['2027-07-01 09:00','2027-07-03 17:00','Konflikturlaub']);click('+ Urlaubsantrag');wait(lambda:'Konflikturlaub' in text(w.content))
    fill('Urlaubsantrag',['2027-08-01 09:00','2027-08-03 17:00','Urlaub ablehnen']);click('+ Urlaubsantrag');wait(lambda:'Urlaub ablehnen' in text(w.content))
    fill('Urlaubsantrag',['2027-09-01 09:00','2027-09-03 17:00','Antrag geändert']);click('+ Urlaubsantrag');wait(lambda:'Antrag geändert' in text(w.content))
    assert 'Urlaubsabgleich nicht eingerichtet' in text(w.content)
    assert not any(b.get_label() in ('Genehmigen','Ablehnen','+ Mitarbeiter','Systemrolle','Archivieren','Bearbeiten') for b in children(w.content,mod.Gtk.Button))
    assert len([b for b in children(w.content,mod.Gtk.Button) if b.get_label()=='Präsenz weiter'])==1
    try:w.local.decide_leave(w.local.leave()[0]['id'],True);raise AssertionError('self approval accepted')
    except mod.LocalStoreError:pass
    fill('Lokale Anmeldung',['lead','Team Passwort 2026']);w.local_login();wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    w.local.create_leave(1900200000,1900300000,'Leitung Urlaub')
    own=next(v for v in w.local.leave() if v['reason']=='Leitung Urlaub')
    try:w.local.decide_leave(own['id'],True);raise AssertionError('lead approved own leave')
    except mod.LocalStoreError:pass
    w.select(3);wait(lambda:'Sommerurlaub' in text(w.content));assert not any(b.get_label()=='+ Mitarbeiter' for b in children(w.content,mod.Gtk.Button))
    try:w.local.save_employee(0,'Nicht erlaubt','Rolle','IT');raise AssertionError('employee management permission bypass')
    except mod.LocalStoreError:pass
    card_click('Sommerurlaub','Genehmigen');wait(lambda:'Entscheidend: Urlaubsleitung' in text(w.content))
    card_click('Urlaub ablehnen','Ablehnen');wait(lambda:next(v for v in w.local.leave() if v['reason']=='Urlaub ablehnen')['status']=='rejected' and 'Abgelehnt' in text(w.content))
    changed=next(v for v in w.local.leave() if v['reason']=='Antrag geändert')
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:db.execute('UPDATE leave_requests SET ends_at=ends_at+3600 WHERE id=?',(changed['id'],))
    notices=[];previous_message=w.message;w.message=lambda value:notices.append(str(value))
    card_click('Antrag geändert','Genehmigen');wait(lambda:bool(notices))
    assert 'aktuelle Stand' in notices[-1]
    wait(lambda:'Antrag geändert' in text(w.content) and 'Daten werden geladen' not in text(w.content))
    updated=next(v for v in w.local.leave() if v['id']==changed['id'])
    assert updated['status']=='pending' and updated['endsAt']==changed['endsAt']+3600
    w.message=previous_message
    conflict=next(v for v in w.local.leave() if v['reason']=='Konflikturlaub')
    other=mod.LocalStore(os.environ['AURA_LOCAL_DB']);other.open();other.login('admin','Team Passwort 2026');other.decide_leave(conflict['id'],True);other.close()
    notices=[];previous_message=w.message;w.message=lambda value:notices.append(str(value))
    card_click('Konflikturlaub','Ablehnen');wait(lambda:bool(notices))
    assert 'aktuelle Stand' in notices[-1]
    wait(lambda:'Konflikturlaub' in text(w.content) and not any(b.get_label()=='Ablehnen' for card in children(w.content,mod.Gtk.Box) if card.get_style_context().has_class('card') and 'Konflikturlaub' in text(card) for b in children(card,mod.Gtk.Button)))
    assert next(v for v in w.local.leave() if v['id']==conflict['id'])['status']=='approved'
    w.message=previous_message

    fill('Lokale Anmeldung',['admin','Team Passwort 2026']);w.local_login();wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    w.select(3);wait(lambda:'Sam Weiß' in text(w.content))
    confirm();card_click('Sam Weiß','Archivieren');wait(lambda:'Sam Weiß' not in text(w.content))
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    setup.open();setup.login('admin','Team Passwort 2026');assert next(v for v in setup.leave() if v['reason']=='Sommerurlaub')['status']=='approved';assert len(setup.employees())==2;setup.close()
print('GTK local team, presence, leave submission, authorization, approval and archival: OK')

#!/usr/bin/python3
import importlib.util,os,tempfile,time,sqlite3
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-accounts-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026')
    with sqlite3.connect(os.environ['AURA_LOCAL_DB']) as db:
        db.execute("CREATE TABLE linux_task_sync_resolutions(id INTEGER PRIMARY KEY,task_id INTEGER,choice TEXT,local_json TEXT,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_task_sync_resolutions VALUES(1,42,'local','lokaler Teststand','Server Teststand','[]','2026-01-01')")
        db.execute("CREATE TABLE linux_dependency_sync_conflicts(id INTEGER PRIMARY KEY,task_id INTEGER,dependency_id INTEGER,choice TEXT,local_present INTEGER,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_dependency_sync_conflicts VALUES(1,42,43,'server',1,'false','[]','2026-01-02')")
        db.execute("CREATE TABLE linux_project_assignment_sync_conflicts(id INTEGER PRIMARY KEY,project_id INTEGER,task_id INTEGER,choice TEXT,local_present INTEGER,remote_present INTEGER,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_project_assignment_sync_conflicts VALUES(1,5,42,'local',1,0,'[]','2026-01-03')")
        db.execute("CREATE TABLE linux_leave_sync_resolutions(id INTEGER PRIMARY KEY,leave_id INTEGER,actor_id INTEGER,local_json TEXT,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_leave_sync_resolutions VALUES(1,7,1,'{}','{}','[]','2026-01-04')")
        for table,column in [('linux_subtask_sync_conflicts','subtask_id'),('linux_ticket_sync_conflicts','ticket_id'),('linux_organization_sync_conflicts','organization_id'),('linux_facility_asset_sync_conflicts','asset_id'),('linux_project_sync_conflicts','project_id'),('linux_maintenance_sync_conflicts','maintenance_id')]:
            db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY,{column} INTEGER,choice TEXT,local_json TEXT,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
            db.execute(f"INSERT INTO {table} VALUES(1,10,'server','{{}}','{{}}','[]','2026-01-05')")
        for table in ('linux_task_attachment_sync_conflicts','linux_ticket_attachment_sync_conflicts'):
            db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY,attachment_id INTEGER,choice TEXT,local_json TEXT,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
            db.execute(f"INSERT INTO {table} VALUES(1,13,'local','{{}}','{{}}','[]','2026-01-05')")
        db.execute("CREATE TABLE linux_maintenance_successor_links(id INTEGER PRIMARY KEY,parent_id INTEGER,local_id INTEGER,old_external_id TEXT,server_json TEXT,local_json TEXT,choice TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_maintenance_successor_links VALUES(1,10,11,'old','{}','{}','server','[]','2026-01-05')")
        db.execute("CREATE TABLE linux_approval_sync_resolutions(id INTEGER PRIMARY KEY,approval_id INTEGER,actor_id INTEGER,local_json TEXT,remote_json TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_approval_sync_resolutions VALUES(1,11,1,'{}','{}','[]','2026-01-05')")
        db.execute("CREATE TABLE linux_booking_sync_resolutions(id INTEGER PRIMARY KEY,booking_id INTEGER,actor_id INTEGER,local_json TEXT,conflicts_json TEXT,previous_operations TEXT,created_at TEXT)")
        db.execute("INSERT INTO linux_booking_sync_resolutions VALUES(1,12,1,'{}','[]','[]','2026-01-05')")
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
    def forms(sequence):
        remaining=list(sequence)
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible()]
            if not dialogs:return True
            d=dialogs[-1];title,values=remaining[0]
            if d.get_title()!=title:return True
            entries=children(d.get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
            for e,v in zip(entries,values):e.set_text(v)
            remaining.pop(0);d.response(mod.Gtk.ResponseType.OK);return bool(remaining)
        mod.GLib.idle_add(submit)
    def choose_employee_role():
        def submit():
            ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Systemrolle zuweisen']
            if not ds:return True
            choices=children(ds[-1].get_content_area(),mod.Gtk.ComboBoxText);assert len(choices)==1 and choices[0].set_active_id('employee')
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def create_account_form(employee_id):
        stage=[0]
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible()]
            if stage[0]==0:
                matches=[d for d in dialogs if d.get_title()=='Konto anlegen']
                if not matches:return True
                stage[0]=1
                next(b for b in children(matches[-1].get_content_area(),mod.Gtk.Button) if b.get_label()=='Person auswählen').clicked()
                return True
            if stage[0]==1:
                matches=[d for d in dialogs if d.get_title()=='Person auswählen']
                if not matches:return True
                combo=children(matches[-1].get_content_area(),mod.Gtk.ComboBoxText)[0]
                assert not combo.set_active_id('1') and combo.set_active_id(str(employee_id))
                stage[0]=2;matches[-1].response(mod.Gtk.ResponseType.OK);return True
            matches=[d for d in dialogs if d.get_title()=='Konto anlegen']
            if not matches:return True
            entries=children(matches[-1].get_content_area(),mod.Gtk.Entry)
            assert entries[0].get_text()==str(employee_id)
            for entry,value in zip(entries[1:],['robin','Temporär Passwort 2026','Temporär Passwort 2026']):entry.set_text(value)
            matches[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def confirm():
        def submit():
            ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
            if not ds:return True
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def click(name):
        found=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(name,len(found));found[0].clicked()
    def card_click(needle,name):
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x)];assert len(cards)==1,(needle,len(cards))
        found=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(needle,name,len(found));found[0].clicked()
    forms([('Lokale Anmeldung',['admin','Admin Passwort 2026'])]);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(5)
    groups={x.get_label():x for x in children(w.content,mod.Gtk.Expander)}
    assert set(groups)=={'Räume','Mitarbeiter & Organisation','Facility & Wartung','Aufgaben & Voraussetzungen','Projekte & Zuordnungen','Tickets'}
    assert not groups['Aufgaben & Voraussetzungen'].get_expanded()
    w.task_auto_notice.set_text('Testhinweis');assert groups['Aufgaben & Voraussetzungen'].get_label().endswith(' · Hinweis')
    w.task_auto_notice.set_text('');assert groups['Aufgaben & Voraussetzungen'].get_label()=='Aufgaben & Voraussetzungen'
    groups['Aufgaben & Voraussetzungen'].set_expanded(True);assert groups['Aufgaben & Voraussetzungen'].get_expanded()
    w.select(5);groups={x.get_label():x for x in children(w.content,mod.Gtk.Expander)};assert groups['Aufgaben & Voraussetzungen'].get_expanded()
    click('Auditprotokoll');wait(lambda:'Weitere Synchronisationsentscheidungen' in text(w.content))
    wait(lambda:len(children(w.content,mod.Gtk.Expander))>=15)
    names=[x.get_label() for x in children(w.content,mod.Gtk.Expander)]
    assert any('Aufgabe 42' in n and 'Lokaler Stand vorgemerkt' in n for n in names)
    assert any('Voraussetzung' in n and 'Serverstand übernommen' in n for n in names)
    assert any('Projektzuordnung' in n and 'Projekt 5' in n for n in names)
    assert any('Urlaub' in n and 'Urlaubsantrag 7' in n for n in names)
    for kind in ('Unteraufgabe','Ticket','Organisation','Anlage','Projekt','Wartung','Wartungsfolge','Freigabe','Buchung','Aufgabenanhang','Ticketanhang'):
        assert any(kind in n for n in names),kind
    w.select(3);wait(lambda:'Mitarbeiter & Präsenz'  in text(w.content) and 'Admin' in text(w.content))
    forms([('Mitarbeiter anlegen',['Robin','Technik','IT'])]);click('+ Mitarbeiter');wait(lambda:'Robin' in text(w.content))
    choose_employee_role();card_click('Robin','Systemrolle');wait(lambda:'Systemrolle employee' in text(w.content))
    user=next(x for x in w.local.employees() if x['name']=='Robin')
    w.select(5);click('Lokale Konten');wait(lambda:'admin' in text(w.content))
    create_account_form(user['id']);click('+ Konto');wait(lambda:'robin' in text(w.content))
    assert 'Passwortwechsel erforderlich' in text(w.content)
    w.local.save_task(0,'Lesbare Aufgabe','IT','Mittel',1)
    w.local.save_ticket(0,'Lesbares Ticket','IT','Mittel',1)
    w.local.save_project(0,'Lesbares Projekt','IT','', 'active',1)
    forms([('Lokale Anmeldung',['robin','Temporär Passwort 2026']),('Passwort ändern',['Eigenes Passwort 2026','Eigenes Passwort 2026'])]);w.local_login()
    wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    w.select(0);wait(lambda:'Office Board & Quick Actions' in text(w.content))
    available=[x.get_label() for x in children(w.content,mod.Gtk.Button)]
    assert '+ Urlaubsantrag einreichen' in available
    assert all(name not in available for name in ('+ Kaffeebohnen-Nachfüllung melden','+ Neues Ticket an IT-Support senden','+ Aufgabe anlegen','+ Raum anlegen'))
    w.select(2);wait(lambda:'Lesbare Aufgabe' in text(w.content) and 'Lesbares Projekt' in text(w.content))
    available=[x.get_label() for x in children(w.content,mod.Gtk.Button)]
    assert 'Details' in available
    assert all(name not in available for name in ('+ Aufgabe anlegen','+ Projekt','Bearbeiten','Archivieren','Projektstatus weiter','Status weiter','Freigaben'))
    click('Details');wait(lambda:'Aufgabe · Lesbare Aufgabe' in text(w.content))
    available=[x.get_label() for x in children(w.content,mod.Gtk.Button)]
    assert all(name not in available for name in ('+ Unteraufgabe','+ Abhängigkeit','Freigabe anfordern','Anhänge','Verlauf'))
    w.select(4);wait(lambda:'Lesbares Ticket' in text(w.content))
    available=[x.get_label() for x in children(w.content,mod.Gtk.Button)]
    assert all(name not in available for name in ('+ Ticket anlegen','Bearbeiten','Archivieren','Status weiter','Verlauf','Anhänge'))
    try:
        w.local.sync_resolution_history()
        raise AssertionError('Mitarbeiterentscheidung ohne Administrationsrecht sichtbar')
    except mod.LocalStoreError:pass
    w.select(5);click('Lokale Konten');wait(lambda:'Berechtigung' in text(w.content))
    forms([('Lokale Anmeldung',['admin','Admin Passwort 2026'])]);w.local_login();wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    w.select(5);click('Lokale Konten');wait(lambda:'robin' in text(w.content));confirm();card_click('robin','Deaktivieren');wait(lambda:'Deaktiviert' in text(w.content))
    confirm();card_click('robin','Aktivieren');wait(lambda:'robin' in text(w.content) and 'Deaktiviert' not in text(w.content))
    forms([('Passwort zurücksetzen',['Neues Temporär 2026','Neues Temporär 2026'])]);card_click('robin','Passwort zurücksetzen');wait(lambda:'Passwortwechsel erforderlich' in text(w.content))
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    setup.open();setup.login('admin','Admin Passwort 2026');assert next(a for a in setup.accounts() if a['username']=='robin')['mustChangePassword'];setup.close()
print('GTK local role assignment, account creation, forced change, access control and reset: OK')

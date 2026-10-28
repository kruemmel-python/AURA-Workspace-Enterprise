#!/usr/bin/python3
"""Exercise real GTK dialogs and local DB without a network connection."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-local-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/local.sqlite'
    os.environ.pop('AURA_SYNC_URL',None); os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py')
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    w=mod.AuraWindow();w.show_all()
    def children(widget,cls):
        result=[widget] if isinstance(widget,cls) else []
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children(): result.extend(children(child,cls))
        return result
    def text(widget): return ' '.join(x.get_text() for x in children(widget,mod.Gtk.Label))
    def pump_until(test):
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
            if not dialogs: return True
            dialog=dialogs[-1]; expected,values=remaining[0]
            if dialog.get_title()!=expected:return True
            entries=children(dialog.get_content_area(),mod.Gtk.Entry)
            assert len(entries)==len(values),(expected,len(entries))
            if expected=='Aufgabe anlegen':assert entries[3].get_text()=='1'
            if expected=='Projekt anlegen':assert entries[2].get_text()=='1'
            for entry,value in zip(entries,values):entry.set_text(value)
            remaining.pop(0);dialog.response(mod.Gtk.ResponseType.OK)
            return bool(remaining)
        mod.GLib.idle_add(submit)
    def click(name):
        found=[b for b in children(w.content,mod.Gtk.Button) if b.get_label()==name]
        assert len(found)==1,(name,len(found));found[0].clicked()
    forms([('Lokales Administratorkonto anlegen',['Test Admin','admin','Test Passwort 2026','Test Passwort 2026']),('Lokale Anmeldung',['admin','Test Passwort 2026'])])
    w.local_login();pump_until(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    assert 'Raumabgleich' in w.state.get_text()
    # The local dashboard exposes the same leave and facility shortcuts as
    # the desktop, and a submitted leave remains in the actual local store.
    w.select(0);pump_until(lambda:any(b.get_label()=='+ Urlaubsantrag einreichen' for b in children(w.content,mod.Gtk.Button)))
    forms([('Urlaubsantrag',['2028-05-08 09:00','2028-05-09 17:00','Dashboard Urlaub'])]);click('+ Urlaubsantrag einreichen')
    pump_until(lambda:any(v['reason']=='Dashboard Urlaub' for v in w.local.leave()))
    def cancel_ticket():
        ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Ticket anlegen']
        if not ds:return True
        ds[-1].response(mod.Gtk.ResponseType.CANCEL);return False
    pump_until(lambda:any(b.get_label()=='+ Kaffeebohnen-Nachfüllung melden' for b in children(w.content,mod.Gtk.Button)))
    mod.GLib.idle_add(cancel_ticket);click('+ Kaffeebohnen-Nachfüllung melden')
    w.select(1);pump_until(lambda:'Noch keine Einträge' in text(w.content))
    def person_form():
        forms_open=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Auswahltest']
        if not forms_open:return True
        parent=forms_open[-1];controls=[x for x in children(parent.get_content_area(),mod.Gtk.Button) if x.get_label()=='Person auswählen'];assert len(controls)==1
        def choose():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Person auswählen']
            if not dialogs:return True
            combo=children(dialogs[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id('1');dialogs[-1].response(mod.Gtk.ResponseType.OK)
            def finish():
                entry=children(parent.get_content_area(),mod.Gtk.Entry)[0];assert entry.get_text()=='1';parent.response(mod.Gtk.ResponseType.OK);return False
            mod.GLib.timeout_add(200,finish);return False
        mod.GLib.idle_add(choose);controls[0].clicked();return False
    mod.GLib.idle_add(person_form);assert w.form('Auswahltest',[('Mitarbeiter-ID','',False)])==['1']
    def cancel_person_form():
        forms_open=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Auswahl abbrechen']
        if not forms_open:return True
        parent=forms_open[-1];control=[x for x in children(parent.get_content_area(),mod.Gtk.Button) if x.get_label()=='Person auswählen'][0]
        def choose():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Person auswählen']
            if not dialogs:return True
            dialogs[-1].response(mod.Gtk.ResponseType.CANCEL)
            mod.GLib.timeout_add(200,lambda:(parent.response(mod.Gtk.ResponseType.OK),False)[1]);return False
        mod.GLib.idle_add(choose);control.clicked();return False
    mod.GLib.idle_add(cancel_person_form);assert w.form('Auswahl abbrechen',[('Mitarbeiter-ID','42',False)])==['42']
    forms([('Raum anlegen',['Raum Lokal','Berlin','6','Display'])]);click('+ Raum anlegen')
    pump_until(lambda:'Raum Lokal' in text(w.content))
    forms([('Raum bearbeiten',['Raum Offline','Hamburg','12','Beamer'])]);click('Bearbeiten')
    pump_until(lambda:'Raum Offline' in text(w.content) and 'Hamburg' in text(w.content))
    click('Buchungen'); pump_until(lambda:'Noch keine Buchungen' in text(w.content))
    forms([('Raum buchen',['2027-03-01 09:00','2027-03-01 10:00','3','1','7'])]);click('+ Buchung')
    pump_until(lambda:'Gebucht' in text(w.content) and '01.03.2027 09:00' in text(w.content))
    errors=[];w.message=lambda msg:errors.append(msg)
    forms([('Raum buchen',['2027-03-01 09:30','2027-03-01 10:30','2','1','7'])]);click('+ Buchung')
    pump_until(lambda:bool(errors)); assert 'bereits gebucht' in errors[-1]
    forms([('Buchung ändern',['2027-03-01 11:00','2027-03-01 12:00','2'])]);click('Umbuchen')
    pump_until(lambda:'01.03.2027 11:00' in text(w.content))
    def confirm_cancel():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
        if not dialogs:return True
        dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(confirm_cancel);click('Stornieren')
    pump_until(lambda:'Storniert' in text(w.content))
    forms([('Raum buchen',['2027-04-01 09:00','2027-04-01 10:00','1','2','7'])]);click('+ Buchung')
    pump_until(lambda:'08.04.2027 09:00' in text(w.content))
    forms([('Sperrzeit anlegen',['2027-05-01 09:00','2027-05-01 11:00','Wartung'])]);click('+ Sperrzeit')
    pump_until(lambda:'Gesperrt' in text(w.content))
    records=w.local.bookings(w.local.rooms()[0]['id'])
    assert len(records)==4 and sum(b['status']=='confirmed' for b in records)==2
    w.select(2); pump_until(lambda:'Meine Vorgänge' in text(w.content) and 'Daten werden geladen' not in text(w.content))
    forms([('Projekt anlegen',['Projekt Lokal','IT','1','Offline'])]);click('+ Projekt')
    pump_until(lambda:'Projekt Lokal' in text(w.content))
    project_id=w.local.projects()[0]['id']
    w.local.save_project(0,'Archivierte Auswahl','IT','','planned',1)
    archived_project=next(p['id'] for p in w.local.projects() if p['name']=='Archivierte Auswahl');w.local.archive_project(archived_project)
    stage=[0]
    def fill_task_with_project_picker():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible()]
        if stage[0]==0:
            parent=next((x for x in dialogs if x.get_title()=='Aufgabe anlegen'),None)
            if not parent:return True
            stage[0]=1;next(b for b in children(parent.get_content_area(),mod.Gtk.Button) if b.get_label()=='Projekt auswählen').clicked();return True
        if stage[0]==1:
            chooser=next((x for x in dialogs if x.get_title()=='Projekt auswählen'),None)
            if not chooser:return True
            combo=children(chooser.get_content_area(),mod.Gtk.ComboBoxText)[0]
            assert not combo.set_active_id(str(archived_project)) and combo.set_active_id(str(project_id))
            stage[0]=2;chooser.response(mod.Gtk.ResponseType.OK);return True
        parent=next((x for x in dialogs if x.get_title()=='Aufgabe anlegen'),None)
        if not parent:return True
        entries=children(parent.get_content_area(),mod.Gtk.Entry);assert entries[3].get_text()=='1' and entries[6].get_text()==str(project_id)
        for entry,value in zip(entries[:6],['Aufgabe Lokal','IT','Hoch','1','2027-06-01 09:00','7']):entry.set_text(value)
        parent.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(fill_task_with_project_picker);click('+ Aufgabe anlegen')
    pump_until(lambda:'Aufgabe Lokal' in text(w.content))
    assert w.local.tasks()[0]['projectIds']==[project_id]
    other=mod.LocalStore(os.environ['AURA_LOCAL_DB']);other.open();other.login('admin','Test Passwort 2026')
    task_id=w.local.tasks()[0]['id'];notices=[];original_message=w.message;w.message=lambda message:notices.append(str(message))
    def concurrent_edit():
        outer=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Aufgabe bearbeiten'),None)
        if not outer:return True
        entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[6].get_text()==str(project_id)
        other.assign_project(task_id,0);entries[0].set_text('Veralteter Titel');entries[6].set_text('0');outer.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(concurrent_edit);w.local_task_form(w.local.tasks()[0]);pump_until(lambda:bool(notices))
    assert 'erneut öffnen' in notices[-1] and w.local.tasks()[0]['title']=='Aufgabe Lokal' and w.local.tasks()[0]['projectIds']==[]
    other.assign_project(task_id,project_id);other.close();w.message=original_message
    pump_until(lambda:any(b.get_label()=='Projektzuordnungen' for b in children(w.content,mod.Gtk.Button)))
    forms([('Aufgabe bearbeiten',['Aufgabe Lokal','IT','Hoch','1','2027-06-01 09:00','7','0'])]);w.local_task_form(w.local.tasks()[0])
    pump_until(lambda:w.local.tasks()[0]['projectIds']==[])
    forms([('Aufgabe bearbeiten',['Aufgabe Lokal','IT','Hoch','1','2027-06-01 09:00','7',str(project_id)])]);w.local_task_form(w.local.tasks()[0])
    pump_until(lambda:w.local.tasks()[0]['projectIds']==[project_id])
    pump_until(lambda:any(b.get_label()=='Projektzuordnungen' for b in children(w.content,mod.Gtk.Button)))
    click('Projektzuordnungen');pump_until(lambda:'Zugeordnet' in text(w.content));click('Entfernen')
    pump_until(lambda:'Nicht zugeordnet' in text(w.content));click('Zuordnen')
    pump_until(lambda:'Nicht zugeordnet' not in text(w.content) and 'Zugeordnet' in text(w.content))
    w.local.save_project(0,'Zweites Projekt','IT','','planned',1);second=next(p['id'] for p in w.local.projects() if p['name']=='Zweites Projekt')
    w.show_local_project_assignments(w.local.tasks()[0]);pump_until(lambda:'Zweites Projekt' in text(w.content));click('Zuordnen')
    pump_until(lambda:len(w.local.tasks()[0]['projectIds'])==2 and len([b for b in children(w.content,mod.Gtk.Button) if b.get_label()=='Entfernen'])==2)
    assert set(w.local.tasks()[0]['projectIds'])=={project_id,second}
    forms([('Aufgabe bearbeiten',['Aufgabe Lokal','IT','Mittel','1','2027-06-01 09:00','7'])]);w.local_task_form(w.local.tasks()[0])
    pump_until(lambda:w.local.tasks()[0]['priority']=='Mittel')
    pump_until(lambda:any(b.get_label()=='Projektzuordnungen' for b in children(w.content,mod.Gtk.Button)))
    assert set(w.local.tasks()[0]['projectIds'])=={project_id,second}
    try:w.local.set_project_edge(w.local.tasks()[0]['id'],second,False,True);raise AssertionError('stale assignment accepted')
    except mod.LocalStoreError:pass
    task_id=w.local.tasks()[0]['id'];w.local.set_project_edge(task_id,second,True,False)
    assert w.local.tasks()[0]['projectIds']==[project_id]
    w.local.set_project_edge(task_id,second,False,True)
    w.local.archive_project(second)
    forms([('Aufgabe bearbeiten',['Aufgabe Lokal','IT','Hoch','1','2027-06-01 09:00','7'])]);w.local_task_form(w.local.tasks()[0])
    pump_until(lambda:w.local.tasks()[0]['priority']=='Hoch' and 'IT · Hoch · open' in text(w.content))
    assert set(w.local.tasks()[0]['projectIds'])=={project_id,second}
    w.show_local_project_assignments(w.local.tasks()[0])
    pump_until(lambda:'Zweites Projekt' in text(w.content) and 'Archiviert' in text(w.content))
    archived=[card for card in children(w.content,mod.Gtk.Box) if card.get_style_context().has_class('card') and 'Zweites Projekt' in text(card)]
    assert len(archived)==1
    [b for b in children(archived[0],mod.Gtk.Button) if b.get_label()=='Entfernen'][0].clicked()
    pump_until(lambda:w.local.tasks()[0]['projectIds']==[project_id] and 'Zweites Projekt' not in text(w.content) and any(b.get_label()=='← Aufgaben' for b in children(w.content,mod.Gtk.Button)))
    click('← Aufgaben')
    pump_until(lambda:'Projekt-IDs ' in text(w.content))
    task=w.local.tasks()[0]; assert task['recurrenceDays']==7 and task['projectIds']==[project_id]
    click('Status weiter');pump_until(lambda:'in_progress' in text(w.content))
    click('Status weiter');pump_until(lambda:len(w.local.tasks())==2)
    pump_until(lambda:text(w.content).count('Aufgabe Lokal')==2)
    tasks=w.local.tasks(); assert {t['status'] for t in tasks}=={'open','done'}
    assert all(t['projectIds']==[project_id] for t in tasks)
    def click_card(needle,name):
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x)]
        assert len(cards)==1,(needle,len(cards))
        matches=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()==name]
        assert len(matches)==1,(needle,name,len(matches));matches[0].clicked()
    forms([('Projekt bearbeiten',['Projekt Aktualisiert','IT','1','Beschreibung neu','active'])]);click_card('Projekt Lokal','Bearbeiten')
    pump_until(lambda:'Projekt Aktualisiert' in text(w.content))
    forms([('Aufgabe bearbeiten',['Aufgabe Erledigt','IT','Mittel','1','2027-06-01 09:00','0',str(project_id)])]);click_card('done','Bearbeiten')
    pump_until(lambda:'Aufgabe Erledigt' in text(w.content))
    def confirm_archive():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
        if not dialogs:return True
        dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(confirm_archive);click_card('Aufgabe Erledigt','Archivieren')
    pump_until(lambda:'Aufgabe Erledigt' not in text(w.content) and 'Aufgabe Lokal' in text(w.content))
    w.select(3); pump_until(lambda:'Mitarbeiter & Präsenz' in text(w.content))
    with w.local_lock:w.local.close();w.local=None
    w.local_mode=False
    forms([('Lokale Anmeldung',['admin','Test Passwort 2026'])]);w.local_login()
    pump_until(lambda:w.local_mode and 'Raum Offline' in text(w.content))
    assert w.local.rooms()[0]['location']=='Hamburg'
    assert any(v['reason']=='Dashboard Urlaub' for v in w.local.leave())
    assert len(w.local.bookings(w.local.rooms()[0]['id']))==4
    assert len(w.local.tasks())==1 and w.local.projects()[0]['id']==project_id and w.local.projects()[0]['name']=='Projekt Aktualisiert'
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
print('GTK local bootstrap, login, room create/edit, booking, conflict, reschedule, cancel, series, block, tasks, project assignment, recurrence and reopen: OK')

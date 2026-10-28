#!/usr/bin/python3
"""Exercise local GTK ticket and multiline comment workflows."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-helpdesk-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db'
    os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Test Passwort 2026');store.open();store.login('admin','Test Passwort 2026')
    store.save_task(0,'Inventur','IT','Hoch',1);store.save_employee(0,'Team','Technik','IT');store.close()
    w=mod.AuraWindow();w.show_all()
    def children(widget,cls):
        result=[widget] if isinstance(widget,cls) else []
        if isinstance(widget,mod.Gtk.Container):
            for c in widget.get_children():result.extend(children(c,cls))
        return result
    def text(widget):return ' '.join(v.get_text() for v in children(widget,mod.Gtk.Label))
    def wait(test):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if test():return
            time.sleep(.02)
        raise AssertionError('GUI timed out: '+text(w.content))
    def fill(title,values,expected_assignee=None):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
            if expected_assignee is not None:assert entries[3].get_text()==expected_assignee
            for entry,value in zip(entries,values):entry.set_text(value)
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def comment(body):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Kommentar hinzufügen']
            if not dialogs:return True
            editors=children(dialogs[-1].get_content_area(),mod.Gtk.TextView);assert len(editors)==1
            editors[0].get_buffer().set_text(body);dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def create_ticket_with_priority_choice():
        stage=[0]
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible()]
            if stage[0]==0:
                outer=next((d for d in dialogs if d.get_title()=='Ticket anlegen'),None)
                if not outer:return True
                stage[0]=1;mod.GLib.idle_add(submit);next(b for b in children(outer.get_content_area(),mod.Gtk.Button) if b.get_label()=='Priorität auswählen').clicked();return False
            if stage[0]==1:
                choice=next((d for d in dialogs if d.get_title()=='Priorität auswählen'),None)
                if not choice:return True
                combo=children(choice.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id('Hoch')
                stage[0]=2;choice.response(mod.Gtk.ResponseType.OK);return True
            outer=next((d for d in dialogs if d.get_title()=='Ticket anlegen'),None)
            if not outer:return True
            entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[2].get_text()=='Hoch' and entries[3].get_text()=='1'
            entries[0].set_text('Monitor kaputt');entries[3].set_text('2');outer.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def confirm():
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
            if not dialogs:return True
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def click(name):
        found=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(name,len(found));found[0].clicked()
    fill('Lokale Anmeldung',['admin','Test Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(4);wait(lambda:'Facility & IT Ticket-Verlauf' in text(w.content) and 'Daten werden geladen' not in text(w.content))
    create_ticket_with_priority_choice();click('+ Ticket anlegen');wait(lambda:'Monitor kaputt' in text(w.content))
    assert w.local.tickets()[0]['assigneeId']==2 and w.local.tickets()[0]['priority']=='Hoch'
    def cancel_priority_and_form():
        outer=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Ticket bearbeiten'),None)
        if not outer:return True
        def cancel_choice():
            dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Priorität auswählen'),None)
            if not dialog:return True
            combo=children(dialog.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.get_active_id()=='Hoch' and combo.set_active_id('Kritisch')
            dialog.response(mod.Gtk.ResponseType.CANCEL);return False
        mod.GLib.idle_add(cancel_choice);next(b for b in children(outer.get_content_area(),mod.Gtk.Button) if b.get_label()=='Priorität auswählen').clicked()
        assert children(outer.get_content_area(),mod.Gtk.Entry)[2].get_text()=='Hoch'
        outer.response(mod.Gtk.ResponseType.CANCEL);return False
    mod.GLib.idle_add(cancel_priority_and_form);click('Bearbeiten')
    assert w.local.tickets()[0]['priority']=='Hoch'
    fill('Ticket bearbeiten',['Monitor reparieren','IT','Mittel','1']);click('Bearbeiten');wait(lambda:'Monitor reparieren' in text(w.content))
    click('Status weiter');wait(lambda:'in_progress' in text(w.content));click('Status weiter');wait(lambda:'resolved' in text(w.content))
    click('Verlauf');wait(lambda:'Noch keine Kommentare' in text(w.content))
    assert 'Serverkommentare werden für diesen Vorgang übernommen' in text(w.content)
    comment('Erste Zeile ä\nZweite Zeile');click('+ Kommentar');wait(lambda:'Erste Zeile ä\nZweite Zeile' in text(w.content))
    ticket=w.local.tickets()[0]; assert w.local.comments('ticket',ticket['id'])[0]['body']=='Erste Zeile ä\nZweite Zeile'
    click('← Zurück');wait(lambda:'Monitor reparieren' in text(w.content))
    confirm();click('Archivieren');wait(lambda:'Noch keine Einträge' in text(w.content))
    w.select(2);wait(lambda:'Inventur' in text(w.content));click('Details');wait(lambda:'Keine Voraussetzungen' in text(w.content))
    click('Verlauf');wait(lambda:'Noch keine Kommentare' in text(w.content))
    assert 'Serverkommentare werden für diesen Vorgang übernommen' in text(w.content)
    comment('Aufgabe geprüft');click('+ Kommentar');wait(lambda:'Aufgabe geprüft' in text(w.content))
    task=w.local.tasks()[0];assert w.local.comments('task',task['id'])[0]['body']=='Aufgabe geprüft'
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    store.open();store.login('admin','Test Passwort 2026')
    assert store.tickets()==[] and store.comments('ticket',ticket['id'])[0]['body']=='Erste Zeile ä\nZweite Zeile'
    assert store.comments('task',task['id'])[0]['body']=='Aufgabe geprüft';store.close()
print('GTK local ticket creation, editing, status, archival and multiline task/ticket comments: OK')

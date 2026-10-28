#!/usr/bin/python3
"""Exercise local task detail dialogs, prerequisites and subtasks through GTK."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-task-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite'
    os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Test Passwort 2026');store.open();store.login('admin','Test Passwort 2026')
    store.save_task(0,'Hauptaufgabe','IT','Hoch',1,1900000000,7)
    store.save_task(0,'Voraussetzung','IT','Mittel',1)
    main=next(t for t in store.tasks() if t['title']=='Hauptaufgabe');store.close()
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
    def fill(title,values):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            dialog=dialogs[-1];entries=children(dialog.get_content_area(),mod.Gtk.Entry)
            assert len(entries)==len(values)
            for entry,value in zip(entries,values):entry.set_text(value)
            dialog.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def accept(title):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def click(name):
        found=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(name,len(found));found[0].clicked()
    def card_click(needle,name):
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x)];assert len(cards)==1,(needle,len(cards))
        matches=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()==name];assert len(matches)==1,(needle,name,len(matches));matches[0].clicked()
    fill('Lokale Anmeldung',['admin','Test Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(2);wait(lambda:'Hauptaufgabe' in text(w.content) and 'Voraussetzung' in text(w.content))
    w.resize(850,550);wait(lambda:w.get_size()[0]<=900 and w.get_size()[1]<=600)
    assert all(any(v.get_label()==name for v in children(w.content,mod.Gtk.Button)) for name in ('Details','Bearbeiten','Projektzuordnungen','Archivieren','Status weiter'))
    card_click('Hauptaufgabe','Details');wait(lambda:'Keine Voraussetzungen' in text(w.content))
    wait(lambda:w.get_size()[0]<=900 and w.get_size()[1]<=600)
    assert 'Lokale Aufgaben synchronisieren' in text(w.content)
    click('Abgleiche öffnen');wait(lambda:'Lokale Aufgaben synchronisieren' in text(w.content))
    w.select(2);wait(lambda:'Hauptaufgabe' in text(w.content));card_click('Hauptaufgabe','Details');wait(lambda:'Keine Voraussetzungen' in text(w.content))
    accept('Voraussetzung auswählen');click('+ Abhängigkeit');wait(lambda:'Voraussetzung' in text(w.content) and 'Keine Voraussetzungen' not in text(w.content))
    fill('Unteraufgabe anlegen',['Prüfung','open','1','2027-06-01 09:00']);click('+ Unteraufgabe');wait(lambda:'Prüfung' in text(w.content))
    other=mod.LocalStore(os.environ['AURA_LOCAL_DB']);other.open();other.login('admin','Test Passwort 2026')
    child=other.subtasks(main['id'])[0];notices=[];original_message=w.message;w.message=lambda value:notices.append(str(value))
    def concurrent_subtask_edit():
        dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabe bearbeiten'),None)
        if not dialog:return True
        other.save_subtask(main['id'],child['id'],'Prüfung','in_progress',1,child['dueAt'])
        entries=children(dialog.get_content_area(),mod.Gtk.Entry);entries[0].set_text('Veralteter Titel');dialog.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(concurrent_subtask_edit);card_click('Prüfung','Bearbeiten');wait(lambda:bool(notices))
    assert 'erneut öffnen' in notices[-1] and other.subtasks(main['id'])[0]['status']=='in_progress' and other.subtasks(main['id'])[0]['title']=='Prüfung'
    other.save_subtask(main['id'],child['id'],'Prüfung','open',1,child['dueAt']);w.show_task_details(main);wait(lambda:'Prüfung' in text(w.content))
    notices.clear()
    def concurrent_subtask_archive():
        dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.MessageDialog) and d.get_visible() and d.get_property('text')=='Unteraufgabe archivieren?'),None)
        if not dialog:return True
        other.save_subtask(main['id'],child['id'],'Prüfung','in_progress',1,child['dueAt'])
        dialog.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(concurrent_subtask_archive);card_click('Prüfung','Archivieren');wait(lambda:bool(notices))
    assert 'erneut öffnen' in notices[-1] and other.subtasks(main['id'])[0]['status']=='in_progress'
    other.save_subtask(main['id'],child['id'],'Prüfung','open',1,child['dueAt']);other.close();w.message=original_message;w.show_task_details(main);wait(lambda:'Prüfung' in text(w.content))
    click('← Aufgaben');wait(lambda:'Hauptaufgabe' in text(w.content) and 'Voraussetzung' in text(w.content))
    card_click('Hauptaufgabe','Status weiter');wait(lambda:'in_progress' in text(w.content))
    messages=[];w.message=lambda value:messages.append(value)
    card_click('Hauptaufgabe','Status weiter');wait(lambda:len(messages)>0)
    assert 'Voraussetzungen' in messages[-1] or 'Unteraufgaben' in messages[-1],messages
    def card_text(needle):
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x)]
        return text(cards[0]) if len(cards)==1 else ''
    card_click('Voraussetzung','Status weiter');wait(lambda:'in_progress' in card_text('Voraussetzung'))
    card_click('Voraussetzung','Status weiter');wait(lambda:'done' in card_text('Voraussetzung'))
    card_click('Hauptaufgabe','Status weiter');wait(lambda:len(messages)>1)
    card_click('Hauptaufgabe','Details');wait(lambda:'Prüfung' in text(w.content))
    def choose_done():
        outer=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabe bearbeiten'),None)
        if not outer:return True
        def choose():
            dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabenstatus auswählen'),None)
            if not dialog:return True
            combo=children(dialog.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.get_active_id()=='open' and combo.set_active_id('done')
            dialog.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(choose);next(b for b in children(outer.get_content_area(),mod.Gtk.Button) if b.get_label()=='Status auswählen').clicked()
        entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[1].get_text()=='done';entries[0].set_text('Prüfung fertig')
        outer.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(choose_done);card_click('Prüfung','Bearbeiten')
    wait(lambda:'Prüfung fertig' in text(w.content) and 'done' in text(w.content))
    stage=[0]
    def clear_assignee():
        dialogs=[d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible()]
        if stage[0]==0:
            outer=next((d for d in dialogs if d.get_title()=='Unteraufgabe bearbeiten'),None)
            if not outer:return True
            stage[0]=1;next(b for b in children(outer.get_content_area(),mod.Gtk.Button) if b.get_label()=='Person auswählen').clicked();return True
        if stage[0]==1:
            dialog=next((d for d in dialogs if d.get_title()=='Person auswählen'),None)
            if not dialog:return True
            combo=children(dialog.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id('0') and combo.get_active_text()=='Nicht zugewiesen'
            stage[0]=2;dialog.response(mod.Gtk.ResponseType.OK);return True
        outer=next((d for d in dialogs if d.get_title()=='Unteraufgabe bearbeiten'),None)
        if not outer:return True
        assert children(outer.get_content_area(),mod.Gtk.Entry)[2].get_text()=='0'
        outer.response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(clear_assignee);card_click('Prüfung fertig','Bearbeiten')
    wait(lambda:'Nicht zugewiesen' in text(w.content));assert w.local.subtasks(main['id'])[0]['assigneeId']==0
    card_click('Voraussetzung','Entfernen');wait(lambda:'Keine Voraussetzungen' in text(w.content))
    click('← Aufgaben');wait(lambda:'Hauptaufgabe' in text(w.content))
    card_click('Hauptaufgabe','Status weiter');wait(lambda:len([t for t in w.local.tasks() if t['title']=='Hauptaufgabe'])==2)
    repeat=next(t for t in w.local.tasks() if t['title']=='Hauptaufgabe' and t['id']!=main['id'])
    assert w.local.subtasks(repeat['id'])[0]['status']=='open'
    wait(lambda:text(w.content).count('Hauptaufgabe')==2)
    card_click('open','Details');wait(lambda:'Prüfung fertig' in text(w.content))
    def confirm_archive():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
        if not dialogs:return True
        dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(confirm_archive);card_click('Prüfung fertig','Archivieren')
    wait(lambda:'Keine Unteraufgaben' in text(w.content))
    assert w.local.subtasks(repeat['id'])==[]
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
print('GTK task dependencies, blocked completion, subtask editing and recurrence: OK')

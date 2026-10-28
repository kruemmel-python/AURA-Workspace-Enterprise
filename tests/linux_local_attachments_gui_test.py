#!/usr/bin/python3
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-attachment-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    source=Path(tmp)/'anhang ä.txt';source.write_text('Daten für AURA',encoding='utf-8')
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_task(0,'Aufgabe','IT','Hoch',1);setup.save_ticket(0,'Ticket','IT','Hoch',1);setup.close()
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
        raise AssertionError('GUI timeout: '+text(w.content))
    def fill(title,values):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
            for e,v in zip(entries,values):e.set_text(v)
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(submit)
    def choose():
        def select():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.FileChooserDialog) and x.get_visible()]
            if not dialogs:return True
            d=dialogs[-1];assert d.set_filename(str(source))
            mod.GLib.timeout_add(300,lambda:(d.response(mod.Gtk.ResponseType.OK),False)[1]);return False
        mod.GLib.idle_add(select)
    def confirm():
        def respond():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
            if not dialogs:return True
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(respond)
    def click(name):
        found=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()==name];assert len(found)==1,(name,len(found));found[0].clicked()
    fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(2);wait(lambda:'Aufgabe' in text(w.content));click('Details');wait(lambda:'Keine Voraussetzungen' in text(w.content));click('Anhänge');wait(lambda:'Keine Anhänge' in text(w.content));w.resize(850,550);wait(lambda:w.get_size()[0]<=900 and w.get_size()[1]<=600)
    assert 'Die Administration richtet den Abgleich ein' in text(w.content) and any(b.get_label()=='Abgleiche öffnen' for b in children(w.content,mod.Gtk.Button))
    choose();click('+ Datei');wait(lambda:'anhang ä.txt' in text(w.content))
    task=w.local.tasks()[0];attachment=w.local.attachments('task',task['id'])[0];saved=Path(w.local.verify_attachment('task',task['id'],attachment['id']))
    errors=[];w.message=lambda message:errors.append(message);saved.write_bytes('verfälscht'.encode('utf-8'));click('Öffnen');wait(lambda:bool(errors));assert 'Größe' in errors[-1] or 'Integritätsprüfung' in errors[-1]
    saved.write_bytes(source.read_bytes());assert w.local.verify_attachment('task',task['id'],attachment['id'])==str(saved)
    confirm();click('Archivieren');wait(lambda:'Archiviert' in text(w.content));assert saved.exists()
    saved.write_bytes(b'kaputt');confirm();click('Wiederherstellen');wait(lambda:len(errors)>1);assert w.local.attachments('task',task['id'])==[]
    saved.write_bytes(source.read_bytes());confirm();click('Wiederherstellen');wait(lambda:len(w.local.attachments('task',task['id']))==1 and any(b.get_label()=='Archivieren' for b in children(w.content,mod.Gtk.Button)))
    confirm();click('Archivieren');wait(lambda:'Archiviert' in text(w.content))
    w.select(4);wait(lambda:'Ticket' in text(w.content));click('Anhänge');wait(lambda:'Keine Anhänge' in text(w.content));choose();click('+ Datei');wait(lambda:'anhang ä.txt' in text(w.content))
    assert 'Die Administration richtet den Abgleich ein' in text(w.content)
    ticket=w.local.tickets()[0];assert len(w.local.attachments('ticket',ticket['id']))==1 and w.local.attachments('task',task['id'])==[]
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    setup.open();setup.login('admin','Admin Passwort 2026');assert len(setup.attachments('ticket',ticket['id']))==1;setup.close()
print('GTK local task/ticket attachment import, integrity failure, archive and persistence: OK')

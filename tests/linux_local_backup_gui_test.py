#!/usr/bin/python3
import importlib.util, os, sqlite3, tempfile, time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-backup-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_room(0,'Test Raum','Berlin',5,'');setup.close()
    w=mod.AuraWindow();w.show_all()
    def children(widget,cls):
        result=[widget] if isinstance(widget,cls) else []
        if isinstance(widget,mod.Gtk.Container):
            for c in widget.get_children():result.extend(children(c,cls))
        return result
    def wait(test):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if test():return
            time.sleep(.02)
        raise AssertionError('GUI timeout')
    def login():
        dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Lokale Anmeldung']
        if not dialogs:return True
        for e,v in zip(children(dialogs[-1].get_content_area(),mod.Gtk.Entry),['admin','Admin Passwort 2026']):e.set_text(v)
        dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(login);w.local_login();wait(lambda:w.local_mode);w.select(5)
    buttons=[x for x in children(w.content,mod.Gtk.Button) if x.get_label()=='Datenbank sichern'];assert len(buttons)==1
    def choose():
        ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.FileChooserDialog) and x.get_visible()]
        if not ds:return True
        d=ds[-1];d.set_current_folder(tmp);d.set_current_name('snapshot.sqlite');mod.GLib.timeout_add(300,lambda:(d.response(mod.Gtk.ResponseType.OK),False)[1]);return False
    def confirm():
        ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible()]
        if not ds:return True
        d=ds[-1];assert 'Anhänge' in d.get_property('secondary-text');d.response(mod.Gtk.ResponseType.OK);return False
    def finish():
        ds=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.MessageDialog) and x.get_visible() and x.get_property('text')=='Datenbank gesichert']
        if not ds:return True
        ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
    mod.GLib.idle_add(choose);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish);buttons[0].clicked()
    backup=Path(tmp)/'snapshot.sqlite';wait(lambda:backup.exists());wait(lambda:not any(isinstance(x,mod.Gtk.MessageDialog) and x.get_visible() for x in mod.Gtk.Window.list_toplevels()))
    with sqlite3.connect(backup) as db:assert db.execute('SELECT name FROM rooms').fetchone()[0]=='Test Raum'
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
print('GTK backup chooser, attachment warning and snapshot: OK')

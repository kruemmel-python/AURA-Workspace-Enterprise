#!/usr/bin/python3
import importlib.util,os,sqlite3,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-restore-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_room(0,'Vorher','Berlin',5,'');backup=Path(tmp)/'backup.sqlite';setup.backup(backup);setup.save_room(0,'Danach','Bonn',4,'');setup.close()
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
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Lokale Anmeldung']
        if not ds:return True
        for e,v in zip(children(ds[-1].get_content_area(),mod.Gtk.Entry),['admin','Admin Passwort 2026']):e.set_text(v)
        ds[-1].response(mod.Gtk.ResponseType.OK);return False
    mod.GLib.idle_add(login);w.local_login();wait(lambda:w.local_mode);w.select(5)
    button=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Datenbank wiederherstellen'][0]
    notices=[];original_message=w.message;w.message=lambda value:notices.append(str(value))
    for name in ('personal_task_comment_sync_running','personal_attachment_sync_running'):
        setattr(w,name,True);button.clicked();assert notices and 'Abgleich' in notices[-1];setattr(w,name,False)
    for kind in ('task','ticket'):
        w.attachment_auto[kind]['running']=True;button.clicked();assert notices and 'Abgleich' in notices[-1];w.attachment_auto[kind]['running']=False
    assert w.local_mode and sorted(v['name'] for v in w.local.rooms())==['Danach','Vorher']
    w.message=original_message
    def choose():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.FileChooserDialog) and v.get_visible()]
        if not ds:return True
        assert ds[-1].set_filename(str(backup));mod.GLib.timeout_add(300,lambda:(ds[-1].response(mod.Gtk.ResponseType.OK),False)[1]);return False
    def confirm():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Datenbank wirklich wiederherstellen?']
        if not ds:return True
        assert 'Anhänge' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.OK);return False
    def finish():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Datenbank wiederhergestellt']
        if not ds:return True
        assert 'Vorherige Datenbank:' in ds[-1].get_property('secondary-text');ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
    def confirm_while_sync_starts():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Datenbank wirklich wiederherstellen?']
        if not ds:return True
        w.personal_attachment_sync_running=True;ds[-1].response(mod.Gtk.ResponseType.OK);return False
    notices.clear();w.message=lambda value:notices.append(str(value))
    mod.GLib.idle_add(choose);mod.GLib.timeout_add(500,confirm_while_sync_starts);button.clicked()
    assert notices and 'Abgleich' in notices[-1] and w.local_mode
    assert sorted(v['name'] for v in w.local.rooms())==['Danach','Vorher']
    w.personal_attachment_sync_running=False;w.message=original_message
    original_run_client=w.run_client
    def run_with_navigation(args,callback):
        if args[args.index('-action')+1]=='restore':
            assert not w.get_sensitive()
            def changed_generation(result,error):
                w.generation+=1
                callback(result,error)
            return original_run_client(args,changed_generation)
        return original_run_client(args,callback)
    w.run_client=run_with_navigation
    mod.GLib.idle_add(choose);mod.GLib.timeout_add(500,confirm);mod.GLib.timeout_add(800,finish);button.clicked();wait(lambda:not w.local_mode and w.local is None);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
    assert w.get_sensitive()
    setup.open();setup.login('admin','Admin Passwort 2026');assert [v['name'] for v in setup.rooms()]==['Vorher'];setup.close()
    previous=list(Path(tmp).glob('workspace.sqlite.before-restore-*'));assert len(previous)==1
    with sqlite3.connect(previous[0]) as db:assert db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]==2
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
print('GTK confirmed database restore, previous snapshot, logout and persisted data: OK')

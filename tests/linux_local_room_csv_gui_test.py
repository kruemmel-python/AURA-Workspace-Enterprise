#!/usr/bin/python3
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-csv-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026');setup.save_room(0,'Süd','Berlin',5,'Display');setup.save_room(0,'Nord','Bonn',6,'Beamer');setup.close()
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
    def login():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Lokale Anmeldung']
        if not ds:return True
        for e,v in zip(children(ds[-1].get_content_area(),mod.Gtk.Entry),['admin','Admin Passwort 2026']):e.set_text(v)
        ds[-1].response(mod.Gtk.ResponseType.OK);return False
    def click(name):
        matches=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()==name];assert len(matches)==1,(name,len(matches));matches[0].clicked()
    mod.GLib.idle_add(login);w.local_login();wait(lambda:w.local_mode);w.select(1);wait(lambda:'Berlin' in text(w.content) and 'Bonn' in text(w.content))
    search=children(w.content,mod.Gtk.SearchEntry)[0];search.set_text('BEAMER')
    cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card')]
    wait(lambda:any('Nord' in text(c) and c.get_visible() for c in cards) and any('Süd' in text(c) and not c.get_visible() for c in cards))
    w.set_focus(search);wait(lambda:search.has_focus())
    generation=w.generation;assert w.dashboard_tick() and w.generation==generation
    w.set_focus(None);assert w.dashboard_tick()
    wait(lambda:w.room_search_widget is not None and w.room_search_widget is not search)
    search=w.room_search_widget;cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card')]
    assert search.get_text()=='BEAMER'
    assert any('Nord' in text(c) and c.get_visible() for c in cards) and any('Süd' in text(c) and not c.get_visible() for c in cards)
    search.set_text('');wait(lambda:all(c.get_visible() for c in cards))
    def choose():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.FileChooserDialog) and v.get_visible()]
        if not ds:return True
        ds[-1].set_current_folder(tmp);ds[-1].set_current_name('raum-export.csv');mod.GLib.timeout_add(300,lambda:(ds[-1].response(mod.Gtk.ResponseType.OK),False)[1]);return False
    def dismiss():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible()]
        if not ds:return True
        assert ds[-1].get_property('text')=='Räume exportiert';ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
    mod.GLib.idle_add(choose);mod.GLib.timeout_add(800,dismiss);click('CSV exportieren');wait(lambda:(Path(tmp)/'raum-export.csv').exists());wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
    w.local.save_room(w.local.rooms()[0]['id'],w.local.rooms()[0]['name'],'Verändert',5,'Display')
    wait(lambda:any(v.get_label()=='CSV importieren' for v in children(w.content,mod.Gtk.Button)))
    def select_import():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.FileChooserDialog) and v.get_visible()]
        if not ds:return True
        assert ds[-1].set_filename(str(Path(tmp)/'raum-export.csv'));mod.GLib.timeout_add(300,lambda:(ds[-1].response(mod.Gtk.ResponseType.OK),False)[1]);return False
    def confirm_import():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible()]
        if not ds:return True
        assert ds[-1].get_property('text')=='Räume importieren?';ds[-1].response(mod.Gtk.ResponseType.OK);return False
    def close_import():
        ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() and v.get_property('text')=='Räume importiert']
        if not ds:return True
        ds[-1].response(mod.Gtk.ResponseType.CLOSE);return False
    mod.GLib.idle_add(select_import);mod.GLib.timeout_add(500,confirm_import);mod.GLib.timeout_add(800,close_import);click('CSV importieren');wait(lambda:'Verändert' not in [v['location'] for v in w.local.rooms()]);wait(lambda:not any(isinstance(v,mod.Gtk.MessageDialog) and v.get_visible() for v in mod.Gtk.Window.list_toplevels()))
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
print('GTK room search, CSV export and confirmed import: OK')

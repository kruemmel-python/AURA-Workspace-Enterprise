#!/usr/bin/python3
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-facility-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Facility Passwort 2026')
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
    def fill(title,values):
        def submit():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()==title]
            if not dialogs:return True
            entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==len(values)
            for entry,value in zip(entries,values):entry.set_text(value)
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
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and needle in text(x)];assert len(cards)==1,(needle,len(cards))
        found=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()==name];assert len(found)==1;found[0].clicked()
    fill('Lokale Anmeldung',['admin','Facility Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(4);wait(lambda:'Facility & IT Ticket-Verlauf' in text(w.content) and 'Daten werden geladen' not in text(w.content))
    click('Facility-Assets');wait(lambda:'Keine Assets' in text(w.content))
    assert 'Facility-Anlagen synchronisieren' in text(w.content) and any(b.get_label()=='Abgleiche öffnen' for b in children(w.content,mod.Gtk.Button))
    fill('Asset anlegen',['Beamer','Technik','Berlin','operational']);click('+ Asset');wait(lambda:'Beamer' in text(w.content))
    fill('Asset bearbeiten',['Beamer Nord','Technik','Bonn','maintenance']);click('Bearbeiten');wait(lambda:'Beamer Nord' in text(w.content) and 'Bonn' in text(w.content))
    asset_id=w.local.assets()[0]['id'];click('Wartung');wait(lambda:'Keine Wartungsaufträge' in text(w.content))
    assert 'Wartungsaufträge synchronisieren' in text(w.content)
    fill('Wartungsauftrag anlegen',['Filter prüfen','2027-06-01 09:00','30']);click('+ Wartungsauftrag');wait(lambda:'Filter prüfen' in text(w.content))
    fill('Wartungsauftrag bearbeiten',['Filter wechseln','2027-06-02 09:00','30']);click('Bearbeiten');wait(lambda:'Filter wechseln' in text(w.content))
    click('Status weiter');wait(lambda:'in_progress' in text(w.content));click('Status weiter');wait(lambda:len(w.local.maintenance(asset_id))==2 and 'completed' in text(w.content))
    orders=w.local.maintenance(asset_id);assert {x['status'] for x in orders}=={'open','completed'}
    confirm();card_click('completed','Archivieren');wait(lambda:len(w.local.maintenance(asset_id))==1)
    click('← Assets');wait(lambda:'Beamer Nord' in text(w.content));confirm();click('Archivieren');wait(lambda:'Keine Assets' in text(w.content))
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    setup.open();setup.login('admin','Facility Passwort 2026');assert setup.assets()==[] and len(setup.maintenance(asset_id))==1;setup.close()
print('GTK local asset and maintenance create, edit, recurrence, archive and reopen: OK')

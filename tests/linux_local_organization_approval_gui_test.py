#!/usr/bin/python3
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-org-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026');setup.open();setup.login('admin','Admin Passwort 2026')
    setup.save_employee(0,'Robin','Projektleitung','IT');employee=next(e for e in setup.employees() if e['name']=='Robin')
    setup.assign_role(employee['id'],'project_manager');setup.create_account(employee['id'],'robin','Temporär Passwort 2026')
    setup.save_task(0,'Projektstart','IT','Hoch',1);setup.close()
    robin=mod.LocalStore(os.environ['AURA_LOCAL_DB']);robin.open();assert robin.login('robin','Temporär Passwort 2026');robin.change_password('Temporär Passwort 2026','Eigenes Passwort 2026')
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
    fill('Lokale Anmeldung',['admin','Admin Passwort 2026']);w.local_login();wait(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
    w.select(3);wait(lambda:'Mitarbeiter & Präsenz' in text(w.content));click('Organisation');wait(lambda:'Keine Organisationseinheiten' in text(w.content))
    fill('Einheit anlegen',['Entwicklung','DEV',str(employee['id'])]);click('+ Einheit');wait(lambda:'Entwicklung' in text(w.content))
    fill('Einheit bearbeiten',['Software','SW','1']);click('Bearbeiten');wait(lambda:'Software' in text(w.content))
    confirm();click('Archivieren');wait(lambda:'Keine Organisationseinheiten' in text(w.content))
    w.select(2);wait(lambda:'Projektstart' in text(w.content));click('Details');wait(lambda:'Keine Voraussetzungen' in text(w.content))
    errors=[];w.message=lambda msg:errors.append(msg)
    fill('Freigabe anfordern',['1']);click('Freigabe anfordern');wait(lambda:len(errors)>0);assert 'unterschiedlich' in errors[-1]
    fill('Freigabe anfordern',[str(employee['id'])]);click('Freigabe anfordern')
    def requested():
        return bool(robin.approvals())
    wait(requested);robin.close()
    fill('Lokale Anmeldung',['robin','Eigenes Passwort 2026']);w.local_login();wait(lambda:w.local_mode and w.tab==1 and 'Daten werden geladen' not in text(w.content))
    w.select(2);wait(lambda:'Projektstart' in text(w.content));click('Freigaben');wait(lambda:'Projektstart' in text(w.content) and 'Offen' in text(w.content))
    click('Genehmigen');wait(lambda:'Genehmigt' in text(w.content));assert w.local.approvals()[0]['status']=='approved'
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    robin.open();robin.login('robin','Eigenes Passwort 2026');assert robin.approvals()[0]['status']=='approved';robin.close()
print('GTK local organization and two-user approval workflow: OK')

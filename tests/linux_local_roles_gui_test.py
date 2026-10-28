#!/usr/bin/python3
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-roles-gui-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.db';os.environ.pop('AURA_SYNC_URL',None);os.environ.pop('AURA_SYNC_TOKEN',None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    setup=mod.LocalStore(os.environ['AURA_LOCAL_DB']);setup.create('Admin','admin','Admin Passwort 2026')
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
    w.select(5);click('Systemrollen');wait(lambda:'Systemadministration' in text(w.content))
    forms([('Rolle anlegen',['test_rolle','Test Rolle','Beschreibung','rooms.manage, tasks.manage'])]);click('+ Rolle')
    wait(lambda:'Test Rolle' in text(w.content) and 'tasks.manage' in text(w.content))
    forms([('Rolle bearbeiten',['test_rolle','Test Rolle 2','Neu','tickets.manage'])]);card_click('Test Rolle','Bearbeiten')
    wait(lambda:'Test Rolle 2' in text(w.content) and 'tickets.manage' in text(w.content))
    confirm();card_click('Test Rolle 2','Archivieren');wait(lambda:'Test Rolle 2' not in text(w.content) and 'Systemadministration' in text(w.content))
    click('← Administration');click('Auditprotokoll');wait(lambda:'role.archived' in text(w.content))
    assert 'role.created' in text(w.content) and 'role.updated' in text(w.content)
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    setup.open();setup.login('admin','Admin Passwort 2026');assert not any(r['code']=='test_rolle' for r in setup.roles());assert any('role.archived' in entry for entry in setup.audit());setup.close()
print('GTK local role create, edit, archive and audit: OK')

#!/usr/bin/python3
"""Read-only local users can inspect active comments and verified attachments."""
import importlib.util,os,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-read-only-') as tmp:
    os.environ['AURA_LOCAL_DB']=str(Path(tmp)/'workspace.db')
    for key in ('AURA_SYNC_URL','AURA_SYNC_TOKEN','AURA_SYNC_CA_CERT'):os.environ.pop(key,None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    admin=mod.LocalStore(os.environ['AURA_LOCAL_DB']);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    admin.save_employee(0,'Leserin','Team','IT');user=next(v for v in admin.employees() if v['name']=='Leserin')
    admin.save_role(0,'reader','Lesen','','tasks.read,tickets.read');admin.assign_role(user['id'],'reader');admin.create_account(user['id'],'leserin','Temporär Passwort 2026')
    admin.save_task(0,'Leseaufgabe','IT','Mittel',1);task=next(v for v in admin.tasks() if v['title']=='Leseaufgabe')
    admin.save_ticket(0,'Leseticket','IT','Mittel',1);ticket=next(v for v in admin.tickets() if v['title']=='Leseticket')
    file=Path(tmp)/'hinweis.txt';file.write_text('Verifizierter Inhalt')
    for kind,item in (('task',task),('ticket',ticket)):
        admin.add_comment(kind,item['id'],'Lesbarer Kommentar '+kind)
        admin.import_attachment(kind,item['id'],file)
        admin.import_attachment(kind,item['id'],file)
        attachments=admin.attachments(kind,item['id']);admin.archive_attachment(kind,item['id'],attachments[-1]['id'])
    reader=mod.LocalStore(os.environ['AURA_LOCAL_DB']);reader.open();assert reader.login('leserin','Temporär Passwort 2026');reader.change_password('Temporär Passwort 2026','Eigenes Passwort 2026')
    def reject(fn):
        try:fn();raise AssertionError('unerlaubter Zugriff')
        except mod.LocalStoreError:pass
    for kind,item in (('task',task),('ticket',ticket)):
        assert len(reader.comments(kind,item['id']))==1
        attachments=reader.attachments(kind,item['id']);assert len(attachments)==1
        assert Path(reader.verify_attachment(kind,item['id'],attachments[0]['id'])).read_text()=='Verifizierter Inhalt'
        reject(lambda:reader.attachments(kind,item['id'],True))
        reject(lambda:reader.add_comment(kind,item['id'],'Nicht erlaubt'))
        reject(lambda:reader.archive_attachment(kind,item['id'],attachments[0]['id']))
    w=mod.AuraWindow();w.local=reader;w.local_mode=True;w.show_all()
    def children(widget,cls):
        result=[widget] if isinstance(widget,cls) else []
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children():result.extend(children(child,cls))
        return result
    def text():return ' '.join(x.get_text() for x in children(w.content,mod.Gtk.Label))
    def buttons():return [x.get_label() for x in children(w.content,mod.Gtk.Button)]
    def wait(fn):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if fn():return
            time.sleep(.02)
        raise AssertionError('GUI timeout: '+text())
    def click(name):
        matches=[b for b in children(w.content,mod.Gtk.Button) if b.get_label()==name];assert len(matches)==1,(name,buttons());matches[0].clicked()
    w.select(2);wait(lambda:'Leseaufgabe' in text());click('Details');wait(lambda:'Aufgabe · Leseaufgabe' in text())
    assert 'Verlauf' in buttons() and 'Anhänge' in buttons() and '+ Unteraufgabe' not in buttons()
    click('Verlauf');wait(lambda:'Lesbarer Kommentar task' in text());assert '+ Kommentar' not in buttons() and 'Kommentare persönlich abgleichen' in buttons()
    click('← Zurück');wait(lambda:'Aufgabe · Leseaufgabe' in text());click('Anhänge');wait(lambda:'hinweis.txt' in text())
    assert 'Öffnen' in buttons() and 'Anhänge persönlich abgleichen' in buttons() and '+ Datei' not in buttons() and 'Archivieren' not in buttons()
    w.select(4);wait(lambda:'Leseticket' in text());assert 'Verlauf' in buttons() and 'Anhänge' in buttons()
    click('Verlauf');wait(lambda:'Lesbarer Kommentar ticket' in text());assert '+ Kommentar' not in buttons() and 'Kommentare persönlich abgleichen' in buttons()
    w.select(4);wait(lambda:'Leseticket' in text());click('Anhänge');wait(lambda:'hinweis.txt' in text());assert 'Öffnen' in buttons() and 'Anhänge persönlich abgleichen' in buttons() and '+ Datei' not in buttons()
    # A collaborator with read access can contribute, but cannot restore
    # archived files or mutate task/ticket metadata.
    admin.save_role(0,'collaborator','Zusammenarbeit','','tasks.read,tickets.read,collaboration.comment')
    admin.assign_role(user['id'],'collaborator')
    for kind,item in (('task',task),('ticket',ticket)):
        reader.add_comment(kind,item['id'],'Eigener Beitrag '+kind)
        reader.import_attachment(kind,item['id'],file)
        uploaded=reader.attachments(kind,item['id'])[-1]
        reader.archive_attachment(kind,item['id'],uploaded['id'])
        reject(lambda:reader.restore_attachment(kind,item['id'],uploaded['id']))
    reject(lambda:reader.save_task(task['id'],'Nicht erlaubt','IT','Mittel',1))
    w.select(2);wait(lambda:'Leseaufgabe' in text());click('Details');wait(lambda:'Aufgabe · Leseaufgabe' in text())
    click('Verlauf');wait(lambda:'Eigener Beitrag task' in text());assert '+ Kommentar' in buttons()
    click('← Zurück');wait(lambda:'Aufgabe · Leseaufgabe' in text());click('Anhänge');wait(lambda:'hinweis.txt' in text())
    assert '+ Datei' in buttons() and 'Archivieren' in buttons() and 'Wiederherstellen' not in buttons()
    w.select(4);wait(lambda:'Leseticket' in text());click('Verlauf');wait(lambda:'Eigener Beitrag ticket' in text());assert '+ Kommentar' in buttons()
    w.select(4);wait(lambda:'Leseticket' in text());click('Anhänge');wait(lambda:'hinweis.txt' in text());assert '+ Datei' in buttons() and 'Archivieren' in buttons()
    admin.save_role(0,'collab_only','Ohne Lesen','','collaboration.comment');admin.assign_role(user['id'],'collab_only')
    for kind,item in (('task',task),('ticket',ticket)):
        reject(lambda:reader.add_comment(kind,item['id'],'Ohne Leserecht'))
        reject(lambda:reader.import_attachment(kind,item['id'],file))
    w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
    with w.local_lock:w.local.close()
    admin.close()
print('GTK local read-only and collaborator task/ticket comments and attachments: OK')

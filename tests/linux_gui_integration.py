#!/usr/bin/python3
"""Run GTK edits against an isolated, real TLS server (requires a display)."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'KR_BUILD/linux-x64/aura-sync-server'
CLIENT = ROOT / 'KR_BUILD/linux-x64/aura-workspace-enterprise'
assert SERVER.is_file() and CLIENT.is_file(), 'Build the Linux server and client first'

with tempfile.TemporaryDirectory(prefix='aura-gui-integration-') as tmp:
    cert, key = Path(tmp)/'cert.pem', Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
    manager='integration-manager'; reader='integration-reader'
    env=os.environ.copy()
    env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}', 'AURA_SYNC_URL':f'https://127.0.0.1:{port}', 'AURA_SYNC_JOURNAL':tmp+'/events.ndjson', 'AURA_CENTRAL_DB':tmp+'/central.sqlite', 'AURA_SYNC_TOKEN':manager, 'AURA_SYNC_IDENTITIES':json.dumps({manager:{'employeeId':9,'permissions':['rooms.manage','projects.manage','tasks.manage','tickets.manage','leave.approve','facility.manage','employees.manage']},reader:{'employeeId':7,'permissions':['rooms.read','projects.read','tasks.read','tickets.read','leave.request','leave.approve','facility.read','employees.read']},'commenter':{'employeeId':7,'permissions':['tasks.read','collaboration.comment']}}), 'AURA_SYNC_REQUIRE_IDENTITIES':'1', 'AURA_SYNC_REQUIRE_TLS':'1', 'AURA_SYNC_TLS_CERT':str(cert), 'AURA_SYNC_TLS_KEY':str(key), 'AURA_SYNC_CA_CERT':str(cert)})
    server=subprocess.Popen([str(SERVER),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        ctx=ssl.create_default_context(cafile=str(cert))
        for attempt in range(100):
            if server.poll() is not None:
                raise RuntimeError(server.stderr.read().decode())
            try:
                urllib.request.urlopen(env['AURA_SYNC_URL']+'/health',context=ctx,timeout=1).close()
                break
            except OSError: time.sleep(.05)
        else: raise RuntimeError('server did not start')
        os.environ.update(env)
        spec=importlib.util.spec_from_file_location('aura_gui',ROOT/'clients/linux/aura_gui.py')
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        w=mod.AuraWindow(); w.show_all()
        def pump_until(test):
            end=time.monotonic()+15
            while time.monotonic()<end:
                while mod.Gtk.events_pending(): mod.Gtk.main_iteration()
                if test(): return
                time.sleep(.02)
            raise AssertionError('GUI timed out: '+text(w.content))
        def children(widget,cls):
            result=[widget] if isinstance(widget,cls) else []
            if isinstance(widget,mod.Gtk.Container):
                for child in widget.get_children(): result.extend(children(child,cls))
            return result
        def text(widget): return ' '.join(v.get_text() for v in children(widget,mod.Gtk.Label))
        def click(name):
            match=[v for v in children(w.content,mod.Gtk.Button) if v.get_label()==name]
            assert len(match)==1,(name,len(match),text(w.content))
            match[0].clicked()
        def fill_and_submit(values,expected_assignee=None):
            def fill():
                dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible()]
                if not dialogs: return True
                entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry)
                assert len(entries)==len(values),(len(entries),values)
                if expected_assignee is not None:assert entries[3].get_text()==expected_assignee
                for entry,value in zip(entries,values): entry.set_text(value)
                dialogs[-1].response(mod.Gtk.ResponseType.OK)
                return False
            mod.GLib.idle_add(fill)
        def confirm(response=mod.Gtk.ResponseType.OK):
            def respond():
                dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.MessageDialog) and v.get_visible()]
                if not dialogs: return True
                dialogs[-1].response(response); return False
            mod.GLib.idle_add(respond)
        def load(tab,needle):
            w.select(tab); pump_until(lambda:'Daten werden geladen' not in text(w.content) and needle in text(w.content))
        pump_until(lambda:w.state.get_text()=='Verbindung aktiv' and 'Daten werden geladen' not in text(w.content))
        # A linked non-manager can update only their own server presence.
        employee=json.loads(subprocess.run([str(CLIENT),'-action','central-employee-create','-name','Präsenz Test','-role','Team','-department','IT','-presence','office'],env=env,text=True,capture_output=True,check=True).stdout)
        subprocess.run([str(CLIENT),'-action','employee-account-link','-id',employee['id'],'-account-id','7'],env=env,text=True,capture_output=True,check=True)
        w.token=reader;w.refresh()
        pump_until(lambda:w.presence_button.get_visible() and w.presence_button.get_sensitive() and w.presence_button.get_label()=='● Im Büro')
        assert w.user_name.get_text()=='Präsenz Test' and w.user_role.get_text()=='Team'
        w.presence_button.clicked();pump_until(lambda:w.presence_button.get_sensitive() and w.presence_button.get_label()=='● Home-Office')
        reader_env=env.copy();reader_env['AURA_SYNC_TOKEN']=reader
        assert json.loads(subprocess.run([str(CLIENT),'-action','my-presence'],env=reader_env,text=True,capture_output=True,check=True).stdout)['presence']=='remote'
        subprocess.run([str(CLIENT),'-action','central-employee-archive','-id',employee['id']],env=env,text=True,capture_output=True,check=True)
        w.token=manager;w.refresh();pump_until(lambda:w.state.get_text()=='Verbindung aktiv' and 'Daten werden geladen' not in text(w.content))
        load(1,'Aktive Räume')
        fill_and_submit(['Raum Original','Berlin','8','Monitor']); click('+ Raum anlegen')
        pump_until(lambda:'Raum Original' in text(w.content))
        fill_and_submit(['Raum Geändert','Bonn','12','Beamer']); click('Bearbeiten')
        pump_until(lambda:'Raum Geändert' in text(w.content) and 'Bonn' in text(w.content))
        room_values=json.loads(subprocess.run([str(CLIENT),'-action','rooms'],env=env,text=True,capture_output=True,check=True).stdout);room_id=room_values[0]['id']
        click('Buchungen');pump_until(lambda:'Noch keine Buchungen vorhanden' in text(w.content))
        fill_and_submit(['2027-05-01 09:00','2027-05-01 10:00','3','2','7']);click('+ Buchung');pump_until(lambda:len([v for v in children(w.content,mod.Gtk.Button) if v.get_label()=='Stornieren'])==2)
        errors=[];w.message=lambda message:errors.append(message)
        fill_and_submit(['2027-05-01 09:30','2027-05-01 10:30','2','1','7']);click('+ Buchung');pump_until(lambda:bool(errors));assert '409' in errors[-1],errors
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and '01.05.2027' in text(x)];assert len(cards)==1
        buttons=[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()=='Umbuchen'];fill_and_submit(['2027-05-02 09:00','2027-05-02 10:00','3']);buttons[0].clicked();pump_until(lambda:'02.05.2027' in text(w.content))
        cards=[x for x in children(w.content,mod.Gtk.Box) if x.get_style_context().has_class('card') and '02.05.2027' in text(x)];confirm();[x for x in children(cards[0],mod.Gtk.Button) if x.get_label()=='Stornieren'][0].clicked();pump_until(lambda:'Storniert' in text(w.content))
        fill_and_submit(['2027-05-03 09:00','2027-05-03 10:00','Wartung']);click('+ Sperrzeit');pump_until(lambda:'Wartung' in text(w.content))
        click('← Räume');pump_until(lambda:'Raum Geändert' in text(w.content))
        confirm(mod.Gtk.ResponseType.CANCEL); click('Archivieren')
        assert 'Raum Geändert' in text(w.content)
        confirm(); click('Archivieren'); pump_until(lambda:'Raum Geändert' not in text(w.content) and 'Noch keine Einträge' in text(w.content))
        load(2,'Meine Vorgänge')
        fill_and_submit(['Projekt Original','IT','9','Beschreibung']); click('+ Projekt')
        pump_until(lambda:'Projekt Original' in text(w.content))
        fill_and_submit(['Projekt Geändert','IT','9','Neu','active']); click('Bearbeiten')
        pump_until(lambda:'Projekt Geändert' in text(w.content) and 'active' in text(w.content))
        confirm(); click('Archivieren'); pump_until(lambda:'Projekt Geändert' not in text(w.content) and 'Daten werden geladen' not in text(w.content))
        load(4,'Facility & IT'); click('Facility-Assets'); pump_until(lambda:'Keine Assets vorhanden' in text(w.content))
        fill_and_submit(['Drucker','Druck','Berlin','operational']); click('+ Asset'); pump_until(lambda:'Drucker' in text(w.content))
        fill_and_submit(['Drucker Neu','Druck','Bonn','maintenance']); click('Bearbeiten'); pump_until(lambda:'Drucker Neu' in text(w.content) and 'Bonn' in text(w.content))
        w.token=reader
        messages=[]; w.message=lambda message:messages.append(message)
        confirm(); click('Archivieren'); pump_until(lambda:bool(messages)); assert '403' in messages[-1],messages
        w.token=manager; confirm(); click('Archivieren'); pump_until(lambda:'Keine Assets vorhanden' in text(w.content))
        fill_and_submit(['Heizung','Technik','Berlin','operational']);click('+ Asset');pump_until(lambda:'Heizung' in text(w.content))
        assets=json.loads(subprocess.run([str(CLIENT),'-action','facility-assets'],env=env,text=True,capture_output=True,check=True).stdout);asset_id=next(v['id'] for v in assets if v['name']=='Heizung')
        click('Wartung');pump_until(lambda:'Keine Wartungsaufträge vorhanden' in text(w.content))
        fill_and_submit(['Filter prüfen','2027-06-01 09:00','30']);click('+ Wartungsauftrag');pump_until(lambda:'Filter prüfen' in text(w.content))
        fill_and_submit(['Filter wechseln','2027-06-02 09:00','30']);click('Bearbeiten');pump_until(lambda:'Filter wechseln' in text(w.content))
        click('Status weiter');pump_until(lambda:'in_progress' in text(w.content));click('Status weiter');pump_until(lambda:'completed' in text(w.content) and 'open' in text(w.content))
        w.token=reader;messages=[];w.message=lambda message:messages.append(message)
        orders=json.loads(subprocess.run([str(CLIENT),'-action','central-maintenance','-asset-id',asset_id],env=env,text=True,capture_output=True,check=True).stdout)
        denied_env=env.copy();denied_env['AURA_SYNC_TOKEN']=reader
        denied=subprocess.run([str(CLIENT),'-action','central-maintenance-archive','-asset-id',asset_id,'-id',orders[0]['id']],env=denied_env,text=True,capture_output=True);assert denied.returncode and '403' in denied.stderr
        w.token=manager
        load(3,'Zentrales Mitarbeiterverzeichnis')
        fill_and_submit(['Mira Müller','Technikerin','IT']);click('+ Mitarbeiter');pump_until(lambda:'Mira Müller' in text(w.content))
        click('Präsenz weiter');pump_until(lambda:'remote' in text(w.content))
        fill_and_submit(['Mira Neumann','Leitung','Support']);click('Bearbeiten');pump_until(lambda:'Mira Neumann' in text(w.content) and 'Support' in text(w.content))
        w.token=reader;messages=[];w.message=lambda message:messages.append(message);confirm();click('Archivieren');pump_until(lambda:bool(messages));assert '403' in messages[-1],messages
        w.token=manager;confirm();click('Archivieren');pump_until(lambda:'Keine Mitarbeiter vorhanden' in text(w.content))
        fill_and_submit(['Team Dauerhaft','Entwicklung','IT']);click('+ Mitarbeiter');pump_until(lambda:'Team Dauerhaft' in text(w.content))
        def choose_account():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Serverkonto zuordnen']
            if not ds:return True
            combo=children(ds[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id('7');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(choose_account);confirm();click('Serverkonto');pump_until(lambda:'Serverkonto 7' in text(w.content))
        click('Organisation');pump_until(lambda:'Keine Organisationseinheiten vorhanden' in text(w.content))
        fill_and_submit(['Entwicklung','DEV','9']);click('+ Einheit');pump_until(lambda:'Entwicklung' in text(w.content) and 'DEV' in text(w.content))
        fill_and_submit(['Entwicklung Nord','DEV-N','9']);click('Bearbeiten');pump_until(lambda:'Entwicklung Nord' in text(w.content))
        w.token=reader;messages=[];w.message=lambda message:messages.append(message);confirm();click('Archivieren');pump_until(lambda:bool(messages));assert '403' in messages[-1],messages
        w.token=manager;confirm();click('Archivieren');pump_until(lambda:'Keine Organisationseinheiten vorhanden' in text(w.content))
        fill_and_submit(['Support','SUP','0']);click('+ Einheit');pump_until(lambda:'Support' in text(w.content))
        click('← Team');pump_until(lambda:'Zentrales Mitarbeiterverzeichnis' in text(w.content))
        w.token=reader
        load(3,'Urlaubsanträge')
        fill_and_submit(['2027-03-01 09:00','2027-03-03 17:00','Urlaub Frühling']); click('+ Urlaubsantrag')
        pump_until(lambda:'Urlaub Frühling' in text(w.content))
        leave_cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Urlaub Frühling' in text(v)];assert len(leave_cards)==1 and 'Team Dauerhaft' in text(leave_cards[0])
        assert not any(v.get_label() in ('Genehmigen','Ablehnen','+ Mitarbeiter') for v in children(w.content,mod.Gtk.Button))
        w.token=manager
        load(3,'Urlaub Frühling')
        stale_leave=next(v for v in json.loads(subprocess.run([str(CLIENT),'-action','leave'],env=env,text=True,capture_output=True,check=True).stdout) if v['reason']=='Urlaub Frühling')
        stale_leave['endsAt']+=3600
        notices=[];previous_message=w.message;w.message=lambda value:notices.append(str(value))
        w.decide_leave(stale_leave,'approved');pump_until(lambda:bool(notices))
        assert '409' in notices[-1] and 'aktuelle Stand' in notices[-1],notices
        pump_until(lambda:'Urlaub Frühling' in text(w.content) and any(v.get_label()=='Genehmigen' for v in children(w.content,mod.Gtk.Button)))
        w.message=previous_message;click('Genehmigen')
        pump_until(lambda:'Genehmigt' in text(w.content))
        legacy={'id':'old-desktop:leave:3','employeeId':7,'startsAt':1900000000,'endsAt':1900003600,'reason':'Alter unklarer Antrag','status':'pending'}
        event={'topic':'leave.requested','payload':json.dumps(legacy)}
        request=urllib.request.Request(env['AURA_SYNC_URL']+'/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer '+reader,'Content-Type':'application/json'})
        assert urllib.request.urlopen(request,context=ctx,timeout=5).status==202
        load(3,'Alter unklarer Antrag')
        legacy_cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Alter unklarer Antrag' in text(v)]
        assert len(legacy_cards)==1 and 'Mitarbeiterzuordnung ungeklärt' in text(legacy_cards[0])
        assert not any(v.get_label() in ('Genehmigen','Ablehnen') for v in children(legacy_cards[0],mod.Gtk.Button))
        def choose_legacy():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Urlaubszuordnung klären']
            if not ds:return True
            combo=children(ds[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];combo.set_active(0);assert 'Team Dauerhaft' in combo.get_active_text()
            ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(choose_legacy);confirm();next(v for v in children(legacy_cards[0],mod.Gtk.Button) if v.get_label()=='Zuordnung klären').clicked()
        pump_until(lambda:'Mitarbeiterzuordnung ungeklärt' not in text(w.content) and 'Alter unklarer Antrag' in text(w.content))
        w.token=manager
        project=json.loads(subprocess.run([str(CLIENT),'-action','create-project','-name','GUI Projekt','-department','IT','-owner-id','9'],env=env,text=True,capture_output=True,check=True).stdout)
        import sqlite3
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute("CREATE TRIGGER fail_creation_edge BEFORE INSERT ON central_events WHEN NEW.topic='central.project_assignment.added' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        failed=subprocess.run([str(CLIENT),'-action','create-task','-title','Rollback Aufgabe','-department','IT','-priority','Mittel','-assignee-id','9','-project-id',project['id']],env=env,text=True,capture_output=True)
        assert failed.returncode and '500' in failed.stderr
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute('DROP TRIGGER fail_creation_edge')
        assert not any(v['title']=='Rollback Aufgabe' for v in json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout))
        load(2,'Meine Vorgänge')
        notices=[];original_message=w.message;w.message=lambda value:notices.append(str(value))
        stage=[0]
        def create_with_project_picker():
            dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible()]
            if stage[0]==0:
                outer=next((v for v in dialogs if v.get_title()=='Aufgabe anlegen'),None)
                if not outer:return True
                stage[0]=1;next(v for v in children(outer.get_content_area(),mod.Gtk.Button) if v.get_label()=='Projekt auswählen').clicked();return True
            if stage[0]==1:
                chooser=next((v for v in dialogs if v.get_title()=='Projekt auswählen'),None)
                if not chooser:return True
                combo=children(chooser.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id(project['id'])
                stage[0]=2;chooser.response(mod.Gtk.ResponseType.OK);return True
            outer=next((v for v in dialogs if v.get_title()=='Aufgabe anlegen'),None)
            if not outer:return True
            entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[3].get_text()=='9' and entries[6].get_text()==project['id']
            for entry,value in zip(entries[:6],['Zentrale Aufgabe','IT','Mittel','9','2027-05-07 09:00','0']):entry.set_text(value)
            outer.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(create_with_project_picker);click('+ Aufgabe anlegen');pump_until(lambda:'Zentrale Aufgabe' in text(w.content) or notices)
        assert not notices,notices
        w.message=original_message
        edges=json.loads(subprocess.run([str(CLIENT),'-action','sync-project-assignments'],env=env,text=True,capture_output=True,check=True).stdout)
        assert any(edge['projectId']==project['id'] for edge in edges)
        central_task=next(t for t in json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout) if t['title']=='Zentrale Aufgabe')
        original_run,original_form=w.run_client,w.form;legacy_fields=[]
        def legacy_run(args,callback):
            if args==['-action','capabilities']:callback({'projectAssignmentSync':1},None)
            else:original_run(args,callback)
        w.run_client=legacy_run;w.form=lambda title,fields,*args,**kwargs:legacy_fields.append(fields)
        w.central_work_form('task',central_task,True)
        assert len(legacy_fields)==1 and len(legacy_fields[0])==6
        w.run_client=original_run;w.form=original_form
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute("CREATE TRIGGER fail_update_edge BEFORE INSERT ON central_events WHEN NEW.topic='central.project_assignment.removed' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        failed=subprocess.run([str(CLIENT),'-action','update-task','-id',central_task['id'],'-title','Darf nicht bleiben','-department','IT','-priority','Hoch','-assignee-id','9','-expected-project-id',project['id'],'-project-id',''],env=env,text=True,capture_output=True)
        assert failed.returncode and '500' in failed.stderr
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute('DROP TRIGGER fail_update_edge')
        assert any(t['id']==central_task['id'] and t['title']=='Zentrale Aufgabe' for t in json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout))
        assert {'projectId':project['id'],'taskId':central_task['id']} in json.loads(subprocess.run([str(CLIENT),'-action','sync-project-assignments'],env=env,text=True,capture_output=True,check=True).stdout)
        def change_while_form_open():
            dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Aufgabe bearbeiten']
            if not dialogs:return True
            entries=children(dialogs[-1].get_content_area(),mod.Gtk.Entry);assert len(entries)==7 and entries[6].get_text()==project['id']
            op={'clientId':'concurrent-test','operationId':'remove','projectId':project['id'],'taskId':central_task['id'],'present':False,'expected':True}
            subprocess.run([str(CLIENT),'-action','sync-project-assignment-event','-payload',json.dumps(op)],env=env,text=True,capture_output=True,check=True)
            entries[0].set_text('Veralteter Titel');entries[6].set_text('0');dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        notices=[];w.message=lambda value:notices.append(str(value));mod.GLib.idle_add(change_while_form_open)
        task_card=next(v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Zentrale Aufgabe' in text(v))
        next(v for v in children(task_card,mod.Gtk.Button) if v.get_label()=='Bearbeiten').clicked();pump_until(lambda:bool(notices));assert '409' in notices[-1] and 'erneut öffnen' in notices[-1],notices
        assert any(t['id']==central_task['id'] and t['title']=='Zentrale Aufgabe' for t in json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout))
        restore={'clientId':'concurrent-test','operationId':'restore','projectId':project['id'],'taskId':central_task['id'],'present':True,'expected':False}
        subprocess.run([str(CLIENT),'-action','sync-project-assignment-event','-payload',json.dumps(restore)],env=env,text=True,capture_output=True,check=True);w.message=original_message
        subprocess.run([str(CLIENT),'-action','archive-project','-id',project['id']],env=env,text=True,capture_output=True,check=True)
        load(2,'Zentrale Aufgabe')
        def pick_approver():
            dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Freigabe anfordern']
            if not dialogs:return True
            outer=dialogs[-1];buttons=[v for v in children(outer.get_content_area(),mod.Gtk.Button) if v.get_label()=='Person auswählen'];assert len(buttons)==1
            def choose():
                choices=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Person auswählen']
                if not choices:return True
                combo=children(choices[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id('7') and 'Team Dauerhaft' in combo.get_active_text();assert not combo.set_active_id('9');combo.set_active_id('7');choices[-1].response(mod.Gtk.ResponseType.OK)
                def submit():
                    entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[0].get_text()=='7';outer.response(mod.Gtk.ResponseType.OK);return False
                mod.GLib.timeout_add(200,submit);return False
            mod.GLib.idle_add(choose);buttons[0].clicked();return False
        prior_generation=w.generation;mod.GLib.idle_add(pick_approver);click('Freigabe anfordern');pump_until(lambda:w.generation>prior_generation and 'Zentrale Aufgabe' in text(w.content))
        request_env=env.copy();reader_env=request_env.copy();reader_env['AURA_SYNC_TOKEN']=reader
        approvals=json.loads(subprocess.run([str(CLIENT),'-action','central-approvals'],env=reader_env,text=True,capture_output=True,check=True).stdout);assert len(approvals)==1 and approvals[0]['status']=='pending';approval_id=approvals[0]['id']
        forbidden=subprocess.run([str(CLIENT),'-action','central-approval-decision','-id',approval_id,'-status','approved'],env=request_env,text=True,capture_output=True);assert forbidden.returncode and '404' in forbidden.stderr
        task_for_approval=approvals[0]['taskId'];self_request=subprocess.run([str(CLIENT),'-action','central-approval-request','-subject-id',task_for_approval,'-approver-id','9'],env=request_env,text=True,capture_output=True);assert self_request.returncode and '400' in self_request.stderr
        missing=subprocess.run([str(CLIENT),'-action','central-approval-request','-subject-id',task_for_approval,'-approver-id','123456'],env=request_env,text=True,capture_output=True);assert missing.returncode and '400' in missing.stderr,missing.stderr
        w.token=reader;load(2,'Meine Vorgänge')
        action_labels=[b.get_label() for b in children(w.content,mod.Gtk.Button)]
        assert 'Freigaben' in action_labels
        assert all(name not in action_labels for name in ('+ Aufgabe anlegen','+ Projekt','Projektstatus weiter','Bearbeiten','Archivieren','Status weiter'))
        click('Freigaben');pump_until(lambda:'Zentrale Aufgabe' in text(w.content) and 'Offen' in text(w.content));click('Genehmigen');pump_until(lambda:'Genehmigt' in text(w.content))
        repeat=subprocess.run([str(CLIENT),'-action','central-approval-decision','-id',approval_id,'-status','rejected'],env=reader_env,text=True,capture_output=True);assert repeat.returncode and '409' in repeat.stderr
        w.token=manager;load(2,'Zentrale Aufgabe')
        click('Verlauf');pump_until(lambda:'Noch keine Kommentare vorhanden' in text(w.content))
        def comment():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Kommentar hinzufügen']
            if not ds:return True
            editor=children(ds[-1].get_content_area(),mod.Gtk.TextView)[0];editor.get_buffer().set_text('Erste Zeile\nZweite Zeile ä');ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(comment);click('+ Kommentar');pump_until(lambda:'Zweite Zeile ä' in text(w.content));assert 'Mitarbeiter #9' in text(w.content)
        click('← Zurück');pump_until(lambda:'Zentrale Aufgabe' in text(w.content))
        fill_and_submit(['Zentrale Aufgabe','Support','Hoch','9','2027-05-08 09:00','0','0']);click('Bearbeiten');pump_until(lambda:'Support' in text(w.content) and 'Hoch' in text(w.content))
        edges=json.loads(subprocess.run([str(CLIENT),'-action','sync-project-assignments'],env=env,text=True,capture_output=True,check=True).stdout)
        assert not any(edge['projectId']==project['id'] for edge in edges)
        click('Anhänge');pump_until(lambda:'Keine Anhänge vorhanden' in text(w.content))
        source=Path(tmp)/'Bericht ä.txt';source.write_text('Beispiel-Anhang',encoding='utf-8')
        def choose_file():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.FileChooserDialog) and v.get_visible()]
            if not ds:return True
            assert ds[-1].set_filename(str(source));mod.GLib.timeout_add(300,lambda:(ds[-1].response(mod.Gtk.ResponseType.OK),False)[1]);return False
        mod.GLib.idle_add(choose_file);click('+ Datei');pump_until(lambda:'Bericht ä.txt' in text(w.content))
        def save_file():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.FileChooserDialog) and v.get_visible()]
            if not ds:return True
            ds[-1].set_current_folder(tmp);ds[-1].set_current_name('download.txt');mod.GLib.timeout_add(300,lambda:(ds[-1].response(mod.Gtk.ResponseType.OK),False)[1]);return False
        mod.GLib.idle_add(save_file);click('Herunterladen');pump_until(lambda:(Path(tmp)/'download.txt').exists());assert (Path(tmp)/'download.txt').read_bytes()==source.read_bytes()
        result=subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True)
        selected=next(v for v in json.loads(result.stdout) if v['title']=='Zentrale Aufgabe');task_id=selected['id'];assert selected['dueAt']>0 and selected.get('recurrenceDays',0)==0
        reader_env=env.copy();reader_env['AURA_SYNC_TOKEN']=reader
        denied=subprocess.run([str(CLIENT),'-action','comments','-subject-type','task','-subject-id',task_id],env=reader_env,text=True,capture_output=True)
        assert denied.returncode and '404' in denied.stderr,denied.stderr
        denied=subprocess.run([str(CLIENT),'-action','archive-task','-id',task_id],env=reader_env,text=True,capture_output=True);assert denied.returncode and '403' in denied.stderr
        denied=subprocess.run([str(CLIENT),'-action','subject-attachments','-subject-type','task','-subject-id',task_id],env=reader_env,text=True,capture_output=True)
        assert denied.returncode and '404' in denied.stderr,denied.stderr
        w.select(2);pump_until(lambda:'Zentrale Aufgabe' in text(w.content))
        fill_and_submit(['Wiederkehrend','IT','Mittel','9','2027-05-10 09:00','7','0']);click('+ Aufgabe anlegen');pump_until(lambda:'Wiederkehrend' in text(w.content) and 'alle 7 Tage' in text(w.content))
        scheduled=json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout)
        recurring=next(v for v in scheduled if v['title']=='Wiederkehrend');assert recurring['recurrenceDays']==7
        cards=[v for v in w.content.get_children() if 'Wiederkehrend' in text(v) and any(b.get_label()=='Details' for b in children(v,mod.Gtk.Button))];assert len(cards)==1
        [v for v in children(cards[0],mod.Gtk.Button) if v.get_label()=='Details'][0].clicked();pump_until(lambda:'Voraussetzungen' in text(w.content))
        def choose_dependency():
            ds=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Voraussetzung auswählen']
            if not ds:return True
            combo=children(ds[-1].get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.set_active_id(task_id);ds[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(choose_dependency);click('+ Abhängigkeit');pump_until(lambda:'Zentrale Aufgabe' in text(w.content) and 'Entfernen' in text(w.content))
        subprocess.run([str(CLIENT),'-action','task-status','-id',recurring['id'],'-status','in_progress'],env=env,text=True,capture_output=True,check=True)
        denied=subprocess.run([str(CLIENT),'-action','task-status','-id',recurring['id'],'-status','done'],env=env,text=True,capture_output=True);assert denied.returncode and '409' in denied.stderr
        click('Entfernen');pump_until(lambda:'Keine Voraussetzungen.' in text(w.content))
        fill_and_submit(['Testfall vorbereiten','open','9','2027-05-09 09:00']);click('+ Unteraufgabe');pump_until(lambda:'Testfall vorbereiten' in text(w.content))
        assert 'Serverkonto-ID 9' in text(w.content)
        denied=subprocess.run([str(CLIENT),'-action','task-status','-id',recurring['id'],'-status','done'],env=env,text=True,capture_output=True);assert denied.returncode and '409' in denied.stderr
        oldchild=json.loads(subprocess.run([str(CLIENT),'-action','subtasks','-task-id',recurring['id']],env=env,text=True,capture_output=True,check=True).stdout)[0]
        notices=[];previous_message=w.message;w.message=lambda value:notices.append(str(value))
        def edit_subtask_concurrently():
            outer=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabe bearbeiten'),None)
            if not outer:return True
            subprocess.run([str(CLIENT),'-action','subtask-save','-task-id',recurring['id'],'-id',oldchild['id'],'-title','Testfall vorbereiten','-status','open','-assignee-id','9','-due','1900000000'],env=env,text=True,capture_output=True,check=True)
            entries=children(outer.get_content_area(),mod.Gtk.Entry);entries[0].set_text('Veraltete Unteraufgabe');outer.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(edit_subtask_concurrently);click('Bearbeiten');pump_until(lambda:bool(notices))
        assert '409' in notices[-1] and 'erneut öffnen' in notices[-1],notices
        current_child=json.loads(subprocess.run([str(CLIENT),'-action','subtasks','-task-id',recurring['id']],env=env,text=True,capture_output=True,check=True).stdout)[0]
        assert current_child['title']=='Testfall vorbereiten' and current_child['dueAt']==1900000000
        subprocess.run([str(CLIENT),'-action','subtask-save','-task-id',recurring['id'],'-id',oldchild['id'],'-title','Testfall vorbereiten','-status','open','-assignee-id','9','-due',str(oldchild['dueAt'])],env=env,text=True,capture_output=True,check=True)
        w.message=previous_message;w.show_task_details(recurring);pump_until(lambda:'Testfall vorbereiten' in text(w.content))
        notices=[];w.message=lambda value:notices.append(str(value))
        def archive_subtask_concurrently():
            dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.MessageDialog) and d.get_visible() and d.get_property('text')=='Unteraufgabe archivieren?'),None)
            if not dialog:return True
            subprocess.run([str(CLIENT),'-action','subtask-save','-task-id',recurring['id'],'-id',oldchild['id'],'-title','Testfall vorbereiten','-status','open','-assignee-id','9','-due','1900000000'],env=env,text=True,capture_output=True,check=True)
            dialog.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(archive_subtask_concurrently);click('Archivieren');pump_until(lambda:bool(notices))
        assert '409' in notices[-1] and 'erneut öffnen' in notices[-1],notices
        current_child=json.loads(subprocess.run([str(CLIENT),'-action','subtasks','-task-id',recurring['id']],env=env,text=True,capture_output=True,check=True).stdout)[0]
        assert current_child['id']==oldchild['id'] and current_child['dueAt']==1900000000
        subprocess.run([str(CLIENT),'-action','subtask-save','-task-id',recurring['id'],'-id',oldchild['id'],'-title','Testfall vorbereiten','-status','open','-assignee-id','9','-due',str(oldchild['dueAt'])],env=env,text=True,capture_output=True,check=True)
        w.message=previous_message;w.show_task_details(recurring);pump_until(lambda:'Testfall vorbereiten' in text(w.content))
        def choose_central_subtask_status():
            outer=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabe bearbeiten'),None)
            if not outer:return True
            def choose():
                dialog=next((d for d in mod.Gtk.Window.list_toplevels() if isinstance(d,mod.Gtk.Dialog) and d.get_visible() and d.get_title()=='Unteraufgabenstatus auswählen'),None)
                if not dialog:return True
                combo=children(dialog.get_content_area(),mod.Gtk.ComboBoxText)[0];assert combo.get_active_id()=='open' and combo.set_active_id('done')
                dialog.response(mod.Gtk.ResponseType.OK);return False
            mod.GLib.idle_add(choose);next(b for b in children(outer.get_content_area(),mod.Gtk.Button) if b.get_label()=='Status auswählen').clicked()
            entries=children(outer.get_content_area(),mod.Gtk.Entry);assert entries[1].get_text()=='done';entries[0].set_text('Testfall abgeschlossen')
            outer.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(choose_central_subtask_status);click('Bearbeiten');pump_until(lambda:'Testfall abgeschlossen' in text(w.content))
        assert 'Erledigt (done)' in text(w.content)
        stage=[0]
        def clear_central_assignee():
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
            assert children(outer.get_content_area(),mod.Gtk.Entry)[2].get_text()=='0';outer.response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(clear_central_assignee);click('Bearbeiten');pump_until(lambda:'Nicht zugewiesen' in text(w.content))
        child=json.loads(subprocess.run([str(CLIENT),'-action','subtasks','-task-id',recurring['id']],env=env,text=True,capture_output=True,check=True).stdout)
        assert len(child)==1 and child[0]['assigneeId']==0
        import sqlite3
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute("CREATE TRIGGER fail_next_subtask BEFORE INSERT ON central_events WHEN NEW.topic='central.task_subtask.saved' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        denied=subprocess.run([str(CLIENT),'-action','task-status','-id',recurring['id'],'-status','done'],env=env,text=True,capture_output=True);assert denied.returncode and '500' in denied.stderr
        snapshot=json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout)
        assert len([v for v in snapshot if v['title']=='Wiederkehrend'])==1 and next(v for v in snapshot if v['id']==recurring['id'])['status']=='in_progress'
        with sqlite3.connect(env['AURA_CENTRAL_DB']) as sql:sql.execute('DROP TRIGGER fail_next_subtask')
        subprocess.run([str(CLIENT),'-action','task-status','-id',recurring['id'],'-status','done'],env=env,text=True,capture_output=True,check=True)
        scheduled=json.loads(subprocess.run([str(CLIENT),'-action','tasks'],env=env,text=True,capture_output=True,check=True).stdout)
        successor=next(v for v in scheduled if v['title']=='Wiederkehrend' and v['id']!=recurring['id']);assert successor['dueAt']==recurring['dueAt']+7*86400 and successor['status']=='open'
        children_next=json.loads(subprocess.run([str(CLIENT),'-action','subtasks','-task-id',successor['id']],env=env,text=True,capture_output=True,check=True).stdout);assert len(children_next)==1 and children_next[0]['title']=='Testfall abgeschlossen' and children_next[0]['status']=='open' and children_next[0]['assigneeId']==0
        w.select(4);pump_until(lambda:'Facility & IT' in text(w.content))
        fill_and_submit(['Zentrales Ticket','IT','Mittel','9'],'9');click('+ Ticket anlegen');pump_until(lambda:'Zentrales Ticket' in text(w.content))
        click('Verlauf');pump_until(lambda:'Noch keine Kommentare vorhanden' in text(w.content))
        mod.GLib.idle_add(comment);click('+ Kommentar');pump_until(lambda:'Zweite Zeile ä' in text(w.content))
        click('← Zurück');pump_until(lambda:'Zentrales Ticket' in text(w.content))
        fill_and_submit(['Zentrales Ticket','Facility','Hoch','9']);click('Bearbeiten');pump_until(lambda:'Facility · Hoch' in text(w.content))
        click('Anhänge');pump_until(lambda:'Keine Anhänge vorhanden' in text(w.content))
        mod.GLib.idle_add(choose_file);click('+ Datei');pump_until(lambda:'Bericht ä.txt' in text(w.content))
        confirm();click('Archivieren');pump_until(lambda:'Keine Anhänge vorhanden' in text(w.content))
        tickets=json.loads(subprocess.run([str(CLIENT),'-action','tickets'],env=env,text=True,capture_output=True,check=True).stdout)
        ticket=next(v for v in tickets if v['title']=='Zentrales Ticket');ticket_id=ticket['id'];assert ticket['category']=='Facility' and ticket['status']=='open' and ticket['createdBy']==9
        click('← Zurück');pump_until(lambda:'Zentrales Ticket' in text(w.content));confirm();click('Archivieren');pump_until(lambda:'Zentrales Ticket' not in text(w.content))
        os.environ['AURA_LOCAL_DB']=tmp+'/local-workspace.sqlite'
        setup=mod.LocalStore(os.environ['AURA_LOCAL_DB'])
        setup.create('Lokaler Admin','localadmin','Lokales-Passwort-2026')
        fill_and_submit(['localadmin','Lokales-Passwort-2026']); w.local_login()
        pump_until(lambda:w.local_mode and 'Noch keine Einträge' in text(w.content))
        fill_and_submit(['Nur Lokal','Offline','4','Display']); click('+ Raum anlegen')
        pump_until(lambda:'Nur Lokal' in text(w.content))
        fill_and_submit([env['AURA_SYNC_URL'],manager,str(cert)]); w.connection()
        pump_until(lambda:not w.local_mode and w.local is None and 'Noch keine Einträge' in text(w.content))
        assert 'Nur Lokal' not in text(w.content)
        check=mod.LocalStore(os.environ['AURA_LOCAL_DB']); check.open(); check.login('localadmin','Lokales-Passwort-2026')
        assert check.rooms()[0]['name']=='Nur Lokal'; check.close()
        def cli(action,*args,token=manager):
            client_env=env.copy(); client_env['AURA_SYNC_TOKEN']=token
            result=subprocess.run([str(CLIENT),'-action',action,*args],env=client_env,text=True,capture_output=True,check=True)
            return json.loads(result.stdout)
        archived_rooms=cli('rooms','-include-archived')
        assert len(archived_rooms)==1 and archived_rooms[0]['name']=='Raum Geändert' and not archived_rooms[0]['active'],archived_rooms
        archived_assets=cli('facility-assets','-include-archived')
        assert len(archived_assets)==2 and any(v['name']=='Drucker Neu' and not v['active'] for v in archived_assets),archived_assets
        archived_projects=cli('projects')
        assert any(p['name']=='Projekt Geändert' and not p['active'] for p in archived_projects),archived_projects
        server.terminate(); server.wait(timeout=5)
        server=subprocess.Popen([str(SERVER),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for attempt in range(100):
            try:
                urllib.request.urlopen(env['AURA_SYNC_URL']+'/health',context=ctx,timeout=1).close(); break
            except OSError: time.sleep(.05)
        else: raise RuntimeError('server did not restart')
        assert cli('rooms','-include-archived')==archived_rooms
        assert sorted(cli('projects'),key=lambda p:p['id'])==sorted(archived_projects,key=lambda p:p['id'])
        employees=cli('employees',token=reader);assert len(employees)==1 and employees[0]['name']=='Team Dauerhaft' and employees[0]['accountEmployeeId']==7,employees
        approvals=cli('central-approvals',token=reader);assert len(approvals)==1 and approvals[0]['status']=='approved' and approvals[0]['decidedBy']=='7',approvals
        organizations=cli('central-organizations',token=reader);assert len(organizations)==1 and organizations[0]['name']=='Support',organizations
        assert sorted(cli('facility-assets','-include-archived'),key=lambda v:v['id'])==sorted(archived_assets,key=lambda v:v['id'])
        orders=cli('central-maintenance','-asset-id',asset_id,token=reader);assert len(orders)==2 and {v['status'] for v in orders}=={'open','completed'} and orders[0]['recurrenceDays']==30,orders
        completed=next(v for v in orders if v['status']=='completed');recurring=next(v for v in orders if v['status']=='open');assert recurring['dueAt']-completed['dueAt']==30*86400
        cli('central-maintenance-archive','-asset-id',asset_id,'-id',completed['id']);assert len(cli('central-maintenance','-asset-id',asset_id))==1
        tasks=cli('tasks');task=next(v for v in tasks if v['id']==task_id);assert task['department']=='Support' and task['priority']=='Hoch' and task['createdBy']==9 and task['status']=='open',task
        comments=cli('comments','-subject-type','task','-subject-id',task_id)
        assert len(comments)==1 and comments[0]['body']=='Erste Zeile\nZweite Zeile ä' and comments[0]['authorId']=='9',comments
        attachments=cli('subject-attachments','-subject-type','task','-subject-id',task_id)
        assert len(attachments)==1 and attachments[0]['originalName']=='Bericht ä.txt' and attachments[0]['uploadedBy']=='9',attachments
        target=Path(tmp)/'download-after-restart.txt';v=attachments[0]
        cli('subject-attachment-download','-subject-type','task','-subject-id',task_id,'-id',v['id'],'-sha256',v['sha256'],'-size',str(v['sizeBytes']),'-file',str(target))
        assert target.read_bytes()==source.read_bytes()
        bad=Path(tmp)/'invalid.txt';fail=subprocess.run([str(CLIENT),'-action','subject-attachment-download','-subject-type','task','-subject-id',task_id,'-id',v['id'],'-sha256','0'*64,'-size',str(v['sizeBytes']),'-file',str(bad)],env=env,text=True,capture_output=True)
        assert fail.returncode and not bad.exists()
        existing=target.read_bytes();fail=subprocess.run([str(CLIENT),'-action','subject-attachment-download','-subject-type','task','-subject-id',task_id,'-id',v['id'],'-sha256',v['sha256'],'-size',str(v['sizeBytes']),'-file',str(target)],env=env,text=True,capture_output=True)
        assert fail.returncode and target.read_bytes()==existing
        server_file=Path(tmp)/'events.ndjson.attachments'/v['sha256'];server_file.write_bytes(b'verfaelscht')
        corrupt=Path(tmp)/'corrupt.txt';fail=subprocess.run([str(CLIENT),'-action','subject-attachment-download','-subject-type','task','-subject-id',task_id,'-id',v['id'],'-sha256',v['sha256'],'-size',str(v['sizeBytes']),'-file',str(corrupt)],env=env,text=True,capture_output=True)
        assert fail.returncode and not corrupt.exists()
        cli('subject-attachment-archive','-subject-type','task','-subject-id',task_id,'-id',v['id'])
        assert cli('subject-attachments','-subject-type','task','-subject-id',task_id)==[]
        archived=cli('subject-attachments-all','-subject-type','task','-subject-id',task_id)[0];assert not archived['active']
        denied=subprocess.run([str(CLIENT),'-action','subject-attachments-all','-subject-type','task','-subject-id',task_id],env=reader_env,text=True,capture_output=True);assert denied.returncode and ('403' in denied.stderr or '404' in denied.stderr)
        failed=subprocess.run([str(CLIENT),'-action','subject-attachment-restore','-subject-type','task','-subject-id',task_id,'-payload',json.dumps(archived)],env=env,text=True,capture_output=True);assert failed.returncode and '409' in failed.stderr
        server_file.write_bytes(source.read_bytes())
        w.token=manager;w.show_attachments('task',task);pump_until(lambda:'Keine Anhänge vorhanden' in text(w.content))
        click('Archivierte Anhänge anzeigen');pump_until(lambda:'Archiviert' in text(w.content))
        confirm();click('Wiederherstellen');pump_until(lambda:any(b.get_label()=='Archivieren' for b in children(w.content,mod.Gtk.Button)))
        assert len(cli('subject-attachments','-subject-type','task','-subject-id',task_id))==1
        failed=subprocess.run([str(CLIENT),'-action','subject-attachment-restore','-subject-type','task','-subject-id',task_id,'-payload',json.dumps(archived)],env=env,text=True,capture_output=True);assert failed.returncode and '409' in failed.stderr
        failed=subprocess.run([str(CLIENT),'-action','ticket-status','-id',ticket_id,'-status','in_progress'],env=env,text=True,capture_output=True);assert failed.returncode and '404' in failed.stderr
        cli('archive-task','-id',task_id)
        failed=subprocess.run([str(CLIENT),'-action','task-status','-id',task_id,'-status','in_progress'],env=env,text=True,capture_output=True);assert failed.returncode and '404' in failed.stderr
        leaves=cli('leave',token=reader)
        assert any(v['reason']=='Urlaub Frühling' and v['status']=='approved' and v['identityVerified'] for v in leaves),leaves
        assert any(v['reason']=='Alter unklarer Antrag' and v['identityVerified'] and v['employeeId']==7 for v in leaves),leaves
        # Reconcile an already decided historical request through both GTK
        # selectors, including cancellation and self-decision rejection.
        lead=cli('central-employee-create','-name','Freigabe Leitung','-role','Leitung','-department','IT','-presence','office')
        cli('employee-account-link','-id',lead['id'],'-account-id','9')
        staff=cli('employees');person=next(v for v in staff if v['name']=='Team Dauerhaft')
        old_decision={'id':'old-desktop:leave:44','employeeId':77,'startsAt':1902000000,'endsAt':1902003600,'reason':'Altentscheidung GUI','status':'approved','decidedBy':55}
        event={'topic':'leave.decided','payload':json.dumps(old_decision)}
        request=urllib.request.Request(env['AURA_SYNC_URL']+'/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':'Bearer '+manager,'Content-Type':'application/json'})
        assert urllib.request.urlopen(request,context=ctx,timeout=5).status==202
        w.token=manager;load(3,'Altentscheidung GUI')
        def choose_decision(first,second,response):
            def choose():
                dialogs=[v for v in mod.Gtk.Window.list_toplevels() if isinstance(v,mod.Gtk.Dialog) and v.get_visible() and v.get_title()=='Urlaubszuordnung klären']
                if not dialogs:return True
                combos=children(dialogs[-1].get_content_area(),mod.Gtk.ComboBoxText);assert len(combos)==2
                if first is not None:assert combos[0].set_active_id(first)
                if second is not None:assert combos[1].set_active_id(second)
                dialogs[-1].response(response);return False
            mod.GLib.idle_add(choose)
        def decision_button():
            cards=[v for v in children(w.content,mod.Gtk.Box) if v.get_style_context().has_class('card') and 'Altentscheidung GUI' in text(v)]
            assert len(cards)==1
            return next(v for v in children(cards[0],mod.Gtk.Button) if v.get_label()=='Zuordnung klären')
        choose_decision(None,None,mod.Gtk.ResponseType.CANCEL);decision_button().clicked()
        assert next(v for v in cli('leave') if v['id']==old_decision['id'])['identityVerified'] is False
        notices=[];prior_message=w.message;w.message=lambda message:notices.append(message)
        choose_decision(person['id'],person['id'],mod.Gtk.ResponseType.OK);decision_button().clicked()
        assert notices and 'unterschiedlichen Personen' in notices[-1]
        assert next(v for v in cli('leave') if v['id']==old_decision['id'])['identityVerified'] is False
        w.message=prior_message;choose_decision(person['id'],lead['id'],mod.Gtk.ResponseType.OK);confirm();decision_button().clicked()
        def resolved_legacy():
            matches=[v for v in cli('leave') if v['id']==old_decision['id']]
            return matches and matches[0]['identityVerified'] and matches[0]['employeeId']==7 and matches[0]['decidedBy']==9
        pump_until(resolved_legacy)
        # A reader can see their assigned task's comments and download active
        # attachments, but only collaborators can add or archive content.
        read_task=cli('create-task','-title','Lesbarer Verlauf','-department','IT','-priority','Mittel','-assignee-id','7')
        cli('add-comment','-subject-type','task','-subject-id',read_task['id'],'-body','Nur lesen')
        sample=Path(tmp)/'reader.txt';sample.write_text('Lesbare Datei')
        uploaded=cli('subject-attachment-upload','-subject-type','task','-subject-id',read_task['id'],'-file',str(sample))
        w.token=reader;load(2,'Lesbarer Verlauf');w.show_comments('task',read_task);pump_until(lambda:'Nur lesen' in text(w.content))
        assert '+ Kommentar' not in [b.get_label() for b in children(w.content,mod.Gtk.Button)]
        w.show_attachments('task',read_task);pump_until(lambda:'reader.txt' in text(w.content))
        actions=[b.get_label() for b in children(w.content,mod.Gtk.Button)]
        assert 'Herunterladen' in actions and all(v not in actions for v in ('+ Datei','Archivieren','Archivierte Anhänge anzeigen'))
        downloaded=Path(tmp)/'reader-downloaded.txt'
        cli('subject-attachment-download','-subject-type','task','-subject-id',read_task['id'],'-id',uploaded['id'],'-sha256',uploaded['sha256'],'-size',str(uploaded['sizeBytes']),'-file',str(downloaded),token=reader)
        assert downloaded.read_text()=='Lesbare Datei'
        w.token='commenter';w.show_comments('task',read_task);pump_until(lambda:'Nur lesen' in text(w.content))
        assert '+ Kommentar' in [b.get_label() for b in children(w.content,mod.Gtk.Button)]
        w.show_attachments('task',read_task);pump_until(lambda:'reader.txt' in text(w.content))
        actions=[b.get_label() for b in children(w.content,mod.Gtk.Button)]
        assert '+ Datei' in actions and 'Archivieren' in actions and 'Archivierte Anhänge anzeigen' not in actions
        cli('add-comment','-subject-type','task','-subject-id',read_task['id'],'-body','Mitarbeiterbeitrag',token='commenter')
        cli('subject-attachment-upload','-subject-type','task','-subject-id',read_task['id'],'-file',str(sample),token='commenter')
        env['AURA_SYNC_TOKEN']=reader
        result=subprocess.run([str(CLIENT),'-action','rooms'],env=env,text=True,capture_output=True,check=True)
        assert json.loads(result.stdout)==[],result.stdout
        result=subprocess.run([str(CLIENT),'-action','update-room','-id','nope','-name','X','-location','Y','-capacity','1'],env=env,text=True,capture_output=True)
        assert result.returncode and '403' in result.stderr,result.stderr
        room={'id':'linux-test:room:1','name':'Sync Raum','location':'Berlin','capacity':4,'equipment':'','active':True}
        operation={'clientId':'linux-test','operationId':'op-1','action':'create','room':room,'expected':None}
        cli('sync-room-event','-payload',json.dumps(operation));cli('sync-room-event','-payload',json.dumps(operation))
        saved=cli('rooms');assert any(v['id']==room['id'] for v in saved),saved
        changed={**room,'location':'Bonn'};cli('update-room','-id',room['id'],'-name',room['name'],'-location','Bonn','-capacity','4','-equipment','')
        stale={'clientId':'linux-test','operationId':'op-2','action':'update','room':{**room,'capacity':5},'expected':room}
        manager_env=env.copy();manager_env['AURA_SYNC_TOKEN']=manager
        result=subprocess.run([str(CLIENT),'-action','sync-room-event','-payload',json.dumps(stale)],env=manager_env,text=True,capture_output=True);assert result.returncode and '409' in result.stderr,result.stderr
        cli('sync-room-event','-payload',json.dumps(operation));assert next(v for v in cli('rooms') if v['id']==room['id'])['location']=='Bonn'
        print('GTK forms, central bookings, comments, attachments, room-sync protocol, authorization and persistence: OK')
        w.disconnect_by_func(mod.Gtk.main_quit); w.destroy()
    finally:
        server.terminate(); server.wait(timeout=5)

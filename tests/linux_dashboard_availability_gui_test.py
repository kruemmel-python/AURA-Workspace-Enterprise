#!/usr/bin/python3
"""Check dashboard availability against real local bookings and blocks."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='aura-dashboard-') as tmp:
    os.environ['AURA_LOCAL_DB']=tmp+'/workspace.sqlite'
    for key in ('AURA_SYNC_URL','AURA_SYNC_TOKEN','AURA_SYNC_CA_CERT'):os.environ.pop(key,None)
    spec=importlib.util.spec_from_file_location('aura_gui',root/'clients/linux/aura_gui.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    store=mod.LocalStore(os.environ['AURA_LOCAL_DB']);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
    for name in ('Gebucht','Gesperrt','Frei'):store.save_room(0,name,'Berlin',5,'Monitor')
    rooms={v['name']:v['id'] for v in store.rooms()};now=int(time.time())
    store.book(rooms['Gebucht'],now-60,now+3600,1)
    store.block_room(rooms['Gesperrt'],now-60,now+3600,'Wartung')
    w=mod.AuraWindow();w.local=store;w.local_mode=True;w.show_all()
    def children(widget):
        result=[widget]
        if isinstance(widget,mod.Gtk.Container):
            for child in widget.get_children():result.extend(children(child))
        return result
    def labels():return [x.get_text() for x in children(w.content) if isinstance(x,mod.Gtk.Label)]
    def wait(predicate):
        end=time.monotonic()+15
        while time.monotonic()<end:
            while mod.Gtk.events_pending():mod.Gtk.main_iteration()
            if predicate():return
            time.sleep(.02)
        raise AssertionError('dashboard timeout: '+repr(labels()))
    def quick_ticket(button_name,title,category):
        buttons=[x for x in children(w.content) if isinstance(x,mod.Gtk.Button) and x.get_label()==button_name];assert len(buttons)==1
        def fill():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Ticket anlegen']
            if not dialogs:return True
            entries=[x for x in children(dialogs[-1].get_content_area()) if isinstance(x,mod.Gtk.Entry)]
            assert [e.get_text() for e in entries]==[title,category,'Mittel','1']
            if not title:entries[0].set_text('IT-Test')
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(fill);buttons[0].clicked()
    w.select(0);wait(lambda:'1 von 3' in labels())
    assert 'Aktive Mitarbeiter' in labels() and 'Freie Meetingräume' in labels()
    wait(lambda:any(text.startswith('Audit: ') and 'room.blocked' in text for text in labels()))
    assert any(text.startswith('Gebucht ·') and text.endswith('Belegt') for text in labels())
    assert any(text.startswith('Gesperrt ·') and text.endswith('Gesperrt') for text in labels())
    assert any(text.startswith('Frei ·') and text.endswith('Frei') for text in labels())
    wait(lambda:any(text.endswith(' · Gebucht · Buchung') for text in labels()) and any(text.endswith(' · Gesperrt · Sperre') for text in labels()))
    quick_ticket('+ Kaffeebohnen-Nachfüllung melden','Kaffeebohnen nachfüllen','Facility')
    wait(lambda:any(v['title']=='Kaffeebohnen nachfüllen' and v['category']=='Facility' for v in store.tickets()))
    w.select(1);wait(lambda:any(text.endswith(' · Gesperrt') for text in labels()))
    assert any(text.endswith(' · Belegt') for text in labels()) and any(text.endswith(' · Frei') for text in labels())
    def row_button(name,action):
        def card(widget):
            parent=widget.get_parent()
            while parent is not None and not parent.get_style_context().has_class('card'):parent=parent.get_parent()
            return parent
        found=[x for x in children(w.content) if isinstance(x,mod.Gtk.Button) and x.get_label()==action and card(x) is not None and any(isinstance(y,mod.Gtk.Label) and y.get_text()==name for y in children(card(x)))]
        assert len(found)==1
        return found[0]
    def book_from_row(name,offset=120):
        button=row_button(name,'Buchen')
        def fill():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Raum buchen']
            if not dialogs:return True
            entries=[x for x in children(dialogs[-1].get_content_area()) if isinstance(x,mod.Gtk.Entry)]
            default_start=mod.AuraWindow.parse_local_time(entries[0].get_text())
            default_end=mod.AuraWindow.parse_local_time(entries[1].get_text())
            assert -5<=default_start-int(time.time())<=300 and default_end-default_start==3600
            values=[datetime.fromtimestamp(now+offset).strftime('%Y-%m-%d %H:%M'),datetime.fromtimestamp(now+offset+3600).strftime('%Y-%m-%d %H:%M'),'1','1','7']
            assert len(entries)==len(values)
            for entry,value in zip(entries,values):entry.set_text(value)
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(fill);button.clicked()
        wait(lambda:any(text=='Buchungen · '+name for text in labels()))
    book_from_row('Frei');assert len(store.bookings(rooms['Frei']))==1
    w.select(1);wait(lambda:any(text=='Gebucht' for text in labels()))
    book_from_row('Gebucht',172800);assert len(store.bookings(rooms['Gebucht']))==2
    def block_from_row(name):
        w.select(1);wait(lambda:any(text==name for text in labels()))
        button=row_button(name,'Sperren')
        def fill():
            dialogs=[x for x in mod.Gtk.Window.list_toplevels() if isinstance(x,mod.Gtk.Dialog) and x.get_visible() and x.get_title()=='Sperrzeit anlegen']
            if not dialogs:return True
            entries=[x for x in children(dialogs[-1].get_content_area()) if isinstance(x,mod.Gtk.Entry)]
            for entry,value in zip(entries,[datetime.fromtimestamp(now+7200).strftime('%Y-%m-%d %H:%M'),datetime.fromtimestamp(now+7800).strftime('%Y-%m-%d %H:%M'),'Wartung']):entry.set_text(value)
            dialogs[-1].response(mod.Gtk.ResponseType.OK);return False
        mod.GLib.idle_add(fill);button.clicked();wait(lambda:any(text=='Buchungen · '+name for text in labels()))
    block_from_row('Frei');assert any(item['status']=='blocked' for item in store.bookings(rooms['Frei']))
    booking=next(b for b in store.bookings(rooms['Gebucht']) if b['startsAt']<=now<b['endsAt']);store.cancel_booking(rooms['Gebucht'],booking['id'])
    w.select(0);wait(lambda:'2 von 3' in labels())
    wait(lambda:any(text.endswith(' · Gesperrt · Sperre') for text in labels()))
    assert not any(text.endswith(' · Gebucht · Buchung') for text in labels())
    original_run_client=w.run_client
    def failed_room(args,callback):
        if args[1]=='bookings' and args[-1]==str(rooms['Gebucht']):
            mod.GLib.idle_add(lambda:(callback(None,'Testfehler'),False)[1]);return
        original_run_client(args,callback)
    w.run_client=failed_room;w.refresh();wait(lambda:'Einige Raumtermine konnten nicht geladen werden.' in labels());w.run_client=original_run_client
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url=f'http://127.0.0.1:{port}';env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_JOURNAL':tmp+'/server.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_URL':url,'AURA_SYNC_TOKEN':'test-token'})
    for key in ('AURA_SYNC_IDENTITIES','AURA_SYNC_REQUIRE_IDENTITIES','AURA_SYNC_REQUIRE_TLS','AURA_SYNC_TLS_CERT','AURA_SYNC_TLS_KEY','AURA_SYNC_CA_CERT'):env.pop(key,None)
    env['AURA_SYNC_IDENTITIES']=json.dumps({'test-token':{'employeeId':1,'permissions':['*']},'leave-only':{'employeeId':2,'permissions':['leave.request','rooms.read','tasks.read','tickets.read','employees.read']}})
    server=subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',timeout=1).close();break
            except OSError:time.sleep(.05)
        else:raise AssertionError('server failed to start')
        client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
        def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,check=True,capture_output=True,text=True).stdout)
        first=cli('-action','create-room','-name','Zentral belegt','-location','Berlin','-capacity','5')
        cli('-action','create-room','-name','Zentral frei','-location','Berlin','-capacity','5')
        cli('-action','central-book','-room-key',first['id'],'-starts-at',str(now-60),'-ends-at',str(now+3600))
        w.local_mode=False;w.local=None;store.close();w.url=url;w.token='test-token';w.ca='';w.select(0)
        wait(lambda:'1 von 2' in labels())
        assert any(text.startswith('Zentral belegt ·') and text.endswith('Belegt') for text in labels())
        wait(lambda:any(text.endswith(' · Zentral belegt · Buchung') for text in labels()))
        quick_ticket('+ Neues Ticket an IT-Support senden','','IT-Support')
        wait(lambda:any(v['title']=='IT-Test' and v['category']=='IT-Support' for v in cli('-action','tickets')))
        w.token='leave-only';w.select(0);wait(lambda:'Office Board & Quick Actions' in labels())
        actions=[x.get_label() for x in children(w.content) if isinstance(x,mod.Gtk.Button)]
        assert '+ Urlaubsantrag einreichen' in actions
        assert all(name not in actions for name in ('+ Kaffeebohnen-Nachfüllung melden','+ Neues Ticket an IT-Support senden','+ Aufgabe anlegen','+ Raum anlegen'))
        w.token='test-token';w.select(0);wait(lambda:'1 von 2' in labels())
        w.select(1);wait(lambda:any(text.endswith(' · Belegt') for text in labels()))
        assert any(text.endswith(' · Frei') for text in labels())
        book_from_row('Zentral frei')
        central_rooms=cli('-action','rooms');free=next(room for room in central_rooms if room['name']=='Zentral frei')
        assert len(cli('-action','central-bookings','-room-key',free['id']))==1
        block_from_row('Zentral frei');assert any(item['status']=='blocked' for item in cli('-action','central-bookings','-room-key',free['id']))
    finally:
        server.terminate();server.wait(timeout=5)
        w.disconnect_by_func(mod.Gtk.main_quit);w.destroy()
print('GTK room availability, direct booking, blocks and cancellation: OK')

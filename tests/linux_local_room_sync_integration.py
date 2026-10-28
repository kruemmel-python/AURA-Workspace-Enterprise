#!/usr/bin/python3
"""Exercise durable outbound room synchronization with a real isolated TLS server."""
import importlib.util,json,os,socket,ssl,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore,LocalStoreError
from room_sync import sync_rooms,inspect_room_conflict,rebase_room_conflict,accept_server_room_conflict
server_bin=root/'KR_BUILD/linux-x64/aura-sync-server';client=root/'KR_BUILD/linux-x64/aura-workspace-enterprise'
with tempfile.TemporaryDirectory(prefix='aura-room-sync-') as tmp:
    cert=Path(tmp)/'cert.pem';key=Path(tmp)/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'https://127.0.0.1:{port}';token='manager-token';other='other-token'
    env=os.environ.copy();env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}','AURA_SYNC_URL':url,'AURA_SYNC_JOURNAL':tmp+'/events.ndjson','AURA_CENTRAL_DB':tmp+'/central.sqlite','AURA_SYNC_TOKEN':token,'AURA_SYNC_IDENTITIES':json.dumps({token:{'employeeId':9,'permissions':['rooms.manage']},other:{'employeeId':8,'permissions':['rooms.manage']}}),'AURA_SYNC_REQUIRE_IDENTITIES':'1','AURA_SYNC_REQUIRE_TLS':'1','AURA_SYNC_TLS_CERT':str(cert),'AURA_SYNC_TLS_KEY':str(key),'AURA_SYNC_CA_CERT':str(cert)})
    ctx=ssl.create_default_context(cafile=str(cert))
    def start():
        process=subprocess.Popen([str(server_bin),'--server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
            try:urllib.request.urlopen(url+'/health',context=ctx,timeout=1).close();return process
            except OSError:time.sleep(.05)
        raise AssertionError('server did not start')
    def cli(*args):return json.loads(subprocess.run([str(client),*args],env=env,capture_output=True,text=True,check=True).stdout)
    process=start()
    try:
        db=Path(tmp)/'workspace.db';store=LocalStore(db);store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026');store.save_room(0,'Lokaler Raum','Berlin',4,'Monitor')
        lock=threading.Lock();sync=lambda credential=token:sync_rooms(store,lock,lambda:True,client,url,credential,str(cert))
        assert sync()=={'sent':1,'received':0};room=cli('-action','rooms')[0];assert room['name']=='Lokaler Raum' and store.next_room_sync()['pending']==0
        process.terminate();process.wait(timeout=5);room_local=store.rooms()[0];store.save_room(room_local['id'],'Lokaler Raum','Bonn',5,'Beamer')
        try:sync();raise AssertionError('offline sync succeeded')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==1
        store.close();store.open();store.login('admin','Admin Passwort 2026');process=start();assert sync()=={'sent':1,'received':0}
        assert cli('-action','rooms')[0]['location']=='Bonn'
        central=cli('-action','create-room','-name','Zentraler Raum','-location','Hamburg','-capacity','3','-equipment','Tafel')
        assert sync()=={'sent':0,'received':1};imported=next(v for v in store.rooms() if v['name']=='Zentraler Raum');assert imported['location']=='Hamburg' and store.next_room_sync()['pending']==0
        cli('-action','update-room','-id',central['id'],'-name','Zentraler Raum','-location','Kiel','-capacity','3','-equipment','Tafel')
        assert sync()=={'sent':0,'received':1};assert next(v for v in store.rooms() if v['id']==imported['id'])['location']=='Kiel' and store.next_room_sync()['pending']==0
        store.save_room(imported['id'],'Zentraler Raum','Kiel',5,'Tafel');assert sync()=={'sent':1,'received':0}
        cli('-action','archive-room','-id',central['id']);assert sync()=={'sent':0,'received':1};assert all(v['id']!=imported['id'] for v in store.rooms()) and store.next_room_sync()['pending']==0
        store.save_room(0,'Archivraum','Berlin',2,'');archived=next(v for v in store.rooms() if v['name']=='Archivraum');store.archive_room(archived['id']);assert sync()=={'sent':2,'received':0}
        all_rooms=cli('-action','rooms','-include-archived');assert any(v['name']=='Archivraum' and not v['active'] for v in all_rooms)
        store.save_room(room_local['id'],'Lokaler Raum','Köln',6,'Beamer');pending=store.next_room_sync()['operation']
        cli('-action','sync-room-event','-payload',json.dumps(pending,ensure_ascii=False));assert store.next_room_sync()['pending']==1
        cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Extern','-capacity','6','-equipment','Beamer')
        assert sync()=={'sent':1,'received':1} and cli('-action','rooms')[0]['location']=='Extern'
        assert next(v for v in store.rooms() if v['id']==room_local['id'])['location']=='Extern'
        cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Extern Neu','-capacity','6','-equipment','Beamer')
        store.save_room(room_local['id'],'Lokaler Raum','Dresden',7,'Beamer')
        try:sync();raise AssertionError('conflicting change accepted')
        except LocalStoreError as exc:assert '409' in str(exc)
        assert store.next_room_sync()['pending']==1 and cli('-action','rooms')[0]['location']=='Extern Neu'
        try:sync(other);raise AssertionError('different server account accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==1
        inspect=lambda:inspect_room_conflict(store,lock,lambda:True,client,url,token,str(cert))
        def rebase(reviewed):return rebase_room_conflict(store,lock,lambda:True,client,url,token,str(cert),reviewed)
        reviewed=inspect();assert reviewed['preview']['local']['location']=='Dresden' and reviewed['remote']['location']=='Extern Neu'
        store.save_room(room_local['id'],'Lokaler Raum','Dresden',8,'Beamer')
        try:rebase(reviewed);raise AssertionError('stale local preview accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==2
        reviewed=inspect();cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Extern Später','-capacity','6','-equipment','Beamer')
        try:rebase(reviewed);raise AssertionError('stale server preview accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==2
        reviewed=inspect()
        with __import__('sqlite3').connect(db) as sql:sql.execute("CREATE TRIGGER fail_rebase BEFORE INSERT ON audit_log WHEN NEW.action='rooms.sync.rebased' BEGIN SELECT RAISE(FAIL,'audit denied'); END")
        try:rebase(reviewed);raise AssertionError('rebase without audit accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==2
        with __import__('sqlite3').connect(db) as sql:
            assert sql.execute('SELECT COUNT(*) FROM linux_room_sync_resolutions').fetchone()[0]==0
            sql.execute('DROP TRIGGER fail_rebase')
        rebase(reviewed);assert store.next_room_sync()['pending']==1
        cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Noch neuer','-capacity','6','-equipment','Beamer')
        try:sync();raise AssertionError('post-review server change overwritten')
        except LocalStoreError as exc:assert '409' in str(exc)
        assert store.next_room_sync()['pending']==1
        reviewed=inspect();rebase(reviewed);assert store.next_room_sync()['pending']==1
        with __import__('sqlite3').connect(db) as sql:
            history=sql.execute('SELECT previous_operations FROM linux_room_sync_resolutions').fetchall();assert len(history)==2 and len(json.loads(history[0][0]))==2
        assert sync()=={'sent':1,'received':0};room_after=next(v for v in cli('-action','rooms') if v['id']==room['id']);assert room_after['location']=='Dresden' and room_after['capacity']==8
        store.save_room(room_local['id'],'Lokaler Raum','Lokal Eins',9,'Beamer');store.save_room(room_local['id'],'Lokaler Raum','Lokal Zwei',10,'Beamer')
        store.save_room(0,'Unabhängig','Berlin',2,'')
        cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Server Eins','-capacity','6','-equipment','Beamer')
        reviewed=inspect()
        def accept(value):return accept_server_room_conflict(store,lock,lambda:True,client,url,token,str(cert),value)
        cli('-action','update-room','-id',room['id'],'-name','Lokaler Raum','-location','Server Zwei','-capacity','6','-equipment','Beamer')
        try:accept(reviewed);raise AssertionError('stale server choice accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==3 and next(v for v in store.rooms() if v['id']==room_local['id'])['location']=='Lokal Zwei'
        reviewed=inspect()
        with __import__('sqlite3').connect(db) as sql:sql.execute("CREATE TRIGGER fail_accept BEFORE INSERT ON audit_log WHEN NEW.action='rooms.sync.accepted_server' BEGIN SELECT RAISE(FAIL,'audit denied'); END")
        try:accept(reviewed);raise AssertionError('server choice without audit accepted')
        except LocalStoreError:pass
        assert store.next_room_sync()['pending']==3 and next(v for v in store.rooms() if v['id']==room_local['id'])['location']=='Lokal Zwei'
        with __import__('sqlite3').connect(db) as sql:
            assert sql.execute('SELECT suppressed FROM linux_room_sync_guard').fetchone()[0]==0;sql.execute('DROP TRIGGER fail_accept')
        accept(reviewed);assert store.next_room_sync()['pending']==1 and next(v for v in store.rooms() if v['id']==room_local['id'])['location']=='Server Zwei'
        with __import__('sqlite3').connect(db) as sql:
            history=sql.execute('SELECT local_json,remote_json,previous_operations FROM linux_room_sync_resolutions ORDER BY id DESC LIMIT 1').fetchone()
            assert json.loads(history[0])['location']=='Lokal Zwei' and json.loads(history[1])['location']=='Server Zwei' and len(json.loads(history[2]))==2
        assert sync()=={'sent':1,'received':0};assert any(v['name']=='Unabhängig' for v in cli('-action','rooms'))
        store.save_room(room_local['id'],'Lokaler Raum','Lokale Änderung',8,'Beamer');cli('-action','archive-room','-id',room['id']);reviewed=inspect();assert not reviewed['remote']['active'];accept(reviewed)
        assert store.next_room_sync()['pending']==0 and all(v['id']!=room_local['id'] for v in store.rooms())
        with __import__('sqlite3').connect(db) as sql:assert sql.execute('SELECT active FROM rooms WHERE id=?',(room_local['id'],)).fetchone()[0]==0
        assert sync()=={'sent':0,'received':0}
        # Independent clients can create the same named room before syncing.
        # The failed create is fenced before the identities are linked.
        second_db=Path(tmp)/'second.db';second=LocalStore(second_db);second.create('Zweite Admin','admin2','Admin Passwort 2026');second.open();second.login('admin2','Admin Passwort 2026')
        second.save_room(0,'Unabhängig','München',7,'Lokal');second_room=second.rooms()[0];second.book(second_room['id'],1900000000,1900003600,2);second_lock=threading.Lock()
        sync2=lambda:sync_rooms(second,second_lock,lambda:True,client,url,token,str(cert))
        inspect2=lambda:inspect_room_conflict(second,second_lock,lambda:True,client,url,token,str(cert))
        rebase2=lambda value:rebase_room_conflict(second,second_lock,lambda:True,client,url,token,str(cert),value)
        try:sync2();raise AssertionError('different room identities accepted silently')
        except LocalStoreError as exc:assert '409' in str(exc)
        review=inspect2();assert review['identityConflict'] and review['remote']['name']=='Unabhängig';original=review['operation']
        with __import__('sqlite3').connect(second_db) as sql:
            sql.execute('CREATE TABLE linux_booking_sync_shadow(booking_id INTEGER PRIMARY KEY,booking_json TEXT NOT NULL)')
            sql.execute('INSERT INTO linux_booking_sync_shadow VALUES(?,?)',(99,json.dumps({'roomKey':review['preview']['local']['id']})))
        try:rebase2(review);raise AssertionError('confirmed booking reference was ignored')
        except LocalStoreError:pass
        with __import__('sqlite3').connect(tmp+'/central.sqlite') as sql:assert sql.execute('SELECT count(*) FROM room_sync_receipts WHERE operation_id=?',(original['operationId'],)).fetchone()[0]==0
        with __import__('sqlite3').connect(second_db) as sql:
            sql.execute('DROP TABLE linux_booking_sync_shadow')
            sql.execute("CREATE TRIGGER fail_mapping BEFORE INSERT ON audit_log WHEN NEW.action='rooms.sync.identity_mapped' BEGIN SELECT RAISE(ABORT,'audit denied'); END")
        try:rebase2(review);raise AssertionError('mapping without audit accepted')
        except LocalStoreError:pass
        assert second.next_room_sync()['operation']==original
        with __import__('sqlite3').connect(second_db) as sql:sql.execute('DROP TRIGGER fail_mapping')
        process.terminate();process.wait(timeout=5);process=start()
        rebase2(review);assert second.next_room_sync()['pending']==1
        denied=subprocess.run([str(client),'-action','sync-room-event','-payload',json.dumps(original)],env=env,text=True,capture_output=True)
        assert denied.returncode!=0 and '409' in denied.stderr
        assert sync2()['sent']==1
        mapped=next(v for v in cli('-action','rooms') if v['id']==review['remote']['id']);assert mapped['location']=='München' and mapped['capacity']==7
        assert any(v['startsAt']==1900000000 for v in second.bookings(second_room['id']))
        with __import__('sqlite3').connect(second_db) as sql:
            assert sql.execute("SELECT external_id FROM sync_object_links WHERE object_type='room' AND local_id=?",(second_room['id'],)).fetchone()[0]==review['remote']['id']
            assert len(json.loads(sql.execute('SELECT previous_operations FROM linux_room_sync_resolutions ORDER BY id DESC LIMIT 1').fetchone()[0]))==1
        # The administrator can alternatively keep the server version.
        central2=cli('-action','create-room','-name','Besprechung','-location','Zentrale','-capacity','3','-equipment','Tafel')
        second.save_room(0,'Besprechung','Lokal',4,'Monitor');local2=next(v for v in second.rooms() if v['name']=='Besprechung')
        try:sync2();raise AssertionError('second collision accepted silently')
        except LocalStoreError:pass
        review2=inspect2();assert review2['identityConflict'] and review2['remote']['id']==central2['id']
        accept_server_room_conflict(second,second_lock,lambda:True,client,url,token,str(cert),review2)
        assert next(v for v in second.rooms() if v['id']==local2['id'])['location']=='Zentrale'
        assert sync2()['sent']==0
        second.close();store.close()
    finally:
        process.terminate();process.wait(timeout=5)
print('Room sync: TLS, offline queue, bidirectional changes, restart, retry, conflict resolution and account binding: OK')

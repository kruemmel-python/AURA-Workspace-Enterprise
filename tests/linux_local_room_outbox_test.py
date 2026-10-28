#!/usr/bin/python3
import importlib.util,sqlite3,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-room-outbox-') as tmp:
    db=Path(tmp)/'workspace.db';s=m.LocalStore(db);s.create('Admin','admin','Admin Passwort 2026');s.open();s.login('admin','Admin Passwort 2026')
    s.save_room(0,'Bestand','Berlin',4,'');assert s.next_room_sync() is None
    s.enable_room_sync('linux-test','https://server.example',9);first=s.next_room_sync();op=first['operation'];assert first['pending']==1 and op['room']['name']=='Bestand' and op['action']=='create' and op['expected'] is None
    room=s.rooms()[0];s.save_room(room['id'],'Bestand','Bonn',5,'Display');s.archive_room(room['id']);assert s.next_room_sync()['pending']==3
    try:s.enable_room_sync('other','https://else.example',9);raise AssertionError('destination changed')
    except m.LocalStoreError:pass
    try:s.acknowledge_room_sync('wrong');raise AssertionError('wrong ack accepted')
    except m.LocalStoreError:pass
    s.acknowledge_room_sync(op['operationId']);update=s.next_room_sync()['operation'];assert update['action']=='update' and update['expected']['location']=='Berlin' and update['room']['location']=='Bonn'
    s.acknowledge_room_sync(update['operationId']);arch=s.next_room_sync()['operation'];assert arch['action']=='archive' and arch['expected']['active'] and not arch['room']['active']
    s.close();s.open();s.login('admin','Admin Passwort 2026');assert s.next_room_sync()['operation']==arch
    with sqlite3.connect(db) as sql:sql.execute("CREATE TRIGGER abort_outbox BEFORE INSERT ON linux_room_sync_outbox BEGIN SELECT RAISE(FAIL,'outbox unavailable'); END")
    try:s.save_room(0,'Nicht speichern','X',2,'');raise AssertionError('change without outbox')
    except m.LocalStoreError:pass
    assert s.rooms()==[]
    with sqlite3.connect(db) as sql:sql.execute('DROP TRIGGER abort_outbox')
    s.acknowledge_room_sync(arch['operationId']);assert s.next_room_sync()['pending']==0
    with sqlite3.connect(db) as sql:sql.execute('DELETE FROM employee_roles')
    try:s.next_room_sync();raise AssertionError('permission bypass')
    except m.LocalStoreError:pass
    s.close()
print('Room outbox: transaction, persistent order, state snapshots, destination binding and authorization: OK')

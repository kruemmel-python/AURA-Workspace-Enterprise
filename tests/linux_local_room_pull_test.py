#!/usr/bin/python3
import importlib.util,sqlite3,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-room-pull-') as tmp:
    path=Path(tmp)/'workspace.db';s=m.LocalStore(path);s.create('Admin','admin','Admin Passwort 2026');s.open();s.login('admin','Admin Passwort 2026');s.save_room(0,'Lokal','Berlin',4,'')
    s.enable_room_sync('linux-test','https://server.example',9);first=s.next_room_sync()['operation'];base=first['room'];s.acknowledge_room_sync(first['operationId'])
    remote={'id':'remote:room:2','name':'Zentral','location':'Kiel','capacity':3,'equipment':'Monitor','active':True}
    assert s.apply_room_snapshot([base,remote])==1 and len(s.rooms())==2 and s.next_room_sync()['pending']==0
    change={**remote,'location':'Hamburg'};assert s.apply_room_snapshot([base,change])==1 and s.next_room_sync()['pending']==0
    remote_local=next(v for v in s.rooms() if v['name']=='Zentral');s.save_room(remote_local['id'],'Zentral','Bremen',3,'Monitor')
    try:s.apply_room_snapshot([base,change]);raise AssertionError('incoming overwrote pending local edit')
    except m.LocalStoreError:pass
    assert next(v for v in s.rooms() if v['name']=='Zentral')['location']=='Bremen' and s.next_room_sync()['pending']==1
    operation=s.next_room_sync()['operation'];s.acknowledge_room_sync(operation['operationId']);after=operation['room']
    assert s.apply_room_snapshot([base,after])==0
    # A legacy writer can bypass capture: a mismatch against the shadow must
    # roll back all other incoming rows, not overwrite the untracked edit.
    with sqlite3.connect(path) as db:db.execute('UPDATE linux_room_sync_guard SET suppressed=1');db.execute("UPDATE rooms SET location='Extern lokal' WHERE name='Zentral'");db.execute('UPDATE linux_room_sync_guard SET suppressed=0')
    try:s.apply_room_snapshot([{**base,'location':'Potsdam'},after]);raise AssertionError('untracked local edit overwritten')
    except m.LocalStoreError:pass
    assert next(v for v in s.rooms() if v['name']=='Lokal')['location']=='Berlin'
    assert next(v for v in s.rooms() if v['name']=='Zentral')['location']=='Extern lokal'
    # Restore expected local state and verify missing and duplicate remote IDs
    # never cause destructive deletion or partial application.
    with sqlite3.connect(path) as db:db.execute('UPDATE linux_room_sync_guard SET suppressed=1');db.execute("UPDATE rooms SET location='Bremen' WHERE name='Zentral'");db.execute('UPDATE linux_room_sync_guard SET suppressed=0')
    for values in ([base],[base,after,after]):
        try:s.apply_room_snapshot(values);raise AssertionError('invalid snapshot accepted')
        except m.LocalStoreError:pass
    assert len(s.rooms())==2 and s.next_room_sync()['pending']==0
    with sqlite3.connect(path) as db:db.execute("CREATE TRIGGER fail_pull_audit BEFORE INSERT ON audit_log WHEN NEW.action='rooms.sync.pulled' BEGIN SELECT RAISE(FAIL,'audit denied'); END")
    try:s.apply_room_snapshot([{**base,'location':'Potsdam'},after]);raise AssertionError('pull without audit accepted')
    except m.LocalStoreError:pass
    assert next(v for v in s.rooms() if v['name']=='Lokal')['location']=='Berlin'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT suppressed FROM linux_room_sync_guard').fetchone()[0]==0
        db.execute('DROP TRIGGER fail_pull_audit')
    for bad in ({**after,'location':'Test\x00unsauber'},{**after,'capacity':'3'}):
        try:s.apply_room_snapshot([base,bad]);raise AssertionError('invalid field accepted')
        except m.LocalStoreError:pass
    archived={**after,'active':False};assert s.apply_room_snapshot([base,archived])==1;assert [v['name'] for v in s.rooms()]==['Lokal']
    assert s.next_room_sync()['pending']==0;s.close()
print('Room pull: initial import, no echo, pending edits, atomic rollback, missing IDs and archive: OK')

#!/usr/bin/python3
import importlib.util
from pathlib import Path
import sqlite3
import tempfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='aura-booking-test-') as tmp:
    path=Path(tmp)/'db.sqlite'; admin=m.LocalStore(path)
    admin.create('Admin','admin','Kennwort-Test-2026');admin.open();admin.login('admin','Kennwort-Test-2026')
    admin.save_room(0,'Konferenz','Berlin',6,'')
    room=admin.rooms()[0]['id']; start=1900000000
    admin.book(room,start,start+3600,3)
    def reject(action):
        try: action()
        except m.LocalStoreError: return
        raise AssertionError('operation unexpectedly accepted')
    reject(lambda:admin.book(room,start+100,start+500,1))
    reject(lambda:admin.book(room,start+3600,start+7200,7))
    admin.book(room,start+3600,start+7200,1)  # Adjacent times are allowed.
    admin.book(room,start+7*86400,start+7*86400+3600,1)
    before=admin.bookings(room)
    reject(lambda:admin.book(room,start+4000,start+4100,1,3,7))
    assert admin.bookings(room)==before  # Entire conflicting series rolled back.
    original=next(x for x in before if x['startsAt']==start)
    admin.reschedule(room,original['id'],start+8000,start+9000,2)
    assert any(x['id']==original['id'] and x['startsAt']==start+8000 for x in admin.bookings(room))
    admin.block_room(room,start+12000,start+13000,'Wartung')
    reject(lambda:admin.book(room,start+12500,start+12600,1))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO employees(name,role,department,presence,active) VALUES('Mitarbeiter','Mitarbeiter','IT','office',1)")
        uid=db.execute("SELECT id FROM employees WHERE name='Mitarbeiter'").fetchone()[0]
        db.execute("INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,must_change_password) SELECT ?, 'employee',password_salt,password_digest,1,0 FROM accounts WHERE username='admin'",(uid,))
        db.execute("INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code='employee'",(uid,))
    other=m.LocalStore(path);other.open();other.login('employee','Kennwort-Test-2026')
    reject(lambda:other.cancel_booking(room,original['id']))
    reject(lambda:other.reschedule(room,original['id'],start+14000,start+15000,1))
    reject(lambda:other.block_room(room,start+16000,start+17000,'Nicht erlaubt'))
    other.book(room,start+18000,start+19000,1)
    own=next(x for x in other.bookings(room) if x['startsAt']==start+18000)
    other.cancel_booking(room,own['id'])
    admin.cancel_booking(room,original['id'])
    assert next(x for x in admin.bookings(room) if x['id']==original['id'])['status']=='cancelled'
    other.close();admin.close()
    admin.open();admin.login('admin','Kennwort-Test-2026')
    assert any(x['status']=='blocked' for x in admin.bookings(room))
    assert any(x['id']==own['id'] and x['status']=='cancelled' for x in admin.bookings(room))
    admin.close()
print('Local booking: conflicts, capacity, series rollback, reschedule, roles, block, cancel and persistence: OK')

"""Local authenticated access to the shared C++ enterprise database."""
import ctypes
import functools
import threading
import hashlib
import hmac
import os
import re
import stat
import sqlite3
import uuid
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / 'KR_BUILD/linux-x64/libaura_linux_local.so'

class LocalStoreError(Exception):
    pass

def _serialize_store_methods(cls):
    # ctypes releases the GIL during native calls. Session responses and errors
    # share mutable native buffers, so each complete Python operation (including
    # decoding and error handling) must use the same per-session reentrant lock.
    def wrap(method):
        @functools.wraps(method)
        def guarded(self,*args,**kwargs):
            with self._operation_lock:
                return method(self,*args,**kwargs)
        return guarded
    for name,method in tuple(vars(cls).items()):
        if not name.startswith('_') and callable(method) and not isinstance(method,(staticmethod,classmethod)):
            setattr(cls,name,wrap(method))
    return cls


@_serialize_store_methods
class LocalStore:
    def __init__(self, path):
        self._operation_lock=threading.RLock()
        if not LIBRARY.is_file():
            raise LocalStoreError('Lokale Bibliothek fehlt. Bitte clients/linux/build.sh ausführen.')
        self.lib=ctypes.CDLL(str(LIBRARY))
        p=ctypes.c_void_p; text=ctypes.c_char_p; i64=ctypes.c_int64; integer=ctypes.c_int
        signatures={
            'aura_local_create':([text,text,text,text],integer),
            'aura_local_open':([text],p), 'aura_local_close':([p],None),
            'aura_local_error':([p],text), 'aura_local_login':([p,text,text],integer), 'aura_local_session_employee':([p],i64), 'aura_local_identity':([p],text),
            'aura_local_change_password':([p,text,text],integer),
            'aura_local_room_sync_enable':([p,text,text,i64],integer), 'aura_local_room_sync_next':([p],text), 'aura_local_room_sync_ack':([p,text],integer), 'aura_local_room_sync_apply':([p,text],integer),
            'aura_local_room_sync_conflict':([p],text), 'aura_local_room_sync_rebase':([p,text,i64,text,text,text,i64],integer),
            'aura_local_room_sync_resolve':([p,text,i64,text,text,text,i64,integer],integer),
            'aura_local_rooms':([p],text), 'aura_local_rooms_import':([p,text],integer), 'aura_local_rooms_export':([p,text],integer),
            'aura_local_room_save':([p,i64,text,text,integer,text],integer),
            'aura_local_room_archive':([p,i64],integer),
            'aura_local_tasks':([p],text), 'aura_local_projects':([p],text), 'aura_local_task_create_in_project':([p,text,text,text,i64,i64,integer,i64],integer),
            'aura_local_tickets':([p],text), 'aura_local_ticket_save':([p,i64,text,text,text,i64],integer),
            'aura_local_employees':([p],text), 'aura_local_employee_save':([p,i64,text,text,text],integer), 'aura_local_employee_save_with_role':([p,i64,text,text,text,text],integer),
            'aura_local_organizations':([p],text), 'aura_local_organization_save':([p,i64,text,text,i64],integer),
            'aura_local_organization_archive':([p,i64],integer), 'aura_local_approvals':([p],text),
            'aura_local_approval_create':([p,i64,i64],integer), 'aura_local_approval_decide':([p,i64,integer],integer),
            'aura_local_roles':([p],text), 'aura_local_assign_role':([p,i64,text],integer),
            'aura_local_role_save':([p,i64,text,text,text,text],integer), 'aura_local_role_archive':([p,i64],integer),
            'aura_local_audit':([p,integer],text),
            'aura_local_backup':([p,text],integer), 'aura_local_restore':([p,text],text),
            'aura_local_accounts':([p],text), 'aura_local_account_create':([p,i64,text,text],integer),
            'aura_local_account_reset':([p,i64,text],integer), 'aura_local_account_enable':([p,i64,integer],integer),
            'aura_local_employee_presence':([p,i64,text],integer), 'aura_local_employee_archive':([p,i64],integer),
            'aura_local_leave':([p],text), 'aura_local_leave_create':([p,i64,i64,text],integer),
            'aura_local_leave_decide':([p,i64,integer],integer), 'aura_local_leave_decide_compared':([p,i64,integer,i64,i64,i64,text,i64],integer),
            'aura_local_assets':([p],text), 'aura_local_asset_save':([p,i64,text,text,text,text],integer),
            'aura_local_asset_archive':([p,i64],integer), 'aura_local_maintenance':([p,i64],text),
            'aura_local_maintenance_save':([p,i64,i64,text,text,i64,integer],integer),
            'aura_local_maintenance_status':([p,i64,i64,text],integer), 'aura_local_maintenance_archive':([p,i64,i64],integer),
            'aura_local_ticket_status':([p,i64,text],integer), 'aura_local_ticket_archive':([p,i64],integer),
            'aura_local_comments':([p,text,i64],text), 'aura_local_comment_add':([p,text,i64,text],integer),
            'aura_local_attachments':([p,text,i64],text),
            'aura_local_attachments_all':([p,text,i64],text),
            'aura_local_attachment_create':([p,text,i64,text,text,i64,text],integer),
            'aura_local_attachment_archive':([p,text,i64,i64],integer),
            'aura_local_attachment_restore':([p,text,i64,i64],integer),
            'aura_local_task_save':([p,i64,text,text,text,i64,i64,integer],integer), 'aura_local_task_update_project':([p,i64,text,text,text,i64,i64,integer,i64,i64],integer),
            'aura_local_task_status':([p,i64,text],integer), 'aura_local_task_archive':([p,i64],integer),
            'aura_local_task_project':([p,i64,i64],integer),
            'aura_local_task_project_edge':([p,i64,i64,integer,integer],integer),
            'aura_local_projects_for_assignment':([p],text),
            'aura_local_dependencies':([p,i64],text), 'aura_local_dependency_change':([p,i64,i64,integer],integer),
            'aura_local_subtasks':([p,i64],text), 'aura_local_subtask_save':([p,i64,i64,text,text,i64,i64],integer), 'aura_local_subtask_save_compared':([p,i64,i64,text,text,i64,i64,text,text,i64,i64],integer),
            'aura_local_subtask_archive':([p,i64,i64],integer), 'aura_local_subtask_archive_compared':([p,i64,i64,text,text,i64,i64],integer),
            'aura_local_project_save':([p,i64,text,text,text,text,i64],integer),
            'aura_local_project_archive':([p,i64],integer),
            'aura_local_project_status':([p,i64,text,text],integer),
            'aura_local_bookings':([p,i64],text),
            'aura_local_book':([p,i64,i64,i64,integer,integer,integer],integer),
            'aura_local_reschedule':([p,i64,i64,i64,i64,integer],integer),
            'aura_local_cancel_booking':([p,i64,i64],integer),
            'aura_local_block_room':([p,i64,i64,i64,text],integer),
        }
        for name,(args,result) in signatures.items():
            fn=getattr(self.lib,name); fn.argtypes=args; fn.restype=result
        self.path=Path(path); self.handle=None

    @staticmethod
    def b(value): return str(value).encode('utf-8')
    def create(self,name,username,password):
        if not self.lib.aura_local_create(self.b(self.path),self.b(name),self.b(username),self.b(password)):
            raise LocalStoreError('Lokale Datenbank konnte nicht angelegt werden. Prüfe Name, Passwort (mindestens 12 Zeichen), Dateirechte und ob die Datei bereits existiert.')
    def open(self):
        self.close(); self.handle=self.lib.aura_local_open(self.b(self.path))
        if not self.handle: raise LocalStoreError('Lokale Datenbank konnte nicht geöffnet werden.')
    def close(self):
        if self.handle: self.lib.aura_local_close(self.handle); self.handle=None
    def error(self):
        raw=self.lib.aura_local_error(self.handle)
        return raw.decode('utf-8',errors='replace') if raw else 'Datenbankfehler'
    def login(self,username,password):
        result=self.lib.aura_local_login(self.handle,self.b(username),self.b(password))
        if not result: raise LocalStoreError(self.error())
        return result == 2
    def session_employee(self):
        employee=self.lib.aura_local_session_employee(self.handle)
        if not employee:raise LocalStoreError(self.error())
        return employee
    def identity(self):
        data=self.lib.aura_local_identity(self.handle)
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def change_password(self,old,new):
        if not self.lib.aura_local_change_password(self.handle,self.b(old),self.b(new)):
            raise LocalStoreError(self.error())
    def rooms(self):
        data=self.lib.aura_local_rooms(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_room(self,id,name,location,capacity,equipment):
        if not self.lib.aura_local_room_save(self.handle,int(id),self.b(name),self.b(location),int(capacity),self.b(equipment)):
            raise LocalStoreError(self.error())
    def archive_room(self,id):
        if not self.lib.aura_local_room_archive(self.handle,int(id)):
            raise LocalStoreError(self.error())

    def bookings(self,room_id):
        data=self.lib.aura_local_bookings(self.handle,int(room_id))
        if data is None: raise LocalStoreError(self.error())
        records=json.loads(data.decode('utf-8'))
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            reasons=dict(db.execute("SELECT d.booking_id,d.reason FROM booking_block_details d JOIN bookings b ON b.id=d.booking_id WHERE b.room_id=?",(int(room_id),)))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_booking_sync_links'").fetchone():
                reasons.update(dict(db.execute("SELECT l.booking_id,l.reason FROM linux_booking_sync_links l JOIN bookings b ON b.id=l.booking_id WHERE b.room_id=? AND l.reason<>''",(int(room_id),))))
                links={r[0]:(r[1],r[2]) for r in db.execute("SELECT l.booking_id,l.remote_client,l.remote_id FROM linux_booking_sync_links l JOIN bookings b ON b.id=l.booking_id WHERE b.room_id=?",(int(room_id),))}
            else:links={}
            for item in records:
                if item['id'] in reasons:item['reason']=reasons[item['id']]
                if item['id'] in links:item['remoteClient'],item['remoteBookingId']=links[item['id']]
        return records
    def book(self,room_id,start,end,attendees,occurrences=1,interval_days=7):
        if not self.lib.aura_local_book(self.handle,int(room_id),int(start),int(end),int(attendees),int(occurrences),int(interval_days)):
            raise LocalStoreError(self.error())
    def reschedule(self,room_id,booking_id,start,end,attendees):
        if not self.lib.aura_local_reschedule(self.handle,int(room_id),int(booking_id),int(start),int(end),int(attendees)):
            raise LocalStoreError(self.error())
    def cancel_booking(self,room_id,booking_id):
        if not self.lib.aura_local_cancel_booking(self.handle,int(room_id),int(booking_id)):
            raise LocalStoreError(self.error())
    def block_room(self,room_id,start,end,reason):
        if not self.lib.aura_local_block_room(self.handle,int(room_id),int(start),int(end),self.b(reason)):
            raise LocalStoreError(self.error())

    def tasks(self):
        data=self.lib.aura_local_tasks(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def projects(self):
        data=self.lib.aura_local_projects(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def projects_for_assignment(self):
        data=self.lib.aura_local_projects_for_assignment(self.handle)
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_task(self,id,title,department,priority,assignee,due=0,recurrence=0):
        if not self.lib.aura_local_task_save(self.handle,int(id),self.b(title),self.b(department),self.b(priority),int(assignee),int(due),int(recurrence)):
            raise LocalStoreError(self.error())
    def create_task_in_project(self,title,department,priority,assignee,due,recurrence,project):
        if not self.lib.aura_local_task_create_in_project(self.handle,self.b(title),self.b(department),self.b(priority),int(assignee),int(due),int(recurrence),int(project)):raise LocalStoreError(self.error())
    def update_task_project(self,id,title,department,priority,assignee,due,recurrence,expected,project):
        if not self.lib.aura_local_task_update_project(self.handle,int(id),self.b(title),self.b(department),self.b(priority),int(assignee),int(due),int(recurrence),int(expected),int(project)):raise LocalStoreError(self.error())
    def task_status(self,id,status):
        if not self.lib.aura_local_task_status(self.handle,int(id),self.b(status)): raise LocalStoreError(self.error())
    def archive_task(self,id):
        if not self.lib.aura_local_task_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def assign_project(self,task_id,project_id):
        if not self.lib.aura_local_task_project(self.handle,int(task_id),int(project_id)): raise LocalStoreError(self.error())
    def set_project_edge(self,task_id,project_id,expected,present):
        if not self.lib.aura_local_task_project_edge(self.handle,int(task_id),int(project_id),int(expected),int(present)):raise LocalStoreError(self.error())
    def save_project(self,id,name,department,description,status,owner):
        if not self.lib.aura_local_project_save(self.handle,int(id),self.b(name),self.b(department),self.b(description),self.b(status),int(owner)):
            raise LocalStoreError(self.error())
    def archive_project(self,id):
        if not self.lib.aura_local_project_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def project_status(self,id,expected,status):
        if not self.lib.aura_local_project_status(self.handle,int(id),self.b(expected),self.b(status)): raise LocalStoreError(self.error())

    def dependencies(self,task_id):
        data=self.lib.aura_local_dependencies(self.handle,int(task_id))
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def dependency_change(self,task_id,dependency_id,add):
        if not self.lib.aura_local_dependency_change(self.handle,int(task_id),int(dependency_id),int(add)):
            raise LocalStoreError(self.error())
    def subtasks(self,task_id):
        data=self.lib.aura_local_subtasks(self.handle,int(task_id))
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_subtask(self,task_id,id,title,status,assignee,due=0):
        if not self.lib.aura_local_subtask_save(self.handle,int(task_id),int(id),self.b(title),self.b(status),int(assignee),int(due)):
            raise LocalStoreError(self.error())
    def save_subtask_compared(self,task_id,id,title,status,assignee,due,expected):
        if not self.lib.aura_local_subtask_save_compared(self.handle,int(task_id),int(id),self.b(title),self.b(status),int(assignee),int(due),self.b(expected['title']),self.b(expected['status']),int(expected['assigneeId']),int(expected['dueAt'])):raise LocalStoreError(self.error())
    def archive_subtask(self,task_id,id):
        if not self.lib.aura_local_subtask_archive(self.handle,int(task_id),int(id)):
            raise LocalStoreError(self.error())

    def archive_subtask_compared(self,task_id,id,expected):
        if not self.lib.aura_local_subtask_archive_compared(self.handle,int(task_id),int(id),self.b(expected['title']),self.b(expected['status']),int(expected['assigneeId']),int(expected['dueAt'])): raise LocalStoreError(self.error())

    def tickets(self):
        data=self.lib.aura_local_tickets(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_ticket(self,id,title,category,priority,assignee):
        if not self.lib.aura_local_ticket_save(self.handle,int(id),self.b(title),self.b(category),self.b(priority),int(assignee)):
            raise LocalStoreError(self.error())
    def ticket_status(self,id,status):
        if not self.lib.aura_local_ticket_status(self.handle,int(id),self.b(status)): raise LocalStoreError(self.error())
    def archive_ticket(self,id):
        if not self.lib.aura_local_ticket_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def comments(self,subject,id):
        data=self.lib.aura_local_comments(self.handle,self.b(subject),int(id))
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def add_comment(self,subject,id,body):
        if not self.lib.aura_local_comment_add(self.handle,self.b(subject),int(id),self.b(body)):
            raise LocalStoreError(self.error())

    def assets(self):
        data=self.lib.aura_local_assets(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_asset(self,id,name,category,location,status):
        if not self.lib.aura_local_asset_save(self.handle,int(id),self.b(name),self.b(category),self.b(location),self.b(status)): raise LocalStoreError(self.error())
    def archive_asset(self,id):
        if not self.lib.aura_local_asset_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def maintenance(self,asset_id):
        data=self.lib.aura_local_maintenance(self.handle,int(asset_id))
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_maintenance(self,asset_id,id,title,status,due,recurrence):
        if not self.lib.aura_local_maintenance_save(self.handle,int(asset_id),int(id),self.b(title),self.b(status),int(due),int(recurrence)): raise LocalStoreError(self.error())
    def maintenance_status(self,asset_id,id,status):
        if not self.lib.aura_local_maintenance_status(self.handle,int(asset_id),int(id),self.b(status)): raise LocalStoreError(self.error())
    def archive_maintenance(self,asset_id,id):
        if not self.lib.aura_local_maintenance_archive(self.handle,int(asset_id),int(id)): raise LocalStoreError(self.error())

    def employees(self):
        data=self.lib.aura_local_employees(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_employee(self,id,name,role,department):
        if not self.lib.aura_local_employee_save(self.handle,int(id),self.b(name),self.b(role),self.b(department)): raise LocalStoreError(self.error())
    def save_employee_with_role(self,id,name,role,department,system_role):
        if not self.lib.aura_local_employee_save_with_role(self.handle,int(id),self.b(name),self.b(role),self.b(department),self.b(system_role)): raise LocalStoreError(self.error())
    def employee_presence(self,id,presence):
        if not self.lib.aura_local_employee_presence(self.handle,int(id),self.b(presence)): raise LocalStoreError(self.error())
    def archive_employee(self,id):
        if not self.lib.aura_local_employee_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def leave(self):
        data=self.lib.aura_local_leave(self.handle)
        if data is None: raise LocalStoreError(self.error())
        entries=json.loads(data.decode('utf-8'))
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('linux_leave_sync_config','linux_leave_sync_outbox','linux_leave_sync_shadow')")}
                if len(tables)==3:
                    for entry in entries:
                        pending=db.execute('SELECT 1 FROM linux_leave_sync_outbox WHERE leave_id=? LIMIT 1',(entry['id'],)).fetchone()
                        shadow=db.execute('SELECT 1 FROM linux_leave_sync_shadow WHERE leave_id=?',(entry['id'],)).fetchone()
                        entry['syncState']='pending' if pending else 'confirmed' if shadow else 'unknown'
                else:
                    for entry in entries:entry['syncState']='unconfigured'
        except sqlite3.Error as exc:raise LocalStoreError('Urlaubsstatus konnte nicht gelesen werden: '+str(exc)) from exc
        return entries
    def create_leave(self,start,end,reason):
        if not self.lib.aura_local_leave_create(self.handle,int(start),int(end),self.b(reason)): raise LocalStoreError(self.error())
    def decide_leave(self,id,approved):
        if not self.lib.aura_local_leave_decide(self.handle,int(id),int(approved)): raise LocalStoreError(self.error())

    def decide_leave_compared(self,id,approved,expected):
        if not self.lib.aura_local_leave_decide_compared(self.handle,int(id),int(approved),int(expected['employeeId']),int(expected['startsAt']),int(expected['endsAt']),self.b(expected['reason']),int(expected['decidedBy'])): raise LocalStoreError(self.error())

    def roles(self):
        data=self.lib.aura_local_roles(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def assign_role(self,employee_id,code):
        if not self.lib.aura_local_assign_role(self.handle,int(employee_id),self.b(code)): raise LocalStoreError(self.error())
    def accounts(self):
        data=self.lib.aura_local_accounts(self.handle)
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def create_account(self,employee_id,username,password):
        if not self.lib.aura_local_account_create(self.handle,int(employee_id),self.b(username),self.b(password)): raise LocalStoreError(self.error())
    def reset_account(self,employee_id,password):
        if not self.lib.aura_local_account_reset(self.handle,int(employee_id),self.b(password)): raise LocalStoreError(self.error())
    def enable_account(self,employee_id,enabled):
        if not self.lib.aura_local_account_enable(self.handle,int(employee_id),int(enabled)): raise LocalStoreError(self.error())

    def save_role(self,id,code,name,description,permissions):
        if not self.lib.aura_local_role_save(self.handle,int(id),self.b(code),self.b(name),self.b(description),self.b(permissions)):
            raise LocalStoreError(self.error())
    def archive_role(self,id):
        if not self.lib.aura_local_role_archive(self.handle,int(id)): raise LocalStoreError(self.error())
    def audit(self,limit=200):
        data=self.lib.aura_local_audit(self.handle,int(limit))
        if data is None: raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))

    def employee_resolution_history(self):
        # Recheck the same administrative permission used by the audit view.
        self.audit(1)
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_employee_sync_resolutions'").fetchone():return []
                rows=db.execute('SELECT id,employee_id,choice,local_json,remote_json,previous_operations,created_at FROM linux_employee_sync_resolutions ORDER BY id DESC LIMIT 200').fetchall()
            return [dict(zip(('id','employee_id','choice','local','remote','operations','created_at'),row)) for row in rows]
        except sqlite3.Error as exc:raise LocalStoreError('Mitarbeiterentscheidungen konnten nicht geladen werden: '+str(exc)) from exc

    def room_resolution_history(self):
        self.audit(1)
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_room_sync_resolutions'").fetchone():return []
                details=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_room_sync_resolution_details'").fetchone()
                join=' LEFT JOIN linux_room_sync_resolution_details d ON d.resolution_id=r.id' if details else ''
                columns='d.choice,d.actor_employee_id,d.identity_mapping' if details else 'NULL,NULL,NULL'
                rows=db.execute('SELECT r.id,r.room_id,r.actor,r.local_json,r.remote_json,r.previous_operations,r.created_at,'+columns+' FROM linux_room_sync_resolutions r'+join+' ORDER BY r.id DESC LIMIT 200').fetchall()
            return [dict(zip(('id','room_id','actor','local','remote','operations','created_at','choice','actor_employee_id','identity_mapping'),row)) for row in rows]
        except sqlite3.Error as exc:raise LocalStoreError('Raumentscheidungen konnten nicht geladen werden: '+str(exc)) from exc

    def sync_resolution_history(self):
        # History contains business data; require the same permission as audit.
        self.audit(1)
        definitions=(
            ('Aufgabe','linux_task_sync_resolutions',"SELECT id,created_at,'Aufgabe '||task_id,choice,local_json,remote_json,previous_operations FROM linux_task_sync_resolutions ORDER BY id DESC LIMIT 200"),
            ('Voraussetzung','linux_dependency_sync_conflicts',"SELECT id,created_at,'Aufgabe '||task_id||' ← '||dependency_id,choice,CAST(local_present AS TEXT),remote_json,previous_operations FROM linux_dependency_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Projektzuordnung','linux_project_assignment_sync_conflicts',"SELECT id,created_at,'Projekt '||project_id||' / Aufgabe '||task_id,choice,CAST(local_present AS TEXT),CAST(remote_present AS TEXT),previous_operations FROM linux_project_assignment_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Unteraufgabe','linux_subtask_sync_conflicts',"SELECT id,created_at,'Unteraufgabe '||subtask_id,choice,local_json,remote_json,previous_operations FROM linux_subtask_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Ticket','linux_ticket_sync_conflicts',"SELECT id,created_at,'Ticket '||ticket_id,choice,local_json,remote_json,previous_operations FROM linux_ticket_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Organisation','linux_organization_sync_conflicts',"SELECT id,created_at,'Organisation '||organization_id,choice,local_json,remote_json,previous_operations FROM linux_organization_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Anlage','linux_facility_asset_sync_conflicts',"SELECT id,created_at,'Anlage '||asset_id,choice,local_json,remote_json,previous_operations FROM linux_facility_asset_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Projekt','linux_project_sync_conflicts',"SELECT id,created_at,'Projekt '||project_id,choice,local_json,remote_json,previous_operations FROM linux_project_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Wartung','linux_maintenance_sync_conflicts',"SELECT id,created_at,'Wartung '||maintenance_id,choice,local_json,remote_json,previous_operations FROM linux_maintenance_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Aufgabenanhang','linux_task_attachment_sync_conflicts',"SELECT id,created_at,'Anhang '||attachment_id,choice,local_json,remote_json,previous_operations FROM linux_task_attachment_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Ticketanhang','linux_ticket_attachment_sync_conflicts',"SELECT id,created_at,'Anhang '||attachment_id,choice,local_json,remote_json,previous_operations FROM linux_ticket_attachment_sync_conflicts ORDER BY id DESC LIMIT 200"),
            ('Wartungsfolge','linux_maintenance_successor_links',"SELECT id,created_at,'Wartung '||parent_id||' / Folge '||local_id,choice,local_json,server_json,previous_operations FROM linux_maintenance_successor_links ORDER BY id DESC LIMIT 200"),
            ('Freigabe','linux_approval_sync_resolutions',"SELECT id,created_at,'Freigabe '||approval_id||' · Mitarbeiter '||actor_id,'server',local_json,remote_json,previous_operations FROM linux_approval_sync_resolutions ORDER BY id DESC LIMIT 200"),
            ('Buchung','linux_booking_sync_resolutions',"SELECT id,created_at,'Buchung '||booking_id||' · Mitarbeiter '||actor_id,'server',local_json,conflicts_json,previous_operations FROM linux_booking_sync_resolutions ORDER BY id DESC LIMIT 200"),
            ('Urlaub','linux_leave_sync_resolutions',"SELECT id,created_at,'Urlaubsantrag '||leave_id||' · Mitarbeiter '||actor_id,'server',local_json,remote_json,previous_operations FROM linux_leave_sync_resolutions ORDER BY id DESC LIMIT 200"),
        )
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                result=[]
                for kind,table,query in definitions:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                        for row in db.execute(query):
                            result.append(dict(zip(('id','created_at','subject','choice','local','remote','operations'),row),kind=kind))
                return result
        except sqlite3.Error as exc:raise LocalStoreError('Synchronisationsentscheidungen konnten nicht geladen werden: '+str(exc)) from exc

    def organizations(self):
        data=self.lib.aura_local_organizations(self.handle)
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def save_organization(self,id,name,code,manager):
        if not self.lib.aura_local_organization_save(self.handle,int(id),self.b(name),self.b(code),int(manager)):raise LocalStoreError(self.error())
    def archive_organization(self,id):
        if not self.lib.aura_local_organization_archive(self.handle,int(id)):raise LocalStoreError(self.error())
    def approvals(self):
        data=self.lib.aura_local_approvals(self.handle)
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def create_approval(self,task_id,approver_id):
        if not self.lib.aura_local_approval_create(self.handle,int(task_id),int(approver_id)):raise LocalStoreError(self.error())
    def decide_approval(self,id,approved):
        if not self.lib.aura_local_approval_decide(self.handle,int(id),int(approved)):raise LocalStoreError(self.error())

    def attachments(self,subject,id,include_archived=False):
        function=self.lib.aura_local_attachments_all if include_archived else self.lib.aura_local_attachments
        data=function(self.handle,self.b(subject),int(id))
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def import_attachment(self,subject,id,source):
        if subject not in ('task','ticket') or int(id)<=0:raise LocalStoreError('Ungültiger Anhangsbezug.')
        # Check write permission before accessing or copying the chosen file;
        # the native write rechecks it immediately before committing.
        identity=self.identity()
        manage=identity.get('canManageTasks' if subject=='task' else 'canManageTickets')
        read=identity.get('canReadTasks' if subject=='task' else 'canReadTickets')
        if not manage and not (read and identity.get('canCollaborate')):raise LocalStoreError('Für diese Aktion fehlt die Berechtigung.')
        self.attachments(subject,id)
        source=Path(source);fd=None;target=None
        try:
            fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>100*1024*1024:raise LocalStoreError('Nur Dateien bis 100 MB sind zulässig.')
            base=self.path.parent.resolve(strict=True)
            directory=base
            for segment in ('attachments',subject,str(int(id))):
                directory=directory/segment
                directory.mkdir(mode=0o700,exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():raise LocalStoreError('Ungültiger Anhangsordner.')
            extension=source.suffix
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,16}',extension):extension='.bin'
            candidate=directory/(uuid.uuid4().hex+extension)
            digest=hashlib.sha256();size=0
            with os.fdopen(fd,'rb') as inp:
                fd=None
                out_fd=os.open(candidate,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                target=candidate
                with os.fdopen(out_fd,'wb') as out:
                    while True:
                        chunk=inp.read(1024*1024)
                        if not chunk:break
                        size+=len(chunk)
                        if size>100*1024*1024:raise LocalStoreError('Datei überschreitet 100 MB.')
                        digest.update(chunk);out.write(chunk)
            target=target.resolve(strict=True)
            if not self.lib.aura_local_attachment_create(self.handle,self.b(subject),int(id),self.b(source.name),self.b(target),size,self.b(digest.hexdigest())):
                raise LocalStoreError(self.error())
        except (OSError,ValueError) as exc:
            if target is not None:target.unlink(missing_ok=True)
            raise LocalStoreError('Anhang konnte nicht importiert werden: '+str(exc)) from exc
        except Exception:
            if target is not None:target.unlink(missing_ok=True)
            raise
        finally:
            if fd is not None:os.close(fd)
    def attachment_path(self,subject,id,stored_path):
        if subject not in ('task','ticket') or int(id)<=0:raise LocalStoreError('Ungültiger Anhangsbezug.')
        base=self.path.parent.resolve(strict=True);expected=base
        for segment in ('attachments',subject,str(int(id))):
            expected=expected/segment
            if expected.is_symlink() or not expected.is_dir():raise LocalStoreError('Ungültiger Anhangsordner.')
        original=Path(stored_path)
        if len(original.parts)<4 or tuple(original.parts[-4:-1])!=('attachments',subject,str(int(id))) or original.name in ('','.','..'):
            raise LocalStoreError('Ungültiger gespeicherter Anhangspfad.')
        # The database can be restored at a different absolute location. Only
        # the filename is taken from the old path; the file must still reside
        # in this workspace and pass the caller's size and digest checks.
        return expected/original.name

    def verify_attachment(self,subject,id,attachment_id,include_archived=False):
        item=next((a for a in self.attachments(subject,id,include_archived) if a['id']==int(attachment_id)),None)
        if item is None:raise LocalStoreError('Anhang nicht gefunden oder archiviert.')
        try:
            target=self.attachment_path(subject,id,item['storedPath'])
            fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as inp:
                info=os.fstat(inp.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size!=item['sizeBytes'] or info.st_size>100*1024*1024:raise LocalStoreError('Anhang hat eine ungültige Größe.')
                digest=hashlib.sha256()
                while True:
                    data=inp.read(1024*1024)
                    if not data:break
                    digest.update(data)
                if not hmac.compare_digest(digest.hexdigest(),item['sha256']):raise LocalStoreError('Integritätsprüfung des Anhangs fehlgeschlagen.')
            return str(target)
        except OSError as exc:raise LocalStoreError('Anhang ist nicht verfügbar: '+str(exc)) from exc
    def archive_attachment(self,subject,id,attachment_id):
        if not self.lib.aura_local_attachment_archive(self.handle,self.b(subject),int(id),int(attachment_id)):raise LocalStoreError(self.error())
    def restore_attachment(self,subject,id,attachment_id):
        self.verify_attachment(subject,id,attachment_id,True)
        if not self.lib.aura_local_attachment_restore(self.handle,self.b(subject),int(id),int(attachment_id)):raise LocalStoreError(self.error())

    def backup(self,destination):
        if not self.lib.aura_local_backup(self.handle,self.b(destination)):raise LocalStoreError(self.error())

    def import_rooms(self,path):
        count=self.lib.aura_local_rooms_import(self.handle,self.b(path))
        if count<0:raise LocalStoreError(self.error())
        return count
    def export_rooms(self,path):
        if not self.lib.aura_local_rooms_export(self.handle,self.b(path)):raise LocalStoreError(self.error())

    def restore(self,source):
        result=self.lib.aura_local_restore(self.handle,self.b(source))
        if result is None:raise LocalStoreError(self.error())
        return result.decode('utf-8')

    def enable_room_sync(self,client,url,actor):
        if not self.lib.aura_local_room_sync_enable(self.handle,self.b(client),self.b(url),int(actor)):raise LocalStoreError(self.error())
    def next_room_sync(self):
        data=self.lib.aura_local_room_sync_next(self.handle)
        if data is None:raise LocalStoreError(self.error())
        return json.loads(data.decode('utf-8'))
    def acknowledge_room_sync(self,operation):
        if not self.lib.aura_local_room_sync_ack(self.handle,self.b(operation)):raise LocalStoreError(self.error())

    def apply_room_snapshot(self,rooms):
        payload=json.dumps(rooms,ensure_ascii=False,separators=(',',':'))
        result=self.lib.aura_local_room_sync_apply(self.handle,self.b(payload))
        if result<0:raise LocalStoreError(self.error())
        return result

    def room_sync_conflict(self):
        result=self.lib.aura_local_room_sync_conflict(self.handle)
        if result is None:raise LocalStoreError(self.error())
        return json.loads(result.decode('utf-8'))
    def rebase_room_sync(self,preview,remote):
        encode=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':'))
        if not self.lib.aura_local_room_sync_rebase(self.handle,self.b(preview['operationId']),int(preview['lastSeq']),self.b(encode(preview['local'])),self.b(encode(remote)),self.b(preview['serverUrl']),int(preview['actor'])):raise LocalStoreError(self.error())

    def accept_server_room(self,preview,remote):
        encode=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':'))
        if not self.lib.aura_local_room_sync_resolve(self.handle,self.b(preview['operationId']),int(preview['lastSeq']),self.b(encode(preview['local'])),self.b(encode(remote)),self.b(preview['serverUrl']),int(preview['actor']),1):raise LocalStoreError(self.error())

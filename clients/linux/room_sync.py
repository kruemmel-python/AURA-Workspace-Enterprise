"""Explicit room synchronization with a durable local outbox."""
import json
import os
import re
import subprocess
import sqlite3
import uuid
from urllib.parse import urlsplit
from local_store import LocalStoreError


class RoomSyncError(LocalStoreError):
    def __init__(self,message,retryable=False):
        super().__init__(message);self.retryable=retryable


def connect_sync_server(client, url, token, ca, capability_name, permission_name, permission_label):
    url=url.strip().rstrip('/')
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalStoreError('Bitte eine gültige Server-URL ohne Zugangsdaten, Abfrage oder Fragment eingeben.')
    if not token:raise LocalStoreError('Bitte ein Server-Token eingeben.')
    if not client.is_file():raise LocalStoreError('Linux-Client fehlt. Bitte clients/linux/build.sh ausführen.')
    env=os.environ.copy();env.update({'AURA_SYNC_URL':url,'AURA_SYNC_TOKEN':token})
    if ca:env['AURA_SYNC_CA_CERT']=ca
    else:env.pop('AURA_SYNC_CA_CERT',None)
    def command(*args):
        try:
            result=subprocess.run([str(client),*args],env=env,capture_output=True,text=True,timeout=120)
        except (OSError,subprocess.TimeoutExpired) as exc:raise RoomSyncError(str(exc),retryable=True) from exc
        if result.returncode:
            message=result.stderr.strip() or 'Serveranfrage fehlgeschlagen.'
            temporary=bool(re.search(r'\b(?:500|502|503|504)\b|dial tcp|connection refused|connection reset|network is unreachable|no such host|i/o timeout|context deadline exceeded|TLS handshake timeout',message,re.IGNORECASE))
            raise RoomSyncError(message,retryable=temporary)
        try:return json.loads(result.stdout)
        except ValueError as exc:raise LocalStoreError('Ungültige Antwort des Linux-Clients.') from exc
    capability=command('-action','capabilities')
    if capability.get(capability_name)!=1:raise LocalStoreError('Der Server unterstützt diese Synchronisation nicht.')
    identity=command('-action','identity');actor=identity.get('employeeId')
    if not isinstance(actor,int) or actor<=0 or (permission_name and not identity.get(permission_name)):
        raise LocalStoreError('Das Serverkonto benötigt die Berechtigung '+permission_label+'.')
    return url,actor,command


def connect_room_server(client,url,token,ca):
    return connect_sync_server(client,url,token,ca,'roomSync','canManageRooms','rooms.manage')

def sync_rooms(store, lock, is_current, client, url, token, ca):
    url,actor,command=connect_room_server(client,url,token,ca)
    def checked(fn):
        with lock:
            if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt; die Warteschlange bleibt erhalten.')
            return fn()
    checked(lambda:store.enable_room_sync('linux-'+uuid.uuid4().hex,url,actor))
    sent=0
    while True:
        state=checked(store.next_room_sync)
        if not state or state['serverUrl']!=url or state['actor']!=actor:
            raise LocalStoreError('Die Warteschlange gehört zu einem anderen Server oder Serverkonto.')
        operation=state['operation']
        if operation is None:
            rooms=command('-action','rooms','-include-archived')
            if not isinstance(rooms,list):raise LocalStoreError('Ungültige Raumliste vom Server.')
            received=checked(lambda:store.apply_room_snapshot(rooms))
            return {'sent':sent,'received':received}
        command('-action','sync-room-event','-payload',json.dumps(operation,ensure_ascii=False,separators=(',',':')))
        checked(lambda:store.acknowledge_room_sync(operation['operationId']))
        sent+=1


def inspect_room_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_room_server(client,url,token,ca)
    with lock:
        if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
        preview=store.room_sync_conflict()
    if not preview:raise LocalStoreError('Es gibt keine ausstehende Raumänderung.')
    if preview['serverUrl']!=url or preview['actor']!=actor:raise LocalStoreError('Die Warteschlange gehört zu einem anderen Server oder Serverkonto.')
    rooms=command('-action','rooms','-include-archived')
    if not isinstance(rooms,list):raise LocalStoreError('Ungültige Raumliste vom Server.')
    matches=[r for r in rooms if r.get('id')==preview['local']['id']]
    identity_conflict=False;operation=None
    if not matches:
        with lock:
            if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
            state=store.next_room_sync()
        operation=state.get('operation') if state else None
        if not operation or operation.get('operationId')!=preview['operationId'] or operation.get('action')!='create' or operation.get('expected') is not None:raise LocalStoreError('Für diesen Raum gibt es keinen eindeutig zugeordneten Serverraum.')
        name=operation.get('room',{}).get('name','')
        matches=[r for r in rooms if isinstance(r.get('name'),str) and r['name'].casefold()==name.casefold() and r.get('id')!=preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für die ursprüngliche Raumanlage gibt es keinen eindeutig gleichnamigen Serverraum.')
        identity_conflict=True
    if len(matches)!=1:raise LocalStoreError('Die Serverraumkennung ist nicht eindeutig.')
    remote=matches[0]
    if any(not isinstance(remote.get(k),str) or '\x00' in remote[k] for k in ('id','name','location','equipment')) or not remote['id'] or not remote['name'] or not remote['location'] or type(remote.get('capacity')) is not int or not 0<remote['capacity']<=2147483647 or type(remote.get('active')) is not bool:
        raise LocalStoreError('Der Serverraum enthält ungültige Daten.')
    result={'preview':preview,'remote':remote}
    if identity_conflict:result.update({'identityConflict':True,'operation':operation})
    return result


def _identity_room_id(db,preview,remote):
    old_id=preview['local']['id'];new_id=remote['id']
    row=db.execute("SELECT local_id FROM sync_object_links WHERE object_type='room' AND external_id=?",(old_id,)).fetchone()
    if not row or db.execute("SELECT 1 FROM sync_object_links WHERE object_type='room' AND external_id=?",(new_id,)).fetchone():raise LocalStoreError('Die Raumzuordnung hat sich geändert.')
    room=row[0]
    if db.execute('SELECT 1 FROM linux_room_sync_shadow WHERE room_id=?',(room,)).fetchone():raise LocalStoreError('Für den lokalen Raum existiert bereits ein bestätigter Ausgangsstand.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_booking_sync_shadow'").fetchone() and db.execute("SELECT 1 FROM linux_booking_sync_shadow WHERE json_extract(booking_json,'$.roomKey')=? LIMIT 1",(old_id,)).fetchone():raise LocalStoreError('Für den Raum existieren bereits zentral bestätigte Buchungen; die Zuordnung muss gesondert geklärt werden.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_booking_sync_shadow'").fetchone() and db.execute('SELECT 1 FROM desktop_booking_sync_shadow WHERE room_id=? LIMIT 1',(room,)).fetchone():raise LocalStoreError('Für den Raum existieren bereits zentral bestätigte Buchungen; die Zuordnung muss gesondert geklärt werden.')
    return room


def _reconcile_room_identity(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    from employee_sync import _db
    fresh=inspect_room_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed or not fresh.get('identityConflict'):raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    normalized,actor,command=connect_sync_server(client,url,token,ca,'roomIdentityReconciliation','canManageRooms','rooms.manage')
    if normalized!=reviewed['preview']['serverUrl'] or actor!=reviewed['preview']['actor']:raise LocalStoreError('Die Serverbindung hat sich geändert.')
    # Check local eligibility before making the durable server-side fence.
    with _db(store,lock,is_current) as db:
        state=store.next_room_sync()
        if not state or state['operation']!=reviewed['operation'] or store.room_sync_conflict()!=reviewed['preview']:raise LocalStoreError('Die lokale Warteschlange hat sich geändert.')
        _identity_room_id(db,reviewed['preview'],reviewed['remote'])
    # Fence the one operation that may already have left this machine. This
    # remains safely retryable even if the local transaction subsequently fails.
    command('-action','abandon-room-event','-payload',json.dumps({'operation':reviewed['operation'],'remote':reviewed['remote']},ensure_ascii=False,separators=(',',':')))
    fresh=inspect_room_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich nach der Serversperre geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            state=store.next_room_sync();preview=store.room_sync_conflict()
            if not state or state['operation']!=reviewed['operation'] or preview!=reviewed['preview']:raise LocalStoreError('Die lokale Warteschlange hat sich geändert.')
            remote=reviewed['remote'];local=preview['local'];old_id=local['id'];new_id=remote['id']
            room=_identity_room_id(db,preview,remote)
            previous=[{'operationId':v[0],'action':v[1],'room':json.loads(v[2]),'expected':json.loads(v[3]) if v[3] else None} for v in db.execute('SELECT operation_id,action,room_json,expected_json FROM linux_room_sync_outbox WHERE room_id=? ORDER BY seq',(room,))]
            enc=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':'))
            actor=store.session_employee()
            cursor=db.execute('INSERT INTO linux_room_sync_resolutions(room_id,actor,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(room,str(actor),enc(local),enc(remote),enc(previous)))
            db.execute('INSERT INTO linux_room_sync_resolution_details(resolution_id,choice,actor_employee_id,identity_mapping) VALUES(?,?,?,1)',(cursor.lastrowid,'server' if accept_server else 'local',actor))
            db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='room' AND local_id=? AND external_id=?",(new_id,room,old_id))
            db.execute('INSERT INTO linux_room_sync_shadow(room_id,room_json) VALUES(?,?)',(room,enc(remote)))
            db.execute('DELETE FROM linux_room_sync_outbox WHERE room_id=?',(room,))
            if accept_server:
                db.execute('UPDATE linux_room_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE rooms SET name=?,location=?,capacity=?,equipment=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['location'],remote['capacity'],remote['equipment'],int(remote['active']),room))
                db.execute('UPDATE linux_room_sync_guard SET suppressed=0 WHERE singleton=1')
            else:
                mapped={**local,'id':new_id}
                if mapped!=remote:
                    action='archive' if remote['active'] and not mapped['active'] else 'update'
                    db.execute('INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json) VALUES(?,?,?,?,?)',(uuid.uuid4().hex,action,room,enc(mapped),enc(remote)))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',(str(store.session_employee()),'rooms.sync.identity_mapped',str(room)+':'+new_id))
    except sqlite3.Error as exc:raise LocalStoreError('Die Raumzuordnung konnte nicht gespeichert werden: '+str(exc)) from exc


def rebase_room_conflict(store,lock,is_current,client,url,token,ca,reviewed):
    if reviewed.get('identityConflict'):return _reconcile_room_identity(store,lock,is_current,client,url,token,ca,reviewed,False)
    fresh=inspect_room_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    with lock:
        if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
        store.rebase_room_sync(reviewed['preview'],reviewed['remote'])


def accept_server_room_conflict(store,lock,is_current,client,url,token,ca,reviewed):
    if reviewed.get('identityConflict'):return _reconcile_room_identity(store,lock,is_current,client,url,token,ca,reviewed,True)
    fresh=inspect_room_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    with lock:
        if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
        store.accept_server_room(reviewed['preview'],reviewed['remote'])

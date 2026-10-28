"""Synchronize leave requests using the authenticated person's server identity."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from local_store import LocalStoreError
from room_sync import connect_sync_server

class LeaveRequestPendingError(LocalStoreError):
    """An earlier request must be sent by its own employee first."""


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))

@contextmanager
def transaction(store,lock,is_current):
    with lock:
        if not is_current():raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
        actor=store.session_employee()
        db=sqlite3.connect(store.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=5)
        try:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
            yield db,actor
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()


def setup(db,url):
    db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_config (singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,leave_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,leave_json TEXT NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_shadow (leave_id INTEGER PRIMARY KEY,leave_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_leave_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url FROM linux_leave_sync_config').fetchone()
    if config:
        if config[1]!=url:raise LocalStoreError('Urlaubsabgleich ist an einen anderen Server gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex
    db.execute('INSERT INTO linux_leave_sync_config VALUES(1,?,?)',(client,url))
    db.execute('''CREATE TRIGGER linux_leave_sync_insert AFTER INSERT ON leave_requests
      WHEN (SELECT suppressed FROM linux_leave_sync_guard WHERE singleton=1)=0
      BEGIN
        INSERT OR IGNORE INTO sync_object_links SELECT 'leave',client_id||':leave:'||NEW.id,NEW.id FROM linux_leave_sync_config;
        INSERT INTO linux_leave_sync_outbox(operation_id,action,leave_id,actor_id,leave_json)
          SELECT lower(hex(randomblob(16))),'request',NEW.id,NEW.employee_id,
          json_object('id',l.external_id,'employeeId',NEW.employee_id,'startsAt',NEW.starts_at,'endsAt',NEW.ends_at,'reason',NEW.reason,'status','pending','decidedBy',0)
          FROM sync_object_links l WHERE object_type='leave' AND local_id=NEW.id;
        INSERT INTO linux_leave_sync_outbox(operation_id,action,leave_id,actor_id,leave_json)
          SELECT lower(hex(randomblob(16))),'decide',NEW.id,NEW.decided_by,
          json_object('id',l.external_id,'employeeId',NEW.employee_id,'startsAt',NEW.starts_at,'endsAt',NEW.ends_at,'reason',NEW.reason,'status',NEW.status,'decidedBy',NEW.decided_by)
          FROM sync_object_links l WHERE object_type='leave' AND local_id=NEW.id AND NEW.status IN ('approved','rejected');
      END''')
    db.execute('''CREATE TRIGGER linux_leave_sync_decide AFTER UPDATE OF status,decided_by ON leave_requests
      WHEN (SELECT suppressed FROM linux_leave_sync_guard WHERE singleton=1)=0 AND OLD.status='pending' AND NEW.status IN ('approved','rejected')
      BEGIN
        INSERT OR IGNORE INTO sync_object_links SELECT 'leave',client_id||':leave:'||NEW.id,NEW.id FROM linux_leave_sync_config;
        INSERT INTO linux_leave_sync_outbox(operation_id,action,leave_id,actor_id,leave_json)
          SELECT lower(hex(randomblob(16))),'decide',NEW.id,NEW.decided_by,
          json_object('id',l.external_id,'employeeId',NEW.employee_id,'startsAt',NEW.starts_at,'endsAt',NEW.ends_at,'reason',NEW.reason,'status',NEW.status,'decidedBy',NEW.decided_by)
          FROM sync_object_links l WHERE object_type='leave' AND local_id=NEW.id;
      END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'leave',?||':leave:'||id,id FROM leave_requests",(client,))
    rows=db.execute("SELECT r.id,l.external_id,r.employee_id,r.starts_at,r.ends_at,r.reason,r.status,r.decided_by FROM leave_requests r JOIN sync_object_links l ON l.object_type='leave' AND l.local_id=r.id WHERE r.active=1 ORDER BY r.id").fetchall()
    for local,external,employee,start,end,reason,status,decider in rows:
        pending={'id':external,'employeeId':employee,'startsAt':start,'endsAt':end,'reason':reason,'status':'pending','decidedBy':0}
        db.execute("INSERT INTO linux_leave_sync_outbox(operation_id,action,leave_id,actor_id,leave_json) VALUES(?,'request',?,?,?)",(uuid.uuid4().hex,local,employee,encode(pending)))
        if status in ('approved','rejected'):
            db.execute("INSERT INTO linux_leave_sync_outbox(operation_id,action,leave_id,actor_id,leave_json) VALUES(?,'decide',?,?,?)",(uuid.uuid4().hex,local,decider,encode({**pending,'status':status,'decidedBy':decider})))
        elif status!='pending':raise LocalStoreError('Ein vorhandener Urlaubsstatus wird noch nicht unterstützt: '+status)
    return client


def account(db,local):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_employee_sync_shadow'").fetchone():
        raise LocalStoreError('Bitte zuerst die Serverkonten im Mitarbeiterverzeichnis zuordnen und den Mitarbeiterabgleich durchführen.')
    row=db.execute("SELECT json_extract(employee_json,'$.accountEmployeeId') FROM linux_employee_sync_shadow WHERE employee_id=?",(local,)).fetchone()
    if not row or type(row[0]) is not int or row[0]<=0:raise LocalStoreError('Für einen beteiligten Mitarbeiter fehlt die bestätigte Serverkonto-Zuordnung. Bitte zuerst den Mitarbeiterabgleich durchführen.')
    same=db.execute("SELECT count(*) FROM linux_employee_sync_shadow WHERE json_extract(employee_json,'$.accountEmployeeId')=?",(row[0],)).fetchone()[0]
    if same!=1:raise LocalStoreError('Eine Serverkonto-ID ist mehreren lokalen Mitarbeitern zugeordnet.')
    return row[0]


def local_id(db,server_id):
    matches=db.execute("SELECT employee_id FROM linux_employee_sync_shadow WHERE json_extract(employee_json,'$.accountEmployeeId')=?",(server_id,)).fetchall()
    return matches[0][0] if len(matches)==1 else None

def employee_external_id(db,local):
    row=db.execute("SELECT l.external_id,json_extract(s.employee_json,'$.id') FROM sync_object_links l JOIN linux_employee_sync_shadow s ON s.employee_id=l.local_id WHERE l.object_type='employee' AND l.local_id=?",(local,)).fetchone()
    if not row or not row[0] or row[0]!=row[1]:raise LocalStoreError('Die bestätigte Mitarbeiterkennung fehlt oder hat sich geändert. Bitte den Mitarbeiterabgleich durchführen.')
    return row[0]

def snapshot_binding_matches(db,raw):
    employee=local_id(db,raw.get('employeeId'))
    decider=local_id(db,raw.get('decidedBy')) if raw.get('decidedBy') else 0
    if employee is None or decider is None:return False
    try:
        return raw.get('employeeExternalId')==employee_external_id(db,employee) and (not decider or raw.get('deciderExternalId')==employee_external_id(db,decider))
    except LocalStoreError:return False

def leave_value(raw):
    return {k:v for k,v in {**raw,'decidedBy':raw.get('decidedBy',0)}.items() if k not in ('identityVerified','identityOriginVerified','employeeExternalId','deciderExternalId')}


def sync_leave(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'leaveSync',None,'leave.request oder leave.approve')
    capabilities=command('-action','capabilities')
    if capabilities.get('leaveIdentityVerified')!=1:raise LocalStoreError('Der Server unterstützt die sichere Urlaubszuordnung noch nicht.')
    identity=command('-action','identity')
    if identity.get('employeeId')!=actor or not (identity.get('canRequestLeave') or identity.get('canApproveLeave')):raise LocalStoreError('Das Serverkonto benötigt leave.request oder leave.approve.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
            client_id=setup(db,url)
        sent=0;waiting_for_request=False
        while True:
            with transaction(store,lock,is_current) as (db,local_actor):
                if account(db,local_actor)!=actor:raise LocalStoreError('Die Serverkonto-Zuordnung hat sich geändert.')
                row=db.execute('SELECT o.seq,o.operation_id,o.action,o.leave_id,o.leave_json,o.wire_json FROM linux_leave_sync_outbox o WHERE o.actor_id=? AND NOT EXISTS (SELECT 1 FROM linux_leave_sync_outbox older WHERE older.leave_id=o.leave_id AND older.seq<o.seq) ORDER BY o.seq LIMIT 1',(local_actor,)).fetchone()
                if not row and db.execute('SELECT 1 FROM linux_leave_sync_outbox WHERE actor_id=? LIMIT 1',(local_actor,)).fetchone():waiting_for_request=True
                if row:
                    seq,op,action,leave_id,payload,wire=row
                    local=json.loads(payload); employee=account(db,local['employeeId'])
                    desired={**local,'employeeId':employee,'decidedBy':actor if action=='decide' else 0}
                    expected={**desired,'status':'pending','decidedBy':0} if action=='decide' else None
                    value={'clientId':client_id,'operationId':op,'action':action,'leave':desired,'expected':expected}
                    frozen=json.loads(wire) if wire is not None else None
                    if (frozen is None and capabilities.get('leaveIdentityBinding')==1) or (frozen is not None and 'employeeExternalId' in frozen):
                        value['employeeExternalId']=employee_external_id(db,local['employeeId'])
                        if action=='decide':value['deciderExternalId']=employee_external_id(db,local_actor)
                    if frozen is not None and frozen!=value:raise LocalStoreError('Die Mitarbeiterzuordnung einer ausstehenden Übertragung hat sich geändert.')
                    if wire is None:db.execute('UPDATE linux_leave_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-leave-event','-payload',encode(value))
            with transaction(store,lock,is_current) as (db,current_actor):
                if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
                if db.execute('SELECT wire_json FROM linux_leave_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),):raise LocalStoreError('Die Urlaubswarteschlange hat sich geändert.')
                db.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?) ON CONFLICT(leave_id) DO UPDATE SET leave_json=excluded.leave_json',(leave_id,encode(value['leave'])))
                db.execute('DELETE FROM linux_leave_sync_outbox WHERE seq=?',(seq,))
                db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','leave.sync.sent',str(leave_id)+':'+action))
                sent+=1
        snapshot=command('-action','leave')
        if not isinstance(snapshot,list):raise LocalStoreError('Ungültige Urlaubsliste vom Server.')
        received=0;unmapped=0
        with transaction(store,lock,is_current) as (db,current_actor):
            if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
            seen=set()
            for raw in snapshot:
                if isinstance(raw,dict) and raw.get('identityVerified') is False:
                    unmapped+=1;continue
                if isinstance(raw,dict) and capabilities.get('leaveSnapshotIdentityBinding')==1 and raw.get('identityOriginVerified') is False:
                    unmapped+=1;continue
                if not isinstance(raw,dict) or raw.get('identityVerified') is not True or not isinstance(raw.get('id'),str) or not raw['id'] or raw['id'] in seen or type(raw.get('employeeId')) is not int or raw['employeeId']<=0 or type(raw.get('startsAt')) is not int or raw['startsAt']<=0 or type(raw.get('endsAt')) is not int or raw['endsAt']<=raw['startsAt'] or not isinstance(raw.get('reason'),str) or not raw['reason'].strip() or raw.get('status') not in ('pending','approved','rejected') or type(raw.get('decidedBy',0)) is not int:
                    raise LocalStoreError('Ungültiger Urlaubsantrag im Serverbestand.')
                seen.add(raw['id']);remote=leave_value(raw)
                if (remote['status']=='pending' and remote['decidedBy']!=0) or (remote['status']!='pending' and (remote['decidedBy']<=0 or remote['decidedBy']==remote['employeeId'])):raise LocalStoreError('Ungültige Entscheidung im Serverbestand.')
                emp=local_id(db,remote['employeeId']);decider=local_id(db,remote['decidedBy']) if remote['decidedBy'] else 0
                if emp is None or decider is None:unmapped+=1;continue
                if capabilities.get('leaveSnapshotIdentityBinding')==1 and not snapshot_binding_matches(db,raw):unmapped+=1;continue
                row=db.execute("SELECT r.id,r.employee_id,r.starts_at,r.ends_at,r.reason,r.status,r.decided_by,r.active,s.leave_json FROM sync_object_links l JOIN leave_requests r ON r.id=l.local_id LEFT JOIN linux_leave_sync_shadow s ON s.leave_id=r.id WHERE l.object_type='leave' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,employee,start,end,reason,status,decided,active,shadow=row
                    if db.execute('SELECT 1 FROM linux_leave_sync_outbox WHERE leave_id=?',(local,)).fetchone():continue
                    current={'id':remote['id'],'employeeId':account(db,employee),'startsAt':start,'endsAt':end,'reason':reason,'status':status,'decidedBy':account(db,decided) if decided else 0}
                    if not active or not shadow or current!=json.loads(shadow):raise LocalStoreError('Urlaubskonflikt: Lokaler Stand weicht vom gemeinsamen Ausgangszustand ab.')
                    if current!=remote:
                        if any(current[k]!=remote[k] for k in ('employeeId','startsAt','endsAt','reason')) or (current['status']!='pending' and (current['status']!=remote['status'] or current['decidedBy']!=remote['decidedBy'])):
                            raise LocalStoreError('Urlaubskonflikt: Mitarbeiterzuordnung oder eine bestehende Entscheidung weicht vom Serverstand ab.')
                        db.execute('UPDATE leave_requests SET employee_id=?,starts_at=?,ends_at=?,reason=?,status=?,decided_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(emp,remote['startsAt'],remote['endsAt'],remote['reason'],remote['status'],decider,local));received+=1
                else:
                    local=db.execute('INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason,status,decided_by,active) VALUES(?,?,?,?,?,?,1)',(emp,remote['startsAt'],remote['endsAt'],remote['reason'],remote['status'],decider)).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('leave',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?) ON CONFLICT(leave_id) DO UPDATE SET leave_json=excluded.leave_json',(local,encode(remote)))
                if not row or current!=remote:
                    db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','leave.sync.received',str(local)+':'+remote['status']))
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
        if waiting_for_request:raise LeaveRequestPendingError('Ein älterer Antrag muss zuerst vom zugeordneten Antragsteller synchronisiert werden. Andere verfügbare Änderungen wurden abgeglichen.')
        return {'sent':sent,'received':received,'unmapped':unmapped}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Urlaubsabgleich fehlgeschlagen: '+str(exc)) from exc


def _conflict_preview(db,local_actor,url,actor):
    if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
    config=db.execute('SELECT server_url FROM linux_leave_sync_config WHERE singleton=1').fetchone()
    if not config or config[0]!=url:raise LocalStoreError('Urlaubswarteschlange gehört zu einem anderen Server.')
    row=db.execute("""SELECT o.seq,o.operation_id,o.action,o.leave_id,r.employee_id,r.starts_at,r.ends_at,r.reason,r.status,r.decided_by,r.active,l.external_id
      FROM linux_leave_sync_outbox o JOIN leave_requests r ON r.id=o.leave_id
      JOIN sync_object_links l ON l.object_type='leave' AND l.local_id=r.id
      WHERE o.actor_id=? ORDER BY o.seq LIMIT 1""",(local_actor,)).fetchone()
    if not row:raise LocalStoreError('Es gibt keine eigene ausstehende Urlaubsänderung.')
    seq,operation,action,leave_id,employee,start,end,reason,status,decided,active,external=row
    pending=db.execute('SELECT seq,operation_id,action,actor_id,leave_json,wire_json FROM linux_leave_sync_outbox WHERE leave_id=? ORDER BY seq',(leave_id,)).fetchall()
    if any(v[3]!=local_actor for v in pending):raise LocalStoreError('Für diesen Antrag stehen Änderungen einer anderen Person aus. Diese müssen zuerst abgeglichen werden.')
    local={'id':external,'employeeId':account(db,employee),'startsAt':start,'endsAt':end,'reason':reason,'status':status,'decidedBy':account(db,decided) if decided else 0,'active':bool(active)}
    return {'seq':seq,'operationId':operation,'action':action,'localId':leave_id,'local':local,'pending':[list(v) for v in pending]}


def _validate_leave_identity_mapping(db,preview,remote,actor,allow_prepared=False):
    config=db.execute('SELECT client_id FROM linux_leave_sync_config WHERE singleton=1').fetchone()
    if not config or preview['local']['id']!=config[0]+':leave:'+str(preview['localId']):raise LocalStoreError('Der lokale Antrag besitzt bereits eine fremde oder zuvor zugeordnete Kennung.')
    pending=preview['pending']
    if preview['action']!='request' or preview['local']['employeeId']!=actor or preview['local']['status']!='pending' or not preview['local']['active'] or len(pending)!=1 or pending[0][2]!='request' or (pending[0][5] is not None and not allow_prepared):raise LocalStoreError('Nur ein eigener, offener Antrag mit einer sicheren Übertragung kann zugeordnet werden.')
    if db.execute('SELECT 1 FROM linux_leave_sync_shadow WHERE leave_id=?',(preview['localId'],)).fetchone():raise LocalStoreError('Für diesen Antrag existiert bereits ein bestätigter Ausgangsstand.')
    if db.execute("SELECT 1 FROM sync_object_links WHERE object_type='leave' AND external_id=?",(remote['id'],)).fetchone():raise LocalStoreError('Der Serverantrag ist bereits lokal zugeordnet.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_subtask_sync_config'").fetchone() and db.execute('SELECT 1 FROM desktop_subtask_sync_config LIMIT 1').fetchone():raise LocalStoreError('Diese Datenbank ist auch für den Windows-Abgleich eingerichtet; die Urlaubskennung kann hier nicht sicher zugeordnet werden.')

def inspect_leave_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'leaveSync',None,'leave.request oder leave.approve')
    capabilities=command('-action','capabilities')
    if capabilities.get('leaveIdentityVerified')!=1:raise LocalStoreError('Der Server unterstützt die sichere Urlaubszuordnung noch nicht.')
    identity=command('-action','identity')
    if identity.get('employeeId')!=actor or not (identity.get('canRequestLeave') or identity.get('canApproveLeave')):raise LocalStoreError('Das Serverkonto benötigt leave.request oder leave.approve.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor): preview=_conflict_preview(db,local_actor,url,actor)
        if preview['action']=='decide' and not identity.get('canApproveLeave'):raise LocalStoreError('Das Serverkonto benötigt leave.approve.')
        if preview['action']=='request' and not identity.get('canRequestLeave'):raise LocalStoreError('Das Serverkonto benötigt leave.request.')
        snapshot=command('-action','leave')
        if not isinstance(snapshot,list):raise LocalStoreError('Ungültige Urlaubsliste vom Server.')
        matches=[v for v in snapshot if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        identity_conflict=False
        if not matches:
            if capabilities.get('leaveSnapshotIdentityBinding')!=1:raise LocalStoreError('Dieser Server unterstützt die sichere Mitarbeiterzuordnung für Urlaubsanträge noch nicht.')
            local=preview['local']
            matches=[v for v in snapshot if isinstance(v,dict) and v.get('id')!=local['id'] and v.get('identityVerified') is True and v.get('identityOriginVerified') is True and all(v.get(k)==local[k] for k in ('employeeId','startsAt','endsAt','reason'))]
            if len(matches)!=1:raise LocalStoreError('Für diesen Antrag gibt es keinen eindeutig passenden Serverantrag derselben Person und desselben Zeitraums.')
            with transaction(store,lock,is_current) as (db,current_actor):
                if current_actor!=local_actor or _conflict_preview(db,current_actor,url,actor)!=preview:raise LocalStoreError('Die lokale Urlaubsänderung hat sich geändert. Bitte erneut prüfen.')
                _validate_leave_identity_mapping(db,preview,matches[0],actor,True)
            frozen=json.loads(preview['pending'][0][5]) if preview['pending'][0][5] is not None else None
            if frozen is not None:
                value=frozen.get('leave') if isinstance(frozen,dict) else None
                if not isinstance(value,dict) or frozen.get('action')!='request' or frozen.get('expected') is not None or frozen.get('operationId')!=preview['operationId'] or any(value.get(k)!=preview['local'][k] for k in ('id','employeeId','startsAt','endsAt','reason','status','decidedBy')):raise LocalStoreError('Die vorbereitete Urlaubsübertragung ist ungültig oder wurde geändert.')
            identity_conflict=True
        if len(matches)!=1:raise LocalStoreError('Der Serverantrag ist nicht eindeutig.')
        value=matches[0]
        if value.get('identityVerified') is not True:raise LocalStoreError('Die Mitarbeiterzuordnung dieses Serverantrags ist nicht bestätigt.')
        if capabilities.get('leaveSnapshotIdentityBinding')==1 and value.get('identityOriginVerified') is False:raise LocalStoreError('Die ursprüngliche Mitarbeiterzuordnung dieses Serverantrags muss zuerst bestätigt werden.')
        if type(value.get('employeeId')) is not int or value['employeeId']<=0 or type(value.get('startsAt')) is not int or value['startsAt']<=0 or type(value.get('endsAt')) is not int or value['endsAt']<=value['startsAt'] or not isinstance(value.get('reason'),str) or not value['reason'].strip() or value.get('status') not in ('pending','approved','rejected') or type(value.get('decidedBy',0)) is not int:
            raise LocalStoreError('Ungültiger Urlaubsantrag im Serverbestand.')
        if capabilities.get('leaveSnapshotIdentityBinding')==1:
            with transaction(store,lock,is_current) as (db,current_actor):
                if current_actor!=local_actor or not snapshot_binding_matches(db,value):raise LocalStoreError('Die Mitarbeiterzuordnung des Serverantrags hat sich geändert. Bitte zuerst Mitarbeiter abgleichen.')
        remote=leave_value(value)
        if (remote['status']=='pending' and remote['decidedBy']!=0) or (remote['status']!='pending' and (remote['decidedBy']<=0 or remote['decidedBy']==remote['employeeId'])):raise LocalStoreError('Ungültige Entscheidung im Serverbestand.')
        # An in-flight decision can still be accepted while the server request
        # is pending. Only a terminal server decision prevents that replay.
        if preview['action']=='decide' and remote['status']=='pending':raise LocalStoreError('Der Serverantrag ist noch offen. Die ausstehende Entscheidung kann noch übertragen werden und darf deshalb nicht verworfen werden.')
        result={'preview':preview,'remote':remote,'url':url,'actor':actor}
        if identity_conflict:
            result['identityConflict']=True
            if frozen is not None:result['preparedOperation']=frozen
        return result
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Urlaubskonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def accept_server_leave(store,lock,is_current,client,url,token,ca,reviewed):
    fresh=inspect_leave_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    if reviewed.get('preparedOperation') is not None:
        normalized,actor,command=connect_sync_server(client,url,token,ca,'leaveRequestReconciliation',None,'leave.request')
        if normalized!=reviewed['url'] or actor!=reviewed['actor']:raise LocalStoreError('Die Serverbindung hat sich geändert.')
        with transaction(store,lock,is_current) as (db,local_actor):
            if _conflict_preview(db,local_actor,normalized,actor)!=reviewed['preview']:raise LocalStoreError('Die lokale Urlaubsänderung hat sich geändert. Bitte erneut prüfen.')
            _validate_leave_identity_mapping(db,reviewed['preview'],reviewed['remote'],actor,True)
        command('-action','abandon-leave-event','-payload',encode({'operation':reviewed['preparedOperation'],'remote':reviewed['remote']}))
        if inspect_leave_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich nach der Serversperre geändert. Bitte erneut prüfen.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            preview=_conflict_preview(db,local_actor,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Urlaubsänderung hat sich geändert. Bitte erneut prüfen.')
            remote=reviewed['remote'];employee=local_id(db,remote['employeeId']);decider=local_id(db,remote['decidedBy']) if remote['decidedBy'] else 0
            if employee is None or decider is None:raise LocalStoreError('Antragsteller oder Entscheider ist keinem eindeutigen lokalen Mitarbeiter zugeordnet.')
            if reviewed.get('identityConflict'):
                _validate_leave_identity_mapping(db,preview,remote,reviewed['actor'],reviewed.get('preparedOperation') is not None)
                updated=db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='leave' AND local_id=? AND external_id=?",(remote['id'],preview['localId'],preview['local']['id'])).rowcount
                if updated!=1:raise LocalStoreError('Die lokale Urlaubskennung hat sich geändert.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_resolutions (id INTEGER PRIMARY KEY,leave_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_leave_sync_resolutions(leave_id,actor_id,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(preview['localId'],local_actor,encode(preview['local']),encode(remote),encode(preview['pending'])))
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
            db.execute('UPDATE leave_requests SET employee_id=?,starts_at=?,ends_at=?,reason=?,status=?,decided_by=?,active=1,updated_at=CURRENT_TIMESTAMP WHERE id=?',(employee,remote['startsAt'],remote['endsAt'],remote['reason'],remote['status'],decider,preview['localId']))
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute('INSERT INTO linux_leave_sync_shadow VALUES(?,?) ON CONFLICT(leave_id) DO UPDATE SET leave_json=excluded.leave_json',(preview['localId'],encode(remote)))
            db.execute('DELETE FROM linux_leave_sync_outbox WHERE leave_id=? AND actor_id=?',(preview['localId'],local_actor))
            action='leave.sync.identity_mapped' if reviewed.get('identityConflict') else 'leave.sync.accepted_server'
            subject=str(preview['localId'])+(':'+preview['local']['id']+' -> '+remote['id'] if reviewed.get('identityConflict') else '')
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',(str(local_actor),action,subject))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Serverantrag konnte nicht übernommen werden: '+str(exc)) from exc


def leave_sync_config(store,lock,is_current):
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_leave_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url FROM linux_leave_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'serverActor':account(db,local_actor)} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Urlaubskonfiguration konnte nicht gelesen werden: '+str(exc)) from exc


def _incoming_leave_preview(db,local_actor,url,actor,snapshot,require_binding=False):
    if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
    if db.execute('SELECT server_url FROM linux_leave_sync_config WHERE singleton=1').fetchone()!=(url,):raise LocalStoreError('Die Urlaubswarteschlange gehört zu einem anderen Server.')
    if not isinstance(snapshot,list):raise LocalStoreError('Ungültige Urlaubsliste vom Server.')
    seen=set()
    for raw in snapshot:
        if not isinstance(raw,dict) or not isinstance(raw.get('id'),str) or not raw['id'] or raw['id'] in seen:raise LocalStoreError('Ungültiger oder doppelter Urlaubsantrag vom Server.')
        seen.add(raw['id'])
        if raw.get('identityVerified') is not True:continue
        if require_binding and raw.get('identityOriginVerified') is False:continue
        if type(raw.get('employeeId')) is not int or raw['employeeId']<=0 or type(raw.get('startsAt')) is not int or raw['startsAt']<=0 or type(raw.get('endsAt')) is not int or raw['endsAt']<=raw['startsAt'] or not isinstance(raw.get('reason'),str) or not raw['reason'].strip() or raw.get('status') not in ('pending','approved','rejected') or type(raw.get('decidedBy',0)) is not int:raise LocalStoreError('Ungültiger Urlaubsantrag vom Server.')
        if require_binding and not snapshot_binding_matches(db,raw):continue
        remote=leave_value(raw)
        if (remote['status']=='pending' and remote['decidedBy']!=0) or (remote['status']!='pending' and (remote['decidedBy']<=0 or remote['decidedBy']==remote['employeeId'])):raise LocalStoreError('Ungültige Entscheidung im Serverbestand.')
        row=db.execute("SELECT r.id,r.employee_id,r.starts_at,r.ends_at,r.reason,r.status,r.decided_by,r.active,s.leave_json FROM sync_object_links l JOIN leave_requests r ON r.id=l.local_id LEFT JOIN linux_leave_sync_shadow s ON s.leave_id=r.id WHERE l.object_type='leave' AND l.external_id=?",(remote['id'],)).fetchone()
        if not row:continue
        local,employee,start,end,reason,status,decided,active,shadow=row
        if db.execute('SELECT 1 FROM linux_leave_sync_outbox WHERE leave_id=?',(local,)).fetchone():continue
        current={'id':remote['id'],'employeeId':account(db,employee),'startsAt':start,'endsAt':end,'reason':reason,'status':status,'decidedBy':account(db,decided) if decided else 0}
        incompatible=any(current[k]!=remote[k] for k in ('employeeId','startsAt','endsAt','reason')) or (current['status']!='pending' and (current['status']!=remote['status'] or current['decidedBy']!=remote['decidedBy']))
        if not incompatible:continue
        if not active or not shadow or current!=json.loads(shadow):raise LocalStoreError('Die lokale Änderung besitzt keinen bestätigten Ausgangsstand und muss gesondert geklärt werden.')
        remote_employee=local_id(db,remote['employeeId']);remote_decider=local_id(db,remote['decidedBy']) if remote['decidedBy'] else 0
        if remote_employee is None or remote_decider is None:raise LocalStoreError('Antragsteller oder Entscheider ist keinem eindeutigen lokalen Mitarbeiter zugeordnet.')
        def name(id):
            if not id:return 'Keine'
            match=db.execute('SELECT name FROM employees WHERE id=?',(id,)).fetchone()
            if not match:raise LocalStoreError('Ein beteiligter Mitarbeiter fehlt lokal.')
            return match[0]
        return {'localId':local,'local':current,'remote':remote,'localEmployeeName':name(employee),'remoteEmployeeName':name(remote_employee),'localDeciderName':name(decided),'remoteDeciderName':name(remote_decider)}
    raise LocalStoreError('Es wurde kein abweichender bereits abgeglichener Urlaubsantrag gefunden.')


def inspect_incoming_leave_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'leaveSync','canApproveLeave','leave.approve')
    capabilities=command('-action','capabilities')
    if capabilities.get('leaveIdentityVerified')!=1:raise LocalStoreError('Der Server unterstützt die sichere Urlaubszuordnung noch nicht.')
    try:
        snapshot=command('-action','leave')
        if not isinstance(snapshot,list) or any(not isinstance(v,dict) or not isinstance(v.get('id'),str) or not v['id'] for v in snapshot) or len({v['id'] for v in snapshot})!=len(snapshot):raise LocalStoreError('Ungültige oder doppelte Urlaubsanträge vom Server.')
        snapshot=sorted(snapshot,key=lambda value:value['id'])
        with transaction(store,lock,is_current) as (db,local_actor):
            if not store.identity().get('canManageEmployees'):raise LocalStoreError('Für diese Aktion fehlt die lokale Mitarbeiterverwaltung.')
            preview=_incoming_leave_preview(db,local_actor,url,actor,snapshot,capabilities.get('leaveSnapshotIdentityBinding')==1)
        return {'url':url,'actor':actor,'preview':preview}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Urlaubszuordnung konnte nicht geprüft werden: '+str(exc)) from exc


def accept_incoming_leave_conflict(store,lock,is_current,client,url,token,ca,reviewed):
    if inspect_incoming_leave_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich geändert. Bitte erneut prüfen.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            if not store.identity().get('canManageEmployees'):raise LocalStoreError('Für diese Aktion fehlt die lokale Mitarbeiterverwaltung.')
            preview=_incoming_leave_preview(db,local_actor,reviewed['url'],reviewed['actor'],[{**reviewed['preview']['remote'],'identityVerified':True}])
            if preview!=reviewed['preview']:raise LocalStoreError('Der lokale Antrag hat sich geändert. Bitte erneut prüfen.')
            remote=preview['remote'];employee=local_id(db,remote['employeeId']);decider=local_id(db,remote['decidedBy']) if remote['decidedBy'] else 0
            db.execute('CREATE TABLE IF NOT EXISTS linux_leave_sync_resolutions (id INTEGER PRIMARY KEY,leave_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_leave_sync_resolutions(leave_id,actor_id,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(preview['localId'],local_actor,encode(preview['local']),encode(remote),'[]'))
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=1 WHERE singleton=1')
            db.execute('UPDATE leave_requests SET employee_id=?,starts_at=?,ends_at=?,reason=?,status=?,decided_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(employee,remote['startsAt'],remote['endsAt'],remote['reason'],remote['status'],decider,preview['localId']))
            db.execute('UPDATE linux_leave_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute('UPDATE linux_leave_sync_shadow SET leave_json=? WHERE leave_id=?',(encode(remote),preview['localId']))
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'leave.sync.reconciled_incoming',?)",(str(local_actor),str(preview['localId'])))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Urlaubszuordnung konnte nicht übernommen werden: '+str(exc)) from exc

"""Explicit, durable employee directory synchronization for local sessions."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from local_store import LocalStoreError
from room_sync import connect_sync_server

FIELDS = ('id', 'name', 'role', 'department', 'presence', 'active')

def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def _directory(value):
    return {key: value[key] for key in FIELDS}

def _validate(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() or '\0' in value[k] for k in ('id','name','role','department','presence')):
        raise LocalStoreError('Ungültiger Mitarbeiter im Serverbestand.')
    if value['presence'] not in ('office','remote','meeting','away') or type(value.get('active')) is not bool:
        raise LocalStoreError('Ungültiger Mitarbeiterstatus im Serverbestand.')
    account=value.get('accountEmployeeId',0)
    if type(account) is not int or account<0:
        raise LocalStoreError('Ungültige Serverkonto-Zuordnung im Mitarbeiterbestand.')
    return {**_directory(value), 'accountEmployeeId': account}

@contextmanager
def _db(store, lock, is_current):
    with lock:
        if not is_current(): raise LocalStoreError('Die lokale Sitzung wurde gewechselt.')
        # This existing native call checks the active session and admin.manage.
        store.next_room_sync()
        db=sqlite3.connect(store.path.resolve().as_uri()+'?mode=rw', uri=True, timeout=5)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally: db.close()

# Every trigger runs within the employee write transaction. Account IDs are
# server-side references and never derive from local numeric account IDs.
_EMPLOYEE_NEW = "json_object('id',l.external_id,'name',NEW.name,'role',NEW.role,'department',NEW.department,'presence',NEW.presence,'active',json(CASE WHEN NEW.active THEN 'true' ELSE 'false' END),'accountEmployeeId',COALESCE((SELECT json_extract(employee_json,'$.accountEmployeeId') FROM linux_employee_sync_shadow WHERE employee_id=NEW.id),0))"
_EMPLOYEE_OLD = _EMPLOYEE_NEW.replace('NEW.', 'OLD.').replace('employee_id=OLD.id','employee_id=NEW.id')
_CREATE = _EMPLOYEE_NEW.replace("json(CASE WHEN NEW.active THEN 'true' ELSE 'false' END)", "json('true')")

def _upgrade_actor_tracking(db):
    if 'actor_id' not in {row[1] for row in db.execute('PRAGMA table_info(linux_employee_sync_outbox)')}:
        db.execute('ALTER TABLE linux_employee_sync_outbox ADD COLUMN actor_id INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_actor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),employee_id INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_employee_sync_actor VALUES(1,0)')
    db.execute('DROP TRIGGER IF EXISTS linux_employee_sync_update')
    db.execute('''CREATE TRIGGER linux_employee_sync_update AFTER UPDATE OF name,role,department,presence,active ON employees
        WHEN EXISTS(SELECT 1 FROM linux_employee_sync_config) AND (SELECT suppressed FROM linux_employee_sync_guard WHERE singleton=1)=0
        AND (OLD.name IS NOT NEW.name OR OLD.role IS NOT NEW.role OR OLD.department IS NOT NEW.department OR OLD.presence IS NOT NEW.presence OR OLD.active IS NOT NEW.active)
        BEGIN
        INSERT OR IGNORE INTO sync_object_links SELECT 'employee',client_id||':employee:'||NEW.id,NEW.id FROM linux_employee_sync_config;
        INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json,expected_json,actor_id)
        SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,''' + _EMPLOYEE_NEW + ',' + _EMPLOYEE_OLD + ''',COALESCE((SELECT employee_id FROM linux_employee_sync_actor WHERE singleton=1),0) FROM sync_object_links l WHERE object_type='employee' AND local_id=NEW.id;
        END''')

def _ensure(db,client,url,actor):
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), client_id TEXT NOT NULL, server_url TEXT NOT NULL, server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE, action TEXT NOT NULL, employee_id INTEGER NOT NULL, employee_json TEXT NOT NULL, expected_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_shadow (employee_id INTEGER PRIMARY KEY, employee_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1), suppressed INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_resolutions (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, choice TEXT NOT NULL, local_json TEXT NOT NULL, remote_json TEXT NOT NULL, previous_operations TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    db.execute('INSERT OR IGNORE INTO linux_employee_sync_guard VALUES(1,0)')
    _upgrade_actor_tracking(db)
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_employee_sync_config').fetchone()
    if config and (config[1]!=url or config[2]!=actor):
        raise LocalStoreError('Die Mitarbeiterwarteschlange ist an einen anderen Server oder ein anderes Serverkonto gebunden.')
    if config: return
    db.execute('INSERT INTO linux_employee_sync_config VALUES(1,?,?,?)',(client,url,actor))
    db.execute('''CREATE TRIGGER linux_employee_sync_insert AFTER INSERT ON employees
        WHEN EXISTS(SELECT 1 FROM linux_employee_sync_config) AND (SELECT suppressed FROM linux_employee_sync_guard WHERE singleton=1)=0
        BEGIN
        INSERT OR IGNORE INTO sync_object_links SELECT 'employee',client_id||':employee:'||NEW.id,NEW.id FROM linux_employee_sync_config;
        INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json,expected_json)
        SELECT lower(hex(randomblob(16))),'create',NEW.id,''' + _CREATE + ''',NULL FROM sync_object_links l WHERE object_type='employee' AND local_id=NEW.id;
        INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json,expected_json)
        SELECT lower(hex(randomblob(16))),'archive',NEW.id,''' + _EMPLOYEE_NEW + ',' + _CREATE + ''' FROM sync_object_links l WHERE object_type='employee' AND local_id=NEW.id AND NEW.active=0;
        END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'employee',?||':employee:'||id,id FROM employees",(client,))
    rows=db.execute("SELECT e.id,l.external_id,e.name,e.role,e.department,e.presence,e.active FROM employees e JOIN sync_object_links l ON l.object_type='employee' AND l.local_id=e.id ORDER BY e.id").fetchall()
    for local,external,name,role,department,presence,active in rows:
        value={'id':external,'name':name,'role':role,'department':department,'presence':presence,'active':True,'accountEmployeeId':0}
        db.execute("INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,_json(value)))
        if not active:
            archived={**value,'active':False}
            db.execute("INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,_json(archived),_json(value)))

def _apply(db, snapshot):
    if not isinstance(snapshot,list) or len(snapshot)>100000: raise LocalStoreError('Ungültige Mitarbeiterliste vom Server.')
    if db.execute('SELECT count(*) FROM linux_employee_sync_outbox').fetchone()[0]: raise LocalStoreError('Mitarbeiteränderungen stehen noch aus.')
    db.execute('UPDATE linux_employee_sync_guard SET suppressed=1 WHERE singleton=1')
    seen=set(); changed=0
    for raw in snapshot:
        remote=_validate(raw); external=remote['id']
        if external in seen: raise LocalStoreError('Doppelte Mitarbeiterkennung im Serverbestand.')
        seen.add(external)
        row=db.execute("""SELECT e.id,e.name,e.role,e.department,e.presence,e.active,s.employee_json
             FROM sync_object_links l JOIN employees e ON e.id=l.local_id
             LEFT JOIN linux_employee_sync_shadow s ON s.employee_id=e.id
             WHERE l.object_type='employee' AND l.external_id=?""",(external,)).fetchone()
        if row:
            local,name,role,department,presence,active,shadow=row
            current={'id':external,'name':name,'role':role,'department':department,'presence':presence,'active':bool(active)}
            if shadow is None or current!=_directory(json.loads(shadow)):
                raise LocalStoreError('Mitarbeiterkonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+name)
            if current!=_directory(remote):
                if not remote['active'] and db.execute('SELECT 1 FROM accounts WHERE employee_id=? AND enabled=1',(local,)).fetchone():
                    raise LocalStoreError('Der Server möchte ein aktives lokales Konto archivieren: '+name)
                db.execute('UPDATE employees SET name=?,role=?,department=?,presence=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['role'],remote['department'],remote['presence'],int(remote['active']),local));changed+=1
        else:
            cursor=db.execute('INSERT INTO employees(name,role,department,presence,active) VALUES(?,?,?,?,?)',(remote['name'],remote['role'],remote['department'],remote['presence'],int(remote['active'])))
            local=cursor.lastrowid
            db.execute("INSERT INTO sync_object_links VALUES('employee',?,?)",(external,local));changed+=1
        db.execute('INSERT INTO linux_employee_sync_shadow VALUES(?,?) ON CONFLICT(employee_id) DO UPDATE SET employee_json=excluded.employee_json',(local,_json(remote)))
    for (external,) in db.execute("SELECT l.external_id FROM linux_employee_sync_shadow s JOIN sync_object_links l ON l.object_type='employee' AND l.local_id=s.employee_id"):
        if external not in seen: raise LocalStoreError('Ein zuvor synchronisierter Mitarbeiter fehlt auf dem Server.')
    db.execute('UPDATE linux_employee_sync_guard SET suppressed=0 WHERE singleton=1')
    db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','employees.sync.pulled',?)",(str(changed),))
    return changed

def sync_employees(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'employeeSync','canManageEmployees','employees.manage')
    try:
        with _db(store,lock,is_current) as db: _ensure(db,'linux-'+uuid.uuid4().hex,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                config=db.execute('SELECT client_id,server_url,server_actor FROM linux_employee_sync_config').fetchone()
                if not config or config[1:]!=(url,actor): raise LocalStoreError('Mitarbeiterwarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('SELECT operation_id,action,employee_id,employee_json,expected_json FROM linux_employee_sync_outbox ORDER BY seq LIMIT 1').fetchone()
            if row is None:
                snapshot=command('-action','sync-employees')
                with _db(store,lock,is_current) as db: received=_apply(db,snapshot)
                return {'sent':sent,'received':received}
            operation,action,local,payload,expected=row
            data={'clientId':config[0],'operationId':operation,'action':action,'employee':json.loads(payload),'expected':json.loads(expected) if expected else None}
            command('-action','sync-employee-event','-payload',_json(data))
            with _db(store,lock,is_current) as db:
                first=db.execute('SELECT operation_id FROM linux_employee_sync_outbox ORDER BY seq LIMIT 1').fetchone()
                if first!=(operation,): raise LocalStoreError('Die Mitarbeiterwarteschlange hat sich geändert.')
                db.execute('INSERT INTO linux_employee_sync_shadow VALUES(?,?) ON CONFLICT(employee_id) DO UPDATE SET employee_json=excluded.employee_json',(local,payload))
                db.execute('DELETE FROM linux_employee_sync_outbox WHERE operation_id=?',(operation,))
            sent+=1
    except (sqlite3.Error,ValueError,KeyError) as exc:
        raise LocalStoreError('Mitarbeiterabgleich fehlgeschlagen: '+str(exc)) from exc


def _preview(db,url,actor):
    config=db.execute('SELECT server_url,server_actor FROM linux_employee_sync_config').fetchone()
    if config!=(url,actor): raise LocalStoreError('Die Mitarbeiterwarteschlange gehört zu einem anderen Server oder Serverkonto.')
    row=db.execute("""SELECT o.operation_id,o.employee_id,l.external_id,e.name,e.role,e.department,e.presence,e.active,
                         (SELECT max(seq) FROM linux_employee_sync_outbox WHERE employee_id=o.employee_id),
                         (SELECT count(*) FROM linux_employee_sync_outbox WHERE employee_id=o.employee_id)
                  FROM linux_employee_sync_outbox o JOIN employees e ON e.id=o.employee_id
                  JOIN sync_object_links l ON l.object_type='employee' AND l.local_id=e.id
                  ORDER BY o.seq LIMIT 1""").fetchone()
    if not row: raise LocalStoreError('Es gibt keine ausstehende Mitarbeiteränderung.')
    operation,local,external,name,role,department,presence,active,last,count=row
    return {'operationId':operation,'localId':local,'lastSeq':last,'count':count,
            'local':{'id':external,'name':name,'role':role,'department':department,'presence':presence,'active':bool(active)}}

def inspect_employee_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'employeeSync','canManageEmployees','employees.manage')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,url,actor)
            config=db.execute('SELECT client_id FROM linux_employee_sync_config').fetchone()
            head=db.execute('SELECT operation_id,action,employee_json,expected_json FROM linux_employee_sync_outbox ORDER BY seq LIMIT 1').fetchone()
            operation={'clientId':config[0],'operationId':head[0],'action':head[1],'employee':json.loads(head[2]),'expected':json.loads(head[3]) if head[3] else None}
        employees=command('-action','sync-employees')
        if not isinstance(employees,list): raise LocalStoreError('Ungültige Mitarbeiterliste vom Server.')
        matches=[_validate(e) for e in employees if isinstance(e,dict) and e.get('id')==preview['local']['id']]
        identity_conflict=False
        if not matches:
            if operation['operationId']!=preview['operationId'] or operation['action']!='create' or operation['expected'] is not None:raise LocalStoreError('Für diesen Mitarbeiter gibt es keinen eindeutig zugeordneten Servereintrag.')
            name=operation['employee']['name']
            matches=[_validate(e) for e in employees if isinstance(e,dict) and isinstance(e.get('name'),str) and e['name'].casefold()==name.casefold() and e.get('id')!=preview['local']['id']]
            if len(matches)!=1:raise LocalStoreError('Für die ursprüngliche Mitarbeiteranlage gibt es keinen eindeutig gleichnamigen Servereintrag.')
            identity_conflict=True
        if len(matches)!=1:raise LocalStoreError('Die Mitarbeiterkennung ist nicht eindeutig.')
        result={'preview':preview,'remote':matches[0],'url':url,'actor':actor}
        if identity_conflict:result.update({'identityConflict':True,'operation':operation})
        return result
    except (sqlite3.Error,ValueError,KeyError) as exc:
        raise LocalStoreError('Mitarbeiterkonflikt konnte nicht geprüft werden: '+str(exc)) from exc

def _identity_employee_check(db,preview,remote,accept_server):
    local=preview['localId'];old=preview['local']['id'];new=remote['id']
    if db.execute("SELECT local_id FROM sync_object_links WHERE object_type='employee' AND external_id=?",(old,)).fetchone()!=(local,) or db.execute("SELECT 1 FROM sync_object_links WHERE object_type='employee' AND external_id=?",(new,)).fetchone():raise LocalStoreError('Die Mitarbeiterzuordnung hat sich geändert.')
    if db.execute('SELECT 1 FROM linux_employee_sync_shadow WHERE employee_id=?',(local,)).fetchone():raise LocalStoreError('Für diesen Mitarbeiter existiert bereits ein bestätigter Ausgangsstand.')
    if accept_server and not remote['active'] and db.execute('SELECT 1 FROM accounts WHERE employee_id=? AND enabled=1',(local,)).fetchone():raise LocalStoreError('Ein Mitarbeiter mit aktivem lokalem Konto kann nicht archiviert werden.')
    if remote['accountEmployeeId'] and db.execute("SELECT 1 FROM linux_employee_sync_shadow WHERE employee_id<>? AND json_extract(employee_json,'$.accountEmployeeId')=?",(local,remote['accountEmployeeId'])).fetchone():raise LocalStoreError('Das Serverkonto ist bereits einem anderen lokalen Mitarbeiter zugeordnet.')
    # An already frozen or confirmed cross-domain payload containing the old
    # identity must not silently be retargeted. Unsynchronized numeric local
    # references remain attached to the same employee row.
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name GLOB 'linux_*_sync_*' OR name GLOB 'desktop_*_sync_*')")]
    for table in tables:
        if table in ('linux_employee_sync_outbox','linux_employee_sync_resolutions'):continue
        quoted='"'+table.replace('"','""')+'"'
        for col in db.execute('PRAGMA table_info('+quoted+')'):
            if 'TEXT' not in (col[2] or '').upper():continue
            column='"'+col[1].replace('"','""')+'"'
            if db.execute('SELECT 1 FROM '+quoted+' WHERE instr('+column+',?)>0 LIMIT 1',(old,)).fetchone():raise LocalStoreError('Die alte Mitarbeiterkennung wird bereits in anderen Abgleichdaten verwendet. Bitte diese Zuordnung gesondert klären.')
    checks=[('task','tasks','assignee_id'),('ticket','tickets','assignee_id'),('project','projects','owner_id'),('subtask','task_subtasks','assignee_id'),('comment','comments','author_id'),('attachment','attachments','uploaded_by'),('organization','organization_units','manager_id'),('leave','leave_requests','employee_id'),('leave','leave_requests','decided_by'),('task_approval','task_approvals','requested_by'),('task_approval','task_approvals','approver_id'),('task_approval','task_approvals','decided_by')]
    for kind,table,column in checks:
        if db.execute('SELECT 1 FROM '+table+' x JOIN sync_object_links l ON l.object_type=? AND l.local_id=x.id WHERE x.'+column+'=? LIMIT 1',(kind,local)).fetchone():raise LocalStoreError('Für diesen Mitarbeiter existieren bereits verknüpfte Vorgänge. Bitte deren Zuordnung gesondert klären.')
    for table in ('linux_booking_sync_shadow','desktop_own_booking_shadow'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            if db.execute('SELECT 1 FROM '+table+' s JOIN bookings b ON b.id=s.booking_id LEFT JOIN booking_block_details d ON d.booking_id=b.id WHERE b.user_id=? OR d.created_by=? LIMIT 1',(local,local)).fetchone():raise LocalStoreError('Für diesen Mitarbeiter existieren bereits zentral bestätigte Buchungen. Bitte deren Zuordnung gesondert klären.')
    return local


def _resolve_employee_identity(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    fresh=inspect_employee_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed or not fresh.get('identityConflict'):raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    normalized,actor,command=connect_sync_server(client,url,token,ca,'employeeIdentityReconciliation','canManageEmployees','employees.manage')
    if normalized!=reviewed['url'] or actor!=reviewed['actor']:raise LocalStoreError('Die Serverbindung hat sich geändert.')
    with _db(store,lock,is_current) as db:
        if _preview(db,normalized,actor)!=reviewed['preview']:raise LocalStoreError('Die lokale Mitarbeiteränderung hat sich geändert.')
        _identity_employee_check(db,reviewed['preview'],reviewed['remote'],accept_server)
    command('-action','abandon-employee-event','-payload',_json({'operation':reviewed['operation'],'remote':reviewed['remote']}))
    if inspect_employee_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich nach der Serversperre geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,normalized,actor)
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Mitarbeiteränderung hat sich geändert.')
            remote=reviewed['remote'];current=preview['local'];local=_identity_employee_check(db,preview,remote,accept_server)
            operations=[{'operationId':op,'action':action,'employee':json.loads(value),'expected':json.loads(expected) if expected else None} for op,action,value,expected in db.execute('SELECT operation_id,action,employee_json,expected_json FROM linux_employee_sync_outbox WHERE employee_id=? ORDER BY seq',(local,))]
            db.execute('INSERT INTO linux_employee_sync_resolutions(employee_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',_json(current),_json(remote),_json(operations)))
            db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='employee' AND local_id=? AND external_id=?",(remote['id'],local,current['id']))
            db.execute('INSERT INTO linux_employee_sync_shadow VALUES(?,?)',(local,_json(remote)))
            db.execute('DELETE FROM linux_employee_sync_outbox WHERE employee_id=?',(local,))
            if accept_server:
                db.execute('UPDATE linux_employee_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE employees SET name=?,role=?,department=?,presence=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['role'],remote['department'],remote['presence'],int(remote['active']),local))
                db.execute('UPDATE linux_employee_sync_guard SET suppressed=0 WHERE singleton=1')
            else:
                desired={**current,'id':remote['id'],'accountEmployeeId':remote['accountEmployeeId']}
                if desired!=remote:
                    action='archive' if remote['active'] and not desired['active'] else 'update'
                    db.execute('INSERT INTO linux_employee_sync_outbox(operation_id,action,employee_id,employee_json,expected_json,actor_id) VALUES(?,?,?,?,?,0)',(uuid.uuid4().hex,action,local,_json(desired),_json(remote)))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','employees.sync.identity_mapped',str(local)+':'+remote['id']))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Mitarbeiterzuordnung konnte nicht gespeichert werden: '+str(exc)) from exc


def resolve_employee_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    if reviewed.get('identityConflict'):return _resolve_employee_identity(store,lock,is_current,client,url,token,ca,reviewed,accept_server)
    fresh=inspect_employee_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed: raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']: raise LocalStoreError('Die lokale Mitarbeiteränderung hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId']; remote=reviewed['remote']; current=preview['local']
            if accept_server and not remote['active'] and db.execute('SELECT 1 FROM accounts WHERE employee_id=? AND enabled=1',(local,)).fetchone():
                raise LocalStoreError('Ein Mitarbeiter mit aktivem lokalem Konto kann nicht durch den Serverstand archiviert werden.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_employee_sync_resolutions (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, choice TEXT NOT NULL, local_json TEXT NOT NULL, remote_json TEXT NOT NULL, previous_operations TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            operations=[{'operationId':op,'action':action,'employee':json.loads(data),'expected':json.loads(expected) if expected else None}
                for op,action,data,expected in db.execute('SELECT operation_id,action,employee_json,expected_json FROM linux_employee_sync_outbox WHERE employee_id=? ORDER BY seq',(local,))]
            db.execute('INSERT INTO linux_employee_sync_resolutions(employee_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',
                (local,'server' if accept_server else 'local',_json(current),_json(remote),_json(operations)))
            if accept_server:
                db.execute('UPDATE linux_employee_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE employees SET name=?,role=?,department=?,presence=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                    (remote['name'],remote['role'],remote['department'],remote['presence'],int(remote['active']),local))
                db.execute('UPDATE linux_employee_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('INSERT INTO linux_employee_sync_shadow VALUES(?,?) ON CONFLICT(employee_id) DO UPDATE SET employee_json=excluded.employee_json',(local,_json(remote)))
                db.execute('DELETE FROM linux_employee_sync_outbox WHERE employee_id=?',(local,))
            else:
                # Preserve only the latest local state, but use the reviewed server
                # account link and baseline. A fresh operation ID prevents a retry
                # receipt from accidentally accepting different content.
                desired={**current,'accountEmployeeId':remote['accountEmployeeId']}
                action='archive' if remote['active'] and not desired['active'] else 'update'
                db.execute('DELETE FROM linux_employee_sync_outbox WHERE employee_id=? AND operation_id<>?',(local,preview['operationId']))
                db.execute('UPDATE linux_employee_sync_outbox SET operation_id=?,action=?,employee_json=?,expected_json=?,actor_id=0 WHERE operation_id=?',
                    (uuid.uuid4().hex,action,_json(desired),_json(remote),preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','employees.sync.accepted_server' if accept_server else 'employees.sync.rebased',str(local)))
    except (sqlite3.Error,ValueError,KeyError) as exc:
        raise LocalStoreError('Mitarbeiterkonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def employee_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_employee_sync_config'").fetchone()
            if not exists:return None
            row=db.execute('SELECT server_url,server_actor FROM linux_employee_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:
        raise LocalStoreError('Mitarbeiterkonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

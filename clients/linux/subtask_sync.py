"""Durable subtask synchronization coordinated with the task outbox."""
import json
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account,local_id
from local_store import LocalStoreError
from room_sync import RoomSyncError

def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def _expr(prefix,active):
    return "json_object('id',l.external_id,'taskId',"+prefix+".task_id,'title',"+prefix+".title,'status',"+prefix+".status,'assigneeId',"+prefix+".assignee_id,'dueAt',"+prefix+".due_at,'active',json("+active+"))"

def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1))')
    db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,subtask_id INTEGER NOT NULL,subtask_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_shadow(subtask_id INTEGER PRIMARY KEY,subtask_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_subtask_sync_guard VALUES(1,0)')
    if db.execute('SELECT 1 FROM linux_subtask_sync_meta').fetchone():return
    db.execute('INSERT INTO linux_subtask_sync_meta VALUES(1)')
    new=_expr('NEW',"CASE WHEN NEW.active THEN 'true' ELSE 'false' END");old=_expr('OLD',"CASE WHEN OLD.active THEN 'true' ELSE 'false' END");create=_expr('NEW',"'true'")
    db.execute('''CREATE TRIGGER linux_subtask_sync_insert AFTER INSERT ON task_subtasks WHEN (SELECT suppressed FROM linux_subtask_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'subtask',client_id||':subtask:'||NEW.id,NEW.id FROM linux_task_sync_config;
      INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+create+''',NULL FROM sync_object_links l WHERE object_type='subtask' AND local_id=NEW.id;
      INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+create+''' FROM sync_object_links l WHERE object_type='subtask' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_subtask_sync_update AFTER UPDATE OF task_id,title,status,assignee_id,due_at,active ON task_subtasks
      WHEN (SELECT suppressed FROM linux_subtask_sync_guard)=0 AND (OLD.task_id IS NOT NEW.task_id OR OLD.title IS NOT NEW.title OR OLD.status IS NOT NEW.status OR OLD.assignee_id IS NOT NEW.assignee_id OR OLD.due_at IS NOT NEW.due_at OR OLD.active IS NOT NEW.active)
      BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'subtask',client_id||':subtask:'||NEW.id,NEW.id FROM linux_task_sync_config;
      INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='subtask' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'subtask',client_id||':subtask:'||s.id,s.id FROM task_subtasks s CROSS JOIN linux_task_sync_config")
    rows=db.execute("SELECT s.id,l.external_id,s.task_id,s.title,s.status,s.assignee_id,s.due_at,s.active FROM task_subtasks s JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=s.id ORDER BY s.id").fetchall()
    for local,external,parent,title,status,assignee,due,active in rows:
        value={'id':external,'taskId':parent,'title':title,'status':status,'assigneeId':assignee,'dueAt':due,'active':True}
        db.execute("INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,encode(value)))
        if not active:db.execute("INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**value,'active':False}),encode(value)))


def wire(db,value):
    parent=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(value['taskId'],)).fetchone()
    if not parent:raise LocalStoreError('Für die Hauptaufgabe fehlt die Synchronisationskennung.')
    return {**value,'taskId':parent[0],'assigneeId':account(db,value['assigneeId']) if value['assigneeId'] else 0}


def drain(store,lock,is_current,command,client_id,url,actor):
    sent=0
    while True:
        with _db(store,lock,is_current) as db:
            if db.execute('SELECT client_id,server_url,server_actor FROM linux_task_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Serverkonto.')
            # A child waits until the parent has been acknowledged. Preserve
            # each child's operation order even if other parents are not ready.
            row=db.execute("""SELECT o.seq,o.operation_id,o.action,o.subtask_id,o.subtask_json,o.expected_json,o.wire_json
              FROM linux_subtask_sync_outbox o JOIN task_subtasks s ON s.id=o.subtask_id
              JOIN linux_task_sync_shadow p ON p.task_id=s.task_id
              WHERE o.seq=(SELECT min(x.seq) FROM linux_subtask_sync_outbox x WHERE x.subtask_id=o.subtask_id)
              ORDER BY o.seq LIMIT 1""").fetchone()
            if row:
                seq,op,action,local,payload,expected,frozen=row
                data={'clientId':client_id,'operationId':op,'action':action,'subtask':wire(db,json.loads(payload)),'expected':wire(db,json.loads(expected)) if expected else None}
                if frozen is not None and json.loads(frozen)!=data:raise LocalStoreError('Eine Mitarbeiter- oder Aufgabenzuordnung einer ausstehenden Unteraufgabe hat sich geändert.')
                if frozen is None:db.execute('UPDATE linux_subtask_sync_outbox SET wire_json=? WHERE seq=?',(encode(data),seq))
        if not row:return sent
        try:
            command('-action','sync-subtask-event','-payload',encode(data))
        except RoomSyncError as exc:
            # If a server completed the parent while an unsent local child was
            # still open, an explicitly completed local child can be represented
            # by its final state. A fresh operation ID is required after 409.
            if '409' not in str(exc) or action!='create':raise
            remote_children=command('-action','sync-subtasks');remote_tasks=command('-action','sync-tasks')
            if not isinstance(remote_children,list) or not isinstance(remote_tasks,list):raise
            if any(isinstance(v,dict) and v.get('id')==data['subtask']['id'] for v in remote_children):raise
            parents=[v for v in remote_tasks if isinstance(v,dict) and v.get('id')==data['subtask']['taskId']]
            if len(parents)!=1 or not parents[0].get('active') or parents[0].get('status')!='done':raise
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT wire_json FROM linux_subtask_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(data),):raise LocalStoreError('Die Unteraufgabenwarteschlange wurde geändert.')
                if db.execute('SELECT 1 FROM linux_subtask_sync_outbox WHERE subtask_id=? AND seq<>? AND wire_json IS NOT NULL',(local,seq)).fetchone():raise LocalStoreError('Eine spätere Unteraufgabenänderung wurde bereits übertragen.')
                current=db.execute('SELECT task_id,title,status,assignee_id,due_at,active FROM task_subtasks WHERE id=?',(local,)).fetchone()
                if not current or current[2] not in ('done','cancelled'):raise exc
                parent,title,status,assignee,due,active=current
                final={'id':data['subtask']['id'],'taskId':parent,'title':title,'status':status,'assigneeId':assignee,'dueAt':due,'active':True}
                if wire(db,final)['taskId']!=data['subtask']['taskId']:raise LocalStoreError('Die Hauptaufgabe wurde geändert.')
                previous=[list(v) for v in db.execute('SELECT seq,operation_id,action,subtask_json,expected_json,wire_json FROM linux_subtask_sync_outbox WHERE subtask_id=? ORDER BY seq',(local,))]
                db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_resolutions(id INTEGER PRIMARY KEY,subtask_id INTEGER NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                db.execute('INSERT INTO linux_subtask_sync_resolutions(subtask_id,previous_operations) VALUES(?,?)',(local,encode(previous)))
                db.execute('DELETE FROM linux_subtask_sync_outbox WHERE subtask_id=? AND seq<>?',(local,seq))
                db.execute('UPDATE linux_subtask_sync_outbox SET operation_id=?,subtask_json=?,expected_json=NULL,wire_json=NULL WHERE seq=?',(uuid.uuid4().hex,encode(final),seq))
                if not active:db.execute("INSERT INTO linux_subtask_sync_outbox(operation_id,action,subtask_id,subtask_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**final,'active':False}),encode(final)))
                db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','subtasks.sync.coalesced',?)",(str(local),))
            continue
        with _db(store,lock,is_current) as db:
            if db.execute('SELECT wire_json FROM linux_subtask_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(data),):raise LocalStoreError('Die Unteraufgabenwarteschlange wurde geändert.')
            db.execute('INSERT INTO linux_subtask_sync_shadow VALUES(?,?) ON CONFLICT(subtask_id) DO UPDATE SET subtask_json=excluded.subtask_json',(local,encode(data['subtask'])))
            db.execute('DELETE FROM linux_subtask_sync_outbox WHERE seq=?',(seq,));sent+=1


def pull(store,lock,is_current,command):
    snapshot=command('-action','sync-subtasks')
    if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Unteraufgabenliste vom Server.')
    received=0
    with _db(store,lock,is_current) as db:
        db.execute('UPDATE linux_subtask_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
        for remote in snapshot:
            if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','taskId','title','status')) or remote['id'] in seen or remote['status'] not in ('open','in_progress','done','cancelled') or type(remote.get('assigneeId')) is not int or remote['assigneeId']<0 or type(remote.get('dueAt')) is not int or remote['dueAt']<0 or type(remote.get('active')) is not bool:raise LocalStoreError('Ungültige Unteraufgabe im Serverbestand.')
            seen.add(remote['id'])
            parent=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_task_sync_shadow h ON h.task_id=l.local_id WHERE l.object_type='task' AND l.external_id=?",(remote['taskId'],)).fetchone()
            if not parent:continue
            parent=parent[0];assignee=local_id(db,remote['assigneeId']) if remote['assigneeId'] else 0
            if assignee is None:raise LocalStoreError('Für eine Unteraufgabe fehlt eine eindeutige Mitarbeiterzuordnung: '+remote['title'])
            row=db.execute("""SELECT s.id,s.task_id,s.title,s.status,s.assignee_id,s.due_at,s.active,h.subtask_json
                FROM sync_object_links l JOIN task_subtasks s ON s.id=l.local_id
                LEFT JOIN linux_subtask_sync_shadow h ON h.subtask_id=s.id WHERE l.object_type='subtask' AND l.external_id=?""",(remote['id'],)).fetchone()
            if row:
                local,task,title,status,assigned,due,active,shadow=row
                if db.execute('SELECT 1 FROM linux_subtask_sync_outbox WHERE subtask_id=?',(local,)).fetchone():continue
                current=wire(db,{'id':remote['id'],'taskId':task,'title':title,'status':status,'assigneeId':assigned,'dueAt':due,'active':bool(active)})
                if not shadow or current!=json.loads(shadow):raise LocalStoreError('Unteraufgabenkonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+title)
                if current!=remote:
                    db.execute('UPDATE task_subtasks SET task_id=?,title=?,status=?,assignee_id=?,due_at=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(parent,remote['title'],remote['status'],assignee,remote['dueAt'],int(remote['active']),local));received+=1
            else:
                local=db.execute('INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at,active) VALUES(?,?,?,?,?,?)',(parent,remote['title'],remote['status'],assignee,remote['dueAt'],int(remote['active']))).lastrowid
                db.execute("INSERT INTO sync_object_links VALUES('subtask',?,?)",(remote['id'],local));received+=1
            db.execute('INSERT INTO linux_subtask_sync_shadow VALUES(?,?) ON CONFLICT(subtask_id) DO UPDATE SET subtask_json=excluded.subtask_json',(local,encode(remote)))
        for (external,) in db.execute("SELECT l.external_id FROM linux_subtask_sync_shadow s JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=s.subtask_id"):
            if external not in seen:raise LocalStoreError('Eine zuvor synchronisierte Unteraufgabe fehlt auf dem Server.')
        db.execute('UPDATE linux_subtask_sync_guard SET suppressed=0 WHERE singleton=1')
        db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','subtasks.sync.pulled',?)",(str(received),))
    return received


def _preview(db,url,actor):
    config=db.execute('SELECT server_url,server_actor FROM linux_task_sync_config').fetchone()
    if config!=(url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Server oder Serverkonto.')
    row=db.execute("""SELECT o.seq,o.operation_id,s.id,l.external_id,s.task_id,s.title,s.status,s.assignee_id,s.due_at,s.active,h.subtask_json
        FROM linux_subtask_sync_outbox o JOIN task_subtasks s ON s.id=o.subtask_id
        JOIN linux_task_sync_shadow parent ON parent.task_id=s.task_id
        JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=s.id
        LEFT JOIN linux_subtask_sync_shadow h ON h.subtask_id=s.id
        WHERE o.seq=(SELECT min(x.seq) FROM linux_subtask_sync_outbox x WHERE x.subtask_id=o.subtask_id)
        ORDER BY o.seq LIMIT 1""").fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Unteraufgabenänderung mit bestätigter Hauptaufgabe.')
    seq,operation,local,external,parent,title,status,assignee,due,active,shadow=row
    current={'id':external,'taskId':parent,'title':title,'status':status,'assigneeId':assignee,'dueAt':due,'active':bool(active)}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,subtask_json,expected_json,wire_json FROM linux_subtask_sync_outbox WHERE subtask_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'local':current,'localWire':wire(db,current),'shadow':json.loads(shadow) if shadow else None,'pending':pending}


def _validate_remote(value):
    if not isinstance(value,dict) or any(not isinstance(value.get(k),str) or not value[k].strip() or '\0' in value[k] for k in ('id','taskId','title','status')) or value['status'] not in ('open','in_progress','done','cancelled') or type(value.get('assigneeId')) is not int or value['assigneeId']<0 or type(value.get('dueAt')) is not int or value['dueAt']<0 or type(value.get('active')) is not bool:
        raise LocalStoreError('Ungültige Unteraufgabe im Serverbestand.')
    return value


def _validate_subtask_identity_mapping(db,preview,remote):
    config=db.execute('SELECT client_id FROM linux_task_sync_config WHERE singleton=1').fetchone()
    if not config or preview['local']['id']!=config[0]+':subtask:'+str(preview['localId']):raise LocalStoreError('Die lokale Unteraufgabe besitzt bereits eine fremde oder zuvor zugeordnete Kennung.')
    if db.execute("SELECT 1 FROM sync_object_links WHERE object_type='subtask' AND external_id=?",(remote['id'],)).fetchone():raise LocalStoreError('Die Serverunteraufgabe ist bereits einer lokalen Unteraufgabe zugeordnet.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_subtask_sync_config'").fetchone() and db.execute('SELECT 1 FROM desktop_subtask_sync_config LIMIT 1').fetchone():raise LocalStoreError('Diese Datenbank ist auch für den Windows-Abgleich eingerichtet; die Unteraufgabenkennung kann hier nicht sicher zugeordnet werden.')

def inspect_subtask_conflict(store,lock,is_current,client,url,token,ca):
    from room_sync import connect_sync_server
    url,actor,command=connect_sync_server(client,url,token,ca,'subtaskSync','canManageTasks','tasks.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        values=command('-action','sync-subtasks')
        if not isinstance(values,list):raise LocalStoreError('Ungültige Unteraufgabenliste vom Server.')
        matches=[_validate_remote(v) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        identity_conflict=False
        if not matches:
            pending=preview['pending']
            if preview['shadow'] is not None or not pending or pending[0][2]!='create' or any(op[5] is not None for op in pending[1:]):raise LocalStoreError('Diese Unteraufgabenanlage wurde bereits bestätigt oder eine spätere Übertragung vorbereitet.')
            frozen=json.loads(pending[0][5]) if pending[0][5] is not None else None
            if frozen is not None and (not isinstance(frozen,dict) or frozen.get('action')!='create' or frozen.get('expected') is not None or frozen.get('operationId')!=pending[0][1] or not isinstance(frozen.get('subtask'),dict) or frozen['subtask'].get('id')!=preview['local']['id'] or frozen['subtask'].get('taskId')!=preview['localWire']['taskId']):raise LocalStoreError('Die vorbereitete Unteraufgabenübertragung ist ungültig.')
            matches=[_validate_remote(v) for v in values if isinstance(v,dict) and v.get('taskId')==preview['localWire']['taskId'] and isinstance(v.get('title'),str) and v['title'].strip().casefold()==preview['local']['title'].strip().casefold() and v.get('id')!=preview['local']['id']]
            if len(matches)!=1:raise LocalStoreError('Für diese Hauptaufgabe gibt es keine eindeutig gleichnamige Serverunteraufgabe zur Zuordnung.')
            with _db(store,lock,is_current) as db:
                if _preview(db,url,actor)!=preview:raise LocalStoreError('Die lokale Unteraufgabe hat sich geändert. Bitte erneut prüfen.')
                _validate_subtask_identity_mapping(db,preview,matches[0])
            identity_conflict=True
        if len(matches)!=1:raise LocalStoreError('Die Serverunteraufgabe ist nicht eindeutig.')
        if matches[0]['taskId']!=preview['localWire']['taskId']:raise LocalStoreError('Die Serverunteraufgabe gehört zu einer anderen Hauptaufgabe.')
        result={'preview':preview,'remote':matches[0],'url':url,'actor':actor}
        if identity_conflict:
            result['identityConflict']=True
            if frozen is not None:result['preparedOperation']=frozen
        return result
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Unteraufgabenkonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_subtask_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    fresh=inspect_subtask_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    if reviewed.get('preparedOperation') is not None:
        from room_sync import connect_sync_server
        normalized,actor,command=connect_sync_server(client,url,token,ca,'subtaskIdentityReconciliation','canManageTasks','tasks.manage')
        if normalized!=reviewed['url'] or actor!=reviewed['actor']:raise LocalStoreError('Die Serverbindung hat sich geändert.')
        with _db(store,lock,is_current) as db:
            if _preview(db,normalized,actor)!=reviewed['preview']:raise LocalStoreError('Die lokale Unteraufgabe hat sich geändert. Bitte erneut prüfen.')
            _validate_subtask_identity_mapping(db,reviewed['preview'],reviewed['remote'])
        command('-action','abandon-subtask-event','-payload',encode({'operation':reviewed['preparedOperation'],'remote':reviewed['remote']}))
        if inspect_subtask_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich nach der Serversperre geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Unteraufgabe oder Warteschlange hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];assigned=local_id(db,remote['assigneeId']) if remote['assigneeId'] else 0
            if reviewed.get('identityConflict'):
                if preview['shadow'] is not None or not preview['pending'] or preview['pending'][0][2]!='create' or any(op[5] is not None for op in preview['pending'][1:]) or (preview['pending'][0][5] is not None and reviewed.get('preparedOperation') is None):raise LocalStoreError('Die Unteraufgabenanlage wurde zwischenzeitlich vorbereitet oder bestätigt.')
                _validate_subtask_identity_mapping(db,preview,remote)
                changed=db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='subtask' AND local_id=? AND external_id=?",(remote['id'],local,current['id'])).rowcount
                if changed!=1:raise LocalStoreError('Die lokale Unteraufgabenkennung hat sich geändert.')
                current={**current,'id':remote['id']}
            if assigned is None:raise LocalStoreError('Die zuständige Serverperson besitzt keine eindeutige lokale Zuordnung.')
            parent=db.execute('SELECT status,active FROM tasks WHERE id=?',(current['taskId'],)).fetchone()
            if not parent:raise LocalStoreError('Die lokale Hauptaufgabe fehlt.')
            if accept_server and parent[1] and parent[0]=='done' and remote['active'] and remote['status'] not in ('done','cancelled'):
                raise LocalStoreError('Eine offene Serverunteraufgabe kann nicht für eine lokal abgeschlossene Hauptaufgabe übernommen werden.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_subtask_sync_conflicts(id INTEGER PRIMARY KEY,subtask_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_subtask_sync_conflicts(subtask_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',encode(current),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_subtask_sync_shadow VALUES(?,?) ON CONFLICT(subtask_id) DO UPDATE SET subtask_json=excluded.subtask_json',(local,encode(remote)))
            if accept_server:
                db.execute('UPDATE linux_subtask_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE task_subtasks SET title=?,status=?,assignee_id=?,due_at=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['title'],remote['status'],assigned,remote['dueAt'],int(remote['active']),local))
                db.execute('UPDATE linux_subtask_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM linux_subtask_sync_outbox WHERE subtask_id=?',(local,))
            else:
                expected={**remote,'taskId':current['taskId'],'assigneeId':assigned}
                action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_subtask_sync_outbox WHERE subtask_id=? AND seq<>?',(local,preview['seq']))
                db.execute('UPDATE linux_subtask_sync_outbox SET operation_id=?,action=?,subtask_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,encode(current),encode(expected),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync',('subtasks.sync.identity_mapped' if reviewed.get('identityConflict') else ('subtasks.sync.accepted_server' if accept_server else 'subtasks.sync.rebased')),str(local)+(':'+preview['local']['id']+' -> '+remote['id'] if reviewed.get('identityConflict') else '')))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Unteraufgabenkonflikt konnte nicht übernommen werden: '+str(exc)) from exc

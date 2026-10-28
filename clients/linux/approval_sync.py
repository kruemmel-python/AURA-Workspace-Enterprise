"""Synchronize task approvals using each participant's authenticated identity."""
import json
import sqlite3
import uuid
from leave_sync import transaction,account,local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server

class ApprovalPendingError(LocalStoreError):
    """An approval is waiting for its task or an earlier request."""
    def __init__(self,message,received=0):
        super().__init__(message);self.received=received


def encode(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def setup(db,url):
    db.execute('CREATE TABLE IF NOT EXISTS linux_approval_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_approval_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,approval_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,approval_json TEXT NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_approval_sync_shadow(approval_id INTEGER PRIMARY KEY,approval_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_approval_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_approval_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url FROM linux_approval_sync_config').fetchone()
    if config:
        if config[1]!=url:raise LocalStoreError('Die Freigabewarteschlange ist an einen anderen Server gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_approval_sync_config VALUES(1,?,?)',(client,url))
    request="json_object('id',l.external_id,'taskId',NEW.task_id,'requestedBy',NEW.requested_by,'approverId',NEW.approver_id,'status','pending','decidedBy',0,'active',json('true'))"
    decision="json_object('id',l.external_id,'taskId',NEW.task_id,'requestedBy',NEW.requested_by,'approverId',NEW.approver_id,'status',NEW.status,'decidedBy',NEW.decided_by,'active',json('true'))"
    db.execute('''CREATE TRIGGER linux_approval_sync_insert AFTER INSERT ON task_approvals WHEN (SELECT suppressed FROM linux_approval_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'task_approval',client_id||':task_approval:'||NEW.id,NEW.id FROM linux_approval_sync_config;
      INSERT INTO linux_approval_sync_outbox(operation_id,action,approval_id,actor_id,approval_json) SELECT lower(hex(randomblob(16))),'request',NEW.id,NEW.requested_by,'''+request+''' FROM sync_object_links l WHERE object_type='task_approval' AND local_id=NEW.id;
      INSERT INTO linux_approval_sync_outbox(operation_id,action,approval_id,actor_id,approval_json) SELECT lower(hex(randomblob(16))),'decide',NEW.id,NEW.decided_by,'''+decision+''' FROM sync_object_links l WHERE object_type='task_approval' AND local_id=NEW.id AND NEW.status IN ('approved','rejected');
    END''')
    db.execute('''CREATE TRIGGER linux_approval_sync_decide AFTER UPDATE OF status,decided_by ON task_approvals WHEN (SELECT suppressed FROM linux_approval_sync_guard)=0 AND OLD.status='pending' AND NEW.status IN ('approved','rejected') BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'task_approval',client_id||':task_approval:'||NEW.id,NEW.id FROM linux_approval_sync_config;
      INSERT INTO linux_approval_sync_outbox(operation_id,action,approval_id,actor_id,approval_json) SELECT lower(hex(randomblob(16))),'decide',NEW.id,NEW.decided_by,'''+decision+''' FROM sync_object_links l WHERE object_type='task_approval' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'task_approval',?||':task_approval:'||id,id FROM task_approvals",(client,))
    rows=db.execute("SELECT a.id,l.external_id,a.task_id,a.requested_by,a.approver_id,a.status,COALESCE(a.decided_by,0) FROM task_approvals a JOIN sync_object_links l ON l.object_type='task_approval' AND l.local_id=a.id ORDER BY a.id").fetchall()
    for local,external,task,requester,approver,status,decider in rows:
        value={'id':external,'taskId':task,'requestedBy':requester,'approverId':approver,'status':'pending','decidedBy':0,'active':True}
        db.execute("INSERT INTO linux_approval_sync_outbox(operation_id,action,approval_id,actor_id,approval_json) VALUES(?,'request',?,?,?)",(uuid.uuid4().hex,local,requester,encode(value)))
        if status in ('approved','rejected'):db.execute("INSERT INTO linux_approval_sync_outbox(operation_id,action,approval_id,actor_id,approval_json) VALUES(?,'decide',?,?,?)",(uuid.uuid4().hex,local,decider,encode({**value,'status':status,'decidedBy':decider})))
        elif status!='pending':raise LocalStoreError('Ein vorhandener Freigabestatus wird nicht unterstützt.')
    return client

def wire(db,value):
    task=db.execute("SELECT l.external_id FROM sync_object_links l JOIN linux_task_sync_shadow s ON s.task_id=l.local_id WHERE l.object_type='task' AND l.local_id=?",(value['taskId'],)).fetchone()
    if not task:raise LocalStoreError('Die Aufgabe der Freigabe ist noch nicht synchronisiert.')
    return {**value,'taskId':task[0],'requestedBy':str(account(db,value['requestedBy'])),'approverId':str(account(db,value['approverId'])),'decidedBy':str(account(db,value['decidedBy'])) if value['decidedBy'] else ''}

def validate(remote,actor):
    if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','taskId','requestedBy','approverId','status')) or remote['status'] not in ('pending','approved','rejected') or type(remote.get('active')) is not bool or not remote['active'] or not isinstance(remote.get('decidedBy'),str):raise LocalStoreError('Ungültige Freigabe vom Server.')
    for key in ('requestedBy','approverId'):
        try:num=int(remote[key])
        except ValueError:raise LocalStoreError('Freigabe verwendet eine nicht zugeordnete Serverkonto-Kennung.')
        if num<=0 or str(num)!=remote[key]:raise LocalStoreError('Ungültige Freigabekennung vom Server.')
    if remote['requestedBy']==remote['approverId'] or str(actor) not in (remote['requestedBy'],remote['approverId']) or (remote['status']=='pending' and remote['decidedBy']!='') or (remote['status']!='pending' and remote['decidedBy']!=remote['approverId']):raise LocalStoreError('Ungültige Freigabeentscheidung vom Server.')
    return remote

def sync_approvals(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'approvalSync','canReadTasks','tasks.read')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            store.approvals()
            if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_task_sync_config'").fetchone() or db.execute('SELECT server_url FROM linux_task_sync_config').fetchone()!=(url,):raise LocalStoreError('Bitte zuerst Aufgaben für denselben Server abgleichen.')
            client_id=setup(db,url)
        sent=0;waiting=False
        while True:
            with transaction(store,lock,is_current) as (db,current_actor):
                store.approvals()
                if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
                if db.execute('SELECT client_id,server_url FROM linux_approval_sync_config').fetchone()!=(client_id,url):raise LocalStoreError('Die Freigabewarteschlange gehört zu einem anderen Server.')
                row=db.execute('''SELECT o.seq,o.operation_id,o.action,o.approval_id,o.approval_json,o.wire_json
                    FROM linux_approval_sync_outbox o WHERE o.actor_id=?
                    AND NOT EXISTS(SELECT 1 FROM linux_approval_sync_outbox older WHERE older.approval_id=o.approval_id AND older.seq<o.seq)
                    AND EXISTS(SELECT 1 FROM linux_task_sync_shadow ts WHERE ts.task_id=json_extract(o.approval_json,'$.taskId'))
                    ORDER BY o.seq LIMIT 1''',(local_actor,)).fetchone()
                if not row and db.execute('SELECT 1 FROM linux_approval_sync_outbox WHERE actor_id=? LIMIT 1',(local_actor,)).fetchone():
                    waiting=True
                if row:
                    seq,op,action,local,payload,frozen=row
                    desired=wire(db,json.loads(payload));expected={**desired,'status':'pending','decidedBy':''} if action=='decide' else None
                    value={'clientId':client_id,'operationId':op,'action':action,'approval':desired,'expected':expected}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Die Zuordnung einer ausstehenden Freigabe hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_approval_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-approval-event','-payload',encode(value))
            with transaction(store,lock,is_current) as (db,current_actor):
                if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
                if db.execute('SELECT wire_json FROM linux_approval_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),):raise LocalStoreError('Die Freigabewarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_approval_sync_shadow VALUES(?,?) ON CONFLICT(approval_id) DO UPDATE SET approval_json=excluded.approval_json',(local,encode(value['approval'])));db.execute('DELETE FROM linux_approval_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-approvals')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Freigabeliste vom Server.')
        received=0;unmapped=0
        with transaction(store,lock,is_current) as (db,current_actor):
            if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
            db.execute('UPDATE linux_approval_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                validate(remote,actor)
                if remote['id'] in seen:raise LocalStoreError('Doppelte Freigabekennung vom Server.')
                seen.add(remote['id']);task=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_task_sync_shadow s ON s.task_id=l.local_id WHERE l.object_type='task' AND l.external_id=?",(remote['taskId'],)).fetchone()
                requester=local_id(db,int(remote['requestedBy']));approver=local_id(db,int(remote['approverId']));decider=local_id(db,int(remote['decidedBy'])) if remote['decidedBy'] else 0
                if not task or requester is None or approver is None or decider is None:unmapped+=1;continue
                row=db.execute("SELECT a.id,a.task_id,a.requested_by,a.approver_id,a.status,COALESCE(a.decided_by,0),s.approval_json FROM sync_object_links l JOIN task_approvals a ON a.id=l.local_id LEFT JOIN linux_approval_sync_shadow s ON s.approval_id=a.id WHERE l.object_type='task_approval' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,task_id,request_id,approver_id,status,decided,shadow=row
                    if db.execute('SELECT 1 FROM linux_approval_sync_outbox WHERE approval_id=?',(local,)).fetchone():continue
                    current=wire(db,{'id':remote['id'],'taskId':task_id,'requestedBy':request_id,'approverId':approver_id,'status':status,'decidedBy':decided,'active':True})
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Freigabekonflikt: Der lokale Stand weicht vom gemeinsamen Ausgangszustand ab.')
                    if current!=remote:db.execute('UPDATE task_approvals SET task_id=?,requested_by=?,approver_id=?,status=?,decided_by=?,decided_at=CASE WHEN ?=0 THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id=?',(task[0],requester,approver,remote['status'],decider,decider,local));received+=1
                else:
                    local=db.execute('INSERT INTO task_approvals(task_id,requested_by,approver_id,status,decided_by,decided_at) VALUES(?,?,?,?,?,CASE WHEN ?=0 THEN NULL ELSE CURRENT_TIMESTAMP END)',(task[0],requester,approver,remote['status'],decider,decider)).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('task_approval',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_approval_sync_shadow VALUES(?,?) ON CONFLICT(approval_id) DO UPDATE SET approval_json=excluded.approval_json',(local,encode(remote)))
            db.execute('UPDATE linux_approval_sync_guard SET suppressed=0 WHERE singleton=1')
        if waiting:raise ApprovalPendingError('Eine Aufgabe oder die ältere Anforderung muss zuerst synchronisiert werden. Andere verfügbare Änderungen wurden abgeglichen.',received)
        return {'sent':sent,'received':received,'unmapped':unmapped}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Freigabeabgleich fehlgeschlagen: '+str(exc)) from exc


def _conflict_preview(db,local_actor,url,actor):
    if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
    config=db.execute('SELECT server_url FROM linux_approval_sync_config WHERE singleton=1').fetchone()
    if not config or config[0]!=url:raise LocalStoreError('Die Freigabewarteschlange gehört zu einem anderen Server.')
    row=db.execute('''SELECT o.seq,o.operation_id,o.action,a.id,l.external_id,a.task_id,a.requested_by,a.approver_id,a.status,COALESCE(a.decided_by,0)
      FROM linux_approval_sync_outbox o JOIN task_approvals a ON a.id=o.approval_id
      JOIN sync_object_links l ON l.object_type='task_approval' AND l.local_id=a.id
      WHERE o.actor_id=? ORDER BY o.seq LIMIT 1''',(local_actor,)).fetchone()
    if not row:raise LocalStoreError('Es gibt keine eigene ausstehende Freigabeänderung.')
    seq,operation,action,local,external,task,requester,approver,status,decider=row
    pending=db.execute('SELECT seq,operation_id,action,actor_id,approval_json,wire_json FROM linux_approval_sync_outbox WHERE approval_id=? ORDER BY seq',(local,)).fetchall()
    if any(v[3]!=local_actor for v in pending):raise LocalStoreError('Für diese Freigabe stehen Änderungen einer anderen Person aus. Diese müssen zuerst abgeglichen werden.')
    if action!='decide':raise LocalStoreError('Nur widersprüchliche Entscheidungen können durch den Serverstand ersetzt werden.')
    current=wire(db,{'id':external,'taskId':task,'requestedBy':requester,'approverId':approver,'status':status,'decidedBy':decider,'active':True})
    return {'seq':seq,'operationId':operation,'action':action,'localId':local,'local':current,'pending':[list(v) for v in pending]}


def inspect_approval_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'approvalSync','canReadTasks','tasks.read')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            store.approvals();preview=_conflict_preview(db,local_actor,url,actor)
        snapshot=command('-action','sync-approvals')
        if not isinstance(snapshot,list):raise LocalStoreError('Ungültige Freigabeliste vom Server.')
        matches=[v for v in snapshot if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für diese Freigabe ist kein eindeutiger Serverstand sichtbar.')
        remote=validate(matches[0],actor)
        if remote['status']=='pending' or any(remote[k]!=preview['local'][k] for k in ('id','taskId','requestedBy','approverId','active')):
            raise LocalStoreError('Der Serverstand ist keine passende abgeschlossene Freigabe. Bitte zuerst den Abgleich erneut versuchen.')
        return {'preview':preview,'remote':remote,'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Freigabekonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def accept_server_approval(store,lock,is_current,client,url,token,ca,reviewed):
    fresh=inspect_approval_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            store.approvals();preview=_conflict_preview(db,local_actor,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Freigabeänderung hat sich geändert. Bitte erneut prüfen.')
            remote=reviewed['remote'];decider=local_id(db,int(remote['decidedBy']))
            if decider is None:raise LocalStoreError('Die entscheidende Person ist keinem eindeutigen lokalen Mitarbeiter zugeordnet.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_approval_sync_resolutions (id INTEGER PRIMARY KEY,approval_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_approval_sync_resolutions(approval_id,actor_id,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(preview['localId'],local_actor,encode(preview['local']),encode(remote),encode(preview['pending'])))
            db.execute('UPDATE linux_approval_sync_guard SET suppressed=1 WHERE singleton=1')
            db.execute('UPDATE task_approvals SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=?',(remote['status'],decider,preview['localId']))
            db.execute('UPDATE linux_approval_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute('INSERT INTO linux_approval_sync_shadow VALUES(?,?) ON CONFLICT(approval_id) DO UPDATE SET approval_json=excluded.approval_json',(preview['localId'],encode(remote)))
            db.execute('DELETE FROM linux_approval_sync_outbox WHERE approval_id=? AND actor_id=?',(preview['localId'],local_actor))
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'approval.sync.accepted_server',?)",(str(local_actor),str(preview['localId'])))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Serverfreigabe konnte nicht übernommen werden: '+str(exc)) from exc

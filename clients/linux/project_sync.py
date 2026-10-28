"""Explicit synchronization of local project records with a central server."""
import json
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account,local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server

class ProjectOwnerPendingError(LocalStoreError):
    def __init__(self,message,received=0):
        super().__init__(message);self.received=received

def _only_unassigned_pending(db):
    rows=db.execute('''SELECT o.action,o.wire_json,json_extract(o.project_json,'$.ownerId'),p.owner_id
      FROM linux_project_sync_outbox o LEFT JOIN projects p ON p.id=o.project_id
      WHERE o.seq=(SELECT min(z.seq) FROM linux_project_sync_outbox z WHERE z.project_id=o.project_id)''').fetchall()
    if any(action!='create' or wire is not None or initial!=0 or current!=0 for action,wire,initial,current in rows):
        raise LocalStoreError('Lokale Projektänderungen stehen noch aus.')
    return bool(rows)

def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def _value(prefix,active):
    return "json_object('id',l.external_id,'name',"+prefix+".name,'department',"+prefix+".department,'description',"+prefix+".description,'status',"+prefix+".status,'ownerId',"+prefix+".owner_id,'active',json("+active+"))"

def setup(db,url,actor):
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,project_id INTEGER NOT NULL,project_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_sync_shadow(project_id INTEGER PRIMARY KEY,project_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_project_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_project_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Projektwarteschlange ist an einen anderen Server oder ein anderes Serverkonto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_project_sync_config VALUES(1,?,?,?)',(client,url,actor))
    new=_value('NEW',"CASE WHEN NEW.active THEN 'true' ELSE 'false' END");old=_value('OLD',"CASE WHEN OLD.active THEN 'true' ELSE 'false' END");create=_value('NEW',"'true'")
    db.execute('''CREATE TRIGGER linux_project_sync_insert AFTER INSERT ON projects WHEN (SELECT suppressed FROM linux_project_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'project',client_id||':project:'||NEW.id,NEW.id FROM linux_project_sync_config;
      INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+create+''',NULL FROM sync_object_links l WHERE object_type='project' AND local_id=NEW.id;
      INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+create+''' FROM sync_object_links l WHERE object_type='project' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_project_sync_update AFTER UPDATE OF name,department,description,status,owner_id,active ON projects
      WHEN (SELECT suppressed FROM linux_project_sync_guard)=0 AND (OLD.name IS NOT NEW.name OR OLD.department IS NOT NEW.department OR OLD.description IS NOT NEW.description OR OLD.status IS NOT NEW.status OR OLD.owner_id IS NOT NEW.owner_id OR OLD.active IS NOT NEW.active)
      BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'project',client_id||':project:'||NEW.id,NEW.id FROM linux_project_sync_config;
      INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='project' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'project',?||':project:'||id,id FROM projects",(client,))
    rows=db.execute("SELECT p.id,l.external_id,p.name,p.department,p.description,p.status,p.owner_id,p.active FROM projects p JOIN sync_object_links l ON l.object_type='project' AND l.local_id=p.id ORDER BY p.id").fetchall()
    for local,external,name,department,description,status,owner,active in rows:
        value={'id':external,'name':name,'department':department,'description':description,'status':status,'ownerId':owner,'active':True}
        db.execute("INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,encode(value)))
        if not active:db.execute("INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**value,'active':False}),encode(value)))
    return client

def wire(db,value):return {**value,'ownerId':account(db,value['ownerId'])}

def sync_projects(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'projectSync','canManageProjects','projects.manage')
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_project_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Projektwarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('''SELECT o.seq,o.operation_id,o.action,o.project_id,o.project_json,o.expected_json,o.wire_json
                    FROM linux_project_sync_outbox o JOIN projects p ON p.id=o.project_id
                    WHERE NOT EXISTS(SELECT 1 FROM linux_project_sync_outbox older WHERE older.project_id=o.project_id AND older.seq<o.seq)
                      AND NOT (o.action='create' AND o.wire_json IS NULL AND json_extract(o.project_json,'$.ownerId')=0 AND p.owner_id=0)
                    ORDER BY o.seq LIMIT 1''').fetchone()
                if row:
                    seq,op,action,local,payload,expected,frozen=row
                    original=json.loads(payload)
                    if action=='create' and frozen is None and original['ownerId']==0 and not db.execute('SELECT 1 FROM linux_project_sync_outbox WHERE project_id=? AND wire_json IS NOT NULL',(local,)).fetchone():
                        current=db.execute('SELECT name,department,description,status,owner_id,active FROM projects WHERE id=?',(local,)).fetchone()
                        if current and current[4]>0:
                            name,department,description,status,owner,active=current
                            initial={'id':original['id'],'name':name,'department':department,'description':description,'status':status,'ownerId':owner,'active':True}
                            db.execute('UPDATE linux_project_sync_outbox SET project_json=? WHERE seq=?',(encode(initial),seq))
                            db.execute('DELETE FROM linux_project_sync_outbox WHERE project_id=? AND seq<>?',(local,seq))
                            if not active:db.execute("INSERT INTO linux_project_sync_outbox(operation_id,action,project_id,project_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**initial,'active':False}),encode(initial)))
                            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','projects.sync.coalesced',?)",(str(local),));payload=encode(initial)
                    value={'clientId':client_id,'operationId':op,'action':action,'project':wire(db,json.loads(payload)),'expected':wire(db,json.loads(expected)) if expected else None}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Die Zuordnung eines ausstehenden Projekts hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_project_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-project-event','-payload',encode(value))
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT operation_id,wire_json FROM linux_project_sync_outbox WHERE seq=? AND NOT EXISTS(SELECT 1 FROM linux_project_sync_outbox older WHERE older.project_id=linux_project_sync_outbox.project_id AND older.seq<linux_project_sync_outbox.seq)',(seq,)).fetchone()!=(op,encode(value)):raise LocalStoreError('Die Projektwarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_project_sync_shadow VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET project_json=excluded.project_json',(local,encode(value['project'])))
                db.execute('DELETE FROM linux_project_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-projects')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Projektliste vom Server.')
        received=0
        with _db(store,lock,is_current) as db:
            pending=_only_unassigned_pending(db)
            db.execute('UPDATE linux_project_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','name','department','status')) or not isinstance(remote.get('description'),str) or '\0' in remote['description'] or remote['id'] in seen or remote['status'] not in ('planned','active','on_hold','completed','cancelled') or type(remote.get('ownerId')) is not int or remote['ownerId']<=0 or type(remote.get('active')) is not bool:raise LocalStoreError('Ungültiges Projekt im Serverbestand.')
                seen.add(remote['id']);owner=local_id(db,remote['ownerId'])
                if owner is None:raise LocalStoreError('Für die Serverprojektleitung fehlt eine eindeutige lokale Zuordnung: '+remote['name'])
                row=db.execute("SELECT p.id,p.name,p.department,p.description,p.status,p.owner_id,p.active,s.project_json FROM sync_object_links l JOIN projects p ON p.id=l.local_id LEFT JOIN linux_project_sync_shadow s ON s.project_id=p.id WHERE l.object_type='project' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,name,department,description,status,local_owner,active,shadow=row
                    if db.execute('SELECT 1 FROM linux_project_sync_outbox WHERE project_id=?',(local,)).fetchone():continue
                    current={'id':remote['id'],'name':name,'department':department,'description':description,'status':status,'ownerId':account(db,local_owner),'active':bool(active)}
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Projektkonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+name)
                    if current!=remote:
                        db.execute('UPDATE projects SET name=?,department=?,description=?,status=?,owner_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['department'],remote['description'],remote['status'],owner,int(remote['active']),local));received+=1
                else:
                    local=db.execute('INSERT INTO projects(name,department,description,status,owner_id,active) VALUES(?,?,?,?,?,?)',(remote['name'],remote['department'],remote['description'],remote['status'],owner,int(remote['active']))).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('project',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_project_sync_shadow VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET project_json=excluded.project_json',(local,encode(remote)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_project_sync_shadow s JOIN sync_object_links l ON l.object_type='project' AND l.local_id=s.project_id"):
                if external not in seen:raise LocalStoreError('Ein zuvor synchronisiertes Projekt fehlt auf dem Server.')
            db.execute('UPDATE linux_project_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','projects.sync.pulled',?)",(str(received),))
        if pending:raise ProjectOwnerPendingError('Ein neues Projekt wartet auf die Zuordnung einer Projektleitung. Andere verfügbare Projekte wurden abgeglichen.',received)
        return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Projektabgleich fehlgeschlagen: '+str(exc)) from exc


def _preview(db,url,actor):
    if db.execute('SELECT server_url,server_actor FROM linux_project_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Projektwarteschlange gehört zu einem anderen Server oder Serverkonto.')
    row=db.execute("""SELECT o.seq,o.operation_id,p.id,l.external_id,p.name,p.department,p.description,p.status,p.owner_id,p.active,s.project_json
       FROM linux_project_sync_outbox o JOIN projects p ON p.id=o.project_id JOIN sync_object_links l ON l.object_type='project' AND l.local_id=p.id
       LEFT JOIN linux_project_sync_shadow s ON s.project_id=p.id ORDER BY o.seq LIMIT 1""").fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Projektänderung.')
    seq,operation,local,external,name,department,description,status,owner,active,shadow=row
    value={'id':external,'name':name,'department':department,'description':description,'status':status,'ownerId':owner,'active':bool(active)}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,project_json,expected_json,wire_json FROM linux_project_sync_outbox WHERE project_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'local':value,'localWire':wire(db,value),'shadow':json.loads(shadow) if shadow else None,'pending':pending}

def _validate_remote(remote):
    if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','name','department','status')) or not isinstance(remote.get('description'),str) or '\0' in remote['description'] or remote['status'] not in ('planned','active','on_hold','completed','cancelled') or type(remote.get('ownerId')) is not int or remote['ownerId']<=0 or type(remote.get('active')) is not bool:raise LocalStoreError('Ungültiges Projekt im Serverbestand.')
    return remote

def inspect_project_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'projectSync','canManageProjects','projects.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        values=command('-action','sync-projects')
        if not isinstance(values,list):raise LocalStoreError('Ungültige Projektliste vom Server.')
        matches=[_validate_remote(v) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für dieses Projekt gibt es keinen eindeutig zugeordneten Servereintrag. Gleichnamige, unabhängig angelegte Projekte müssen gesondert zugeordnet werden.')
        return {'preview':preview,'remote':matches[0],'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Projektkonflikt konnte nicht geprüft werden: '+str(exc)) from exc

def resolve_project_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    fresh=inspect_project_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Das lokale Projekt oder die Warteschlange hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];owner=local_id(db,remote['ownerId'])
            if owner is None:raise LocalStoreError('Die Serverprojektleitung besitzt keine eindeutige lokale Zuordnung.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_project_sync_conflicts(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_project_sync_conflicts(project_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',encode(current),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_project_sync_shadow VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET project_json=excluded.project_json',(local,encode(remote)))
            if accept_server:
                db.execute('UPDATE linux_project_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE projects SET name=?,department=?,description=?,status=?,owner_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['department'],remote['description'],remote['status'],owner,int(remote['active']),local))
                db.execute('UPDATE linux_project_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM linux_project_sync_outbox WHERE project_id=?',(local,))
            else:
                expected={**remote,'ownerId':owner};action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_project_sync_outbox WHERE project_id=? AND seq<>?',(local,preview['seq']))
                db.execute('UPDATE linux_project_sync_outbox SET operation_id=?,action=?,project_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,encode(current),encode(expected),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','projects.sync.accepted_server' if accept_server else 'projects.sync.rebased',str(local)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Projektkonflikt konnte nicht übernommen werden: '+str(exc)) from exc

def project_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_project_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_project_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Projektkonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

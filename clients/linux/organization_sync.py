"""Durable synchronization for organizational units."""
import json
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account,local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server


class OrganizationManagerPendingError(LocalStoreError):
    pass


def encode(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def _value(prefix,active):return "json_object('id',l.external_id,'name',"+prefix+".name,'code',"+prefix+".code,'managerId',"+prefix+".manager_id,'active',json("+active+"))"
def wire(db,value):
    manager=value['managerId']
    if manager and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_employee_sync_outbox'").fetchone() and db.execute('SELECT 1 FROM linux_employee_sync_outbox WHERE employee_id=? LIMIT 1',(manager,)).fetchone():
        if not db.execute('SELECT 1 FROM linux_employee_sync_shadow WHERE employee_id=?',(manager,)).fetchone():raise OrganizationManagerPendingError('Die Organisationsleitung wartet auf den Mitarbeiterabgleich.')
    return {**value,'managerId':account(db,manager) if manager else 0}
def local_manager(db,server):return local_id(db,server) if server else 0

def validate(v):
    if not isinstance(v,dict) or any(not isinstance(v.get(k),str) or not v[k].strip() or '\0' in v[k] for k in ('id','name','code')) or type(v.get('managerId')) is not int or v['managerId']<0 or type(v.get('active')) is not bool:raise LocalStoreError('Ungültige Organisationseinheit vom Server.')
    return v

def setup(db,url,actor):
    db.execute('CREATE TABLE IF NOT EXISTS linux_organization_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_organization_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,organization_id INTEGER NOT NULL,organization_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_organization_sync_shadow(organization_id INTEGER PRIMARY KEY,organization_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_organization_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_organization_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_organization_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Organisationswarteschlange ist an einen anderen Server oder ein anderes Konto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_organization_sync_config VALUES(1,?,?,?)',(client,url,actor))
    new=_value('NEW',"CASE WHEN NEW.active THEN 'true' ELSE 'false' END");old=_value('OLD',"CASE WHEN OLD.active THEN 'true' ELSE 'false' END");create=_value('NEW',"'true'")
    db.execute('''CREATE TRIGGER linux_organization_sync_insert AFTER INSERT ON organization_units WHEN (SELECT suppressed FROM linux_organization_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'organization',client_id||':organization:'||NEW.id,NEW.id FROM linux_organization_sync_config;
      INSERT INTO linux_organization_sync_outbox(operation_id,action,organization_id,organization_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+create+''',NULL FROM sync_object_links l WHERE object_type='organization' AND local_id=NEW.id;
      INSERT INTO linux_organization_sync_outbox(operation_id,action,organization_id,organization_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+create+''' FROM sync_object_links l WHERE object_type='organization' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_organization_sync_update AFTER UPDATE OF name,code,manager_id,active ON organization_units WHEN (SELECT suppressed FROM linux_organization_sync_guard)=0 AND (OLD.name IS NOT NEW.name OR OLD.code IS NOT NEW.code OR OLD.manager_id IS NOT NEW.manager_id OR OLD.active IS NOT NEW.active) BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'organization',client_id||':organization:'||NEW.id,NEW.id FROM linux_organization_sync_config;
      INSERT INTO linux_organization_sync_outbox(operation_id,action,organization_id,organization_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='organization' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'organization',?||':organization:'||id,id FROM organization_units",(client,))
    for local,external,name,code,manager,active in db.execute("SELECT p.id,l.external_id,p.name,p.code,p.manager_id,p.active FROM organization_units p JOIN sync_object_links l ON l.object_type='organization' AND l.local_id=p.id ORDER BY p.id").fetchall():
        value={'id':external,'name':name,'code':code,'managerId':manager,'active':True}
        db.execute("INSERT INTO linux_organization_sync_outbox(operation_id,action,organization_id,organization_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,encode(value)))
        if not active:db.execute("INSERT INTO linux_organization_sync_outbox(operation_id,action,organization_id,organization_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**value,'active':False}),encode(value)))
    return client

def sync_organizations(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'organizationSync','canManageEmployees','employees.manage')
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_organization_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Organisationswarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('SELECT seq,operation_id,action,organization_id,organization_json,expected_json,wire_json FROM linux_organization_sync_outbox ORDER BY seq LIMIT 1').fetchone()
                if row:
                    seq,op,action,local,payload,expected,frozen=row
                    value={'clientId':client_id,'operationId':op,'action':action,'organization':wire(db,json.loads(payload)),'expected':wire(db,json.loads(expected)) if expected else None}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Die Mitarbeiterzuordnung einer ausstehenden Organisationsänderung hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_organization_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-organization-event','-payload',encode(value))
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT operation_id,wire_json FROM linux_organization_sync_outbox ORDER BY seq LIMIT 1').fetchone()!=(op,encode(value)):raise LocalStoreError('Die Organisationswarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_organization_sync_shadow VALUES(?,?) ON CONFLICT(organization_id) DO UPDATE SET organization_json=excluded.organization_json',(local,encode(value['organization'])));db.execute('DELETE FROM linux_organization_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-organizations')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Organisationsliste vom Server.')
        received=0
        with _db(store,lock,is_current) as db:
            if db.execute('SELECT 1 FROM linux_organization_sync_outbox LIMIT 1').fetchone():raise LocalStoreError('Lokale Organisationsänderungen stehen noch aus.')
            db.execute('UPDATE linux_organization_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                validate(remote)
                if remote['id'] in seen:raise LocalStoreError('Doppelte Organisationseinheit im Serverbestand.')
                seen.add(remote['id']);manager=local_manager(db,remote['managerId'])
                if manager is None:raise LocalStoreError('Für die Organisationsleitung fehlt eine eindeutige lokale Zuordnung: '+remote['name'])
                row=db.execute("SELECT p.id,p.name,p.code,p.manager_id,p.active,s.organization_json FROM sync_object_links l JOIN organization_units p ON p.id=l.local_id LEFT JOIN linux_organization_sync_shadow s ON s.organization_id=p.id WHERE l.object_type='organization' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,name,code,local_owner,active,shadow=row;current=wire(db,{'id':remote['id'],'name':name,'code':code,'managerId':local_owner,'active':bool(active)})
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Organisationskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+name)
                    if current!=remote:db.execute('UPDATE organization_units SET name=?,code=?,manager_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['code'],manager,int(remote['active']),local));received+=1
                else:
                    local=db.execute('INSERT INTO organization_units(name,code,manager_id,active) VALUES(?,?,?,?)',(remote['name'],remote['code'],manager,int(remote['active']))).lastrowid;db.execute("INSERT INTO sync_object_links VALUES('organization',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_organization_sync_shadow VALUES(?,?) ON CONFLICT(organization_id) DO UPDATE SET organization_json=excluded.organization_json',(local,encode(remote)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_organization_sync_shadow s JOIN sync_object_links l ON l.object_type='organization' AND l.local_id=s.organization_id"):
                if external not in seen:raise LocalStoreError('Eine zuvor synchronisierte Organisationseinheit fehlt auf dem Server.')
            db.execute('UPDATE linux_organization_sync_guard SET suppressed=0 WHERE singleton=1');db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','organizations.sync.pulled',?)",(str(received),))
        return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Organisationsabgleich fehlgeschlagen: '+str(exc)) from exc

def _preview(db,url,actor):
    if db.execute('SELECT server_url,server_actor FROM linux_organization_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Organisationswarteschlange gehört zu einem anderen Serverkonto.')
    row=db.execute('''SELECT o.seq,o.operation_id,p.id,l.external_id,p.name,p.code,p.manager_id,p.active,s.organization_json
       FROM linux_organization_sync_outbox o JOIN organization_units p ON p.id=o.organization_id JOIN sync_object_links l ON l.object_type='organization' AND l.local_id=p.id
       LEFT JOIN linux_organization_sync_shadow s ON s.organization_id=p.id ORDER BY o.seq LIMIT 1''').fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Organisationsänderung.')
    seq,operation,local,external,name,code,manager,active,shadow=row;value={'id':external,'name':name,'code':code,'managerId':manager,'active':bool(active)}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,organization_json,expected_json,wire_json FROM linux_organization_sync_outbox WHERE organization_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'local':value,'localWire':wire(db,value),'shadow':json.loads(shadow) if shadow else None,'pending':pending}

def inspect_organization_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'organizationSync','canManageEmployees','employees.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        values=command('-action','sync-organizations')
        if not isinstance(values,list):raise LocalStoreError('Ungültige Organisationsliste vom Server.')
        matches=[validate(v) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für diese Organisationseinheit gibt es keinen eindeutig zugeordneten Servereintrag. Unabhängig angelegte Einheiten mit demselben Namen oder Code müssen gesondert zugeordnet werden.')
        return {'preview':preview,'remote':matches[0],'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Organisationskonflikt konnte nicht geprüft werden: '+str(exc)) from exc

def resolve_organization_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    if inspect_organization_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Organisationseinheit oder ihre Warteschlange hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];manager=local_manager(db,remote['managerId'])
            if manager is None:raise LocalStoreError('Die Serverleitung besitzt keine eindeutige lokale Zuordnung.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_organization_sync_conflicts(id INTEGER PRIMARY KEY,organization_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_organization_sync_conflicts(organization_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',encode(current),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_organization_sync_shadow VALUES(?,?) ON CONFLICT(organization_id) DO UPDATE SET organization_json=excluded.organization_json',(local,encode(remote)))
            if accept_server:
                db.execute('UPDATE linux_organization_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE organization_units SET name=?,code=?,manager_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['name'],remote['code'],manager,int(remote['active']),local))
                db.execute('UPDATE linux_organization_sync_guard SET suppressed=0 WHERE singleton=1');db.execute('DELETE FROM linux_organization_sync_outbox WHERE organization_id=?',(local,))
            else:
                expected={**remote,'managerId':manager};action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_organization_sync_outbox WHERE organization_id=? AND seq<>?',(local,preview['seq']))
                db.execute('UPDATE linux_organization_sync_outbox SET operation_id=?,action=?,organization_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,encode(current),encode(expected),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','organizations.sync.accepted_server' if accept_server else 'organizations.sync.rebased',str(local)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Organisationskonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def organization_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_organization_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_organization_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Organisationskonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

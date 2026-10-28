"""Durable synchronization of maintenance orders and their asset references."""
import json
import re
import sqlite3
import uuid
from employee_sync import _db
from local_store import LocalStoreError
from room_sync import connect_sync_server,RoomSyncError


class MaintenanceAssetPendingError(LocalStoreError):
    pass


def encode(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def _value(prefix,active):return "json_object('id',l.external_id,'assetId',"+prefix+".asset_id,'title',"+prefix+".title,'status',"+prefix+".status,'dueAt',"+prefix+".due_at,'recurrenceDays',"+prefix+".recurrence_days,'active',json("+active+"))"
def validate(v):
    if not isinstance(v,dict) or any(not isinstance(v.get(k),str) or not v[k].strip() or '\0' in v[k] for k in ('id','assetId','title','status')) or v['status'] not in ('open','in_progress','completed','cancelled') or type(v.get('dueAt')) is not int or v['dueAt']<0 or type(v.get('recurrenceDays')) is not int or not 0<=v['recurrenceDays']<=3660 or type(v.get('active')) is not bool:raise LocalStoreError('Ungültiger Wartungsauftrag vom Server.')
    return v

def _binding(db,url,actor):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_facility_asset_sync_config'").fetchone() or db.execute('SELECT server_url,server_actor FROM linux_facility_asset_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Bitte zuerst die Anlagen für denselben Server und dasselbe Konto abgleichen.')

def wire(db,value):
    row=db.execute("SELECT l.external_id FROM sync_object_links l JOIN linux_facility_asset_sync_shadow s ON s.asset_id=l.local_id WHERE l.object_type='facility_asset' AND l.local_id=?",(value['assetId'],)).fetchone()
    if not row:
        if db.execute('SELECT 1 FROM linux_facility_asset_sync_outbox WHERE asset_id=? LIMIT 1',(value['assetId'],)).fetchone():raise MaintenanceAssetPendingError('Der Wartungsauftrag wartet auf den Anlagenabgleich.')
        raise LocalStoreError('Für den Wartungsauftrag ist die Anlage noch nicht bestätigt.')
    return {**value,'assetId':row[0]}

def local_asset(db,external):
    row=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_facility_asset_sync_shadow s ON s.asset_id=l.local_id WHERE l.object_type='facility_asset' AND l.external_id=?",(external,)).fetchone()
    return row[0] if row else None

def setup(db,url,actor):
    _binding(db,url,actor)
    db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,maintenance_id INTEGER NOT NULL,maintenance_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_sync_shadow(maintenance_id INTEGER PRIMARY KEY,maintenance_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_maintenance_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_maintenance_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Wartungswarteschlange ist an einen anderen Server oder ein anderes Konto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_maintenance_sync_config VALUES(1,?,?,?)',(client,url,actor))
    new=_value('NEW',"CASE WHEN NEW.active THEN 'true' ELSE 'false' END");old=_value('OLD',"CASE WHEN OLD.active THEN 'true' ELSE 'false' END");create=_value('NEW',"'true'")
    db.execute('''CREATE TRIGGER linux_maintenance_sync_insert AFTER INSERT ON maintenance_orders WHEN (SELECT suppressed FROM linux_maintenance_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'maintenance_order',client_id||':maintenance_order:'||NEW.id,NEW.id FROM linux_maintenance_sync_config;
      INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+create+''',NULL FROM sync_object_links l WHERE object_type='maintenance_order' AND local_id=NEW.id;
      INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+create+''' FROM sync_object_links l WHERE object_type='maintenance_order' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_maintenance_sync_update AFTER UPDATE OF asset_id,title,status,due_at,recurrence_days,active ON maintenance_orders WHEN (SELECT suppressed FROM linux_maintenance_sync_guard)=0 AND (OLD.asset_id IS NOT NEW.asset_id OR OLD.title IS NOT NEW.title OR OLD.status IS NOT NEW.status OR OLD.due_at IS NOT NEW.due_at OR OLD.recurrence_days IS NOT NEW.recurrence_days OR OLD.active IS NOT NEW.active) BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'maintenance_order',client_id||':maintenance_order:'||NEW.id,NEW.id FROM linux_maintenance_sync_config;
      INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='maintenance_order' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'maintenance_order',?||':maintenance_order:'||id,id FROM maintenance_orders",(client,))
    for local,external,asset,title,status,due,repeat,active in db.execute("SELECT p.id,l.external_id,p.asset_id,p.title,p.status,p.due_at,p.recurrence_days,p.active FROM maintenance_orders p JOIN sync_object_links l ON l.object_type='maintenance_order' AND l.local_id=p.id ORDER BY p.id").fetchall():
        value={'id':external,'assetId':asset,'title':title,'status':status,'dueAt':due,'recurrenceDays':repeat,'active':True}
        db.execute("INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,encode(value)))
        if not active:db.execute("INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**value,'active':False}),encode(value)))
    return client

def sync_maintenance(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'maintenanceSync','canManageFacility','facility.manage')
    capabilities=command('-action','capabilities')
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                _binding(db,url,actor)
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_maintenance_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Wartungswarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('SELECT seq,operation_id,action,maintenance_id,maintenance_json,expected_json,wire_json FROM linux_maintenance_sync_outbox ORDER BY seq LIMIT 1').fetchone()
                if row:
                    seq,op,action,local,payload,expected,frozen=row
                    value={'clientId':client_id,'operationId':op,'action':action,'maintenanceOrder':wire(db,json.loads(payload)),'expected':wire(db,json.loads(expected)) if expected else None}
                    frozen_value=json.loads(frozen) if frozen is not None else None
                    needs_reservation=(frozen_value is not None and 'recurrenceSuccessorId' in frozen_value) or (frozen_value is None and capabilities.get('maintenanceRecurrenceReservation')==1)
                    if needs_reservation and action!='archive' and value['maintenanceOrder']['status']=='completed' and value['maintenanceOrder']['recurrenceDays']>0:
                        successors=set()
                        for (subject,) in db.execute("SELECT subject FROM audit_log WHERE action='maintenance_order.recurrence.created' AND subject LIKE ?",(str(local)+'->%',)):
                            match=re.fullmatch(r'([1-9][0-9]*)->([1-9][0-9]*)',subject) if isinstance(subject,str) else None
                            if not match or int(match.group(1))!=local:raise LocalStoreError('Ungültige Folgeauftragsbeziehung.')
                            linked=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='maintenance_order' AND local_id=?",(int(match.group(2)),)).fetchone()
                            if not linked:raise LocalStoreError('Die Kennung des Folgeauftrags fehlt.')
                            successors.add(linked[0])
                        if len(successors)>1:raise LocalStoreError('Mehrere Folgeaufträge für denselben Auftrag.')
                        if successors:value['recurrenceSuccessorId']=next(iter(successors))
                    if frozen is not None and frozen_value!=value:raise LocalStoreError('Die Anlagenzuordnung einer ausstehenden Wartungsänderung hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_maintenance_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-maintenance-event','-payload',encode(value))
            with _db(store,lock,is_current) as db:
                _binding(db,url,actor)
                if db.execute('SELECT operation_id,wire_json FROM linux_maintenance_sync_outbox ORDER BY seq LIMIT 1').fetchone()!=(op,encode(value)):raise LocalStoreError('Die Wartungswarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_maintenance_sync_shadow VALUES(?,?) ON CONFLICT(maintenance_id) DO UPDATE SET maintenance_json=excluded.maintenance_json',(local,encode(value['maintenanceOrder'])));db.execute('DELETE FROM linux_maintenance_sync_outbox WHERE seq=?',(seq,));sent+=1
        if capabilities.get('maintenanceRecurrenceLink')==1:
            with _db(store,lock,is_current) as db:
                _binding(db,url,actor);relations={};targets={}
                for (subject,) in db.execute("SELECT subject FROM audit_log WHERE action='maintenance_order.recurrence.created' ORDER BY id"):
                    match=re.fullmatch(r'([1-9][0-9]*)->([1-9][0-9]*)',subject) if isinstance(subject,str) else None
                    if not match:raise LocalStoreError('Ungültiges Wartungsfolgeprotokoll.')
                    source,successor=map(int,match.groups())
                    if source==successor or (source in relations and relations[source]!=successor) or (successor in targets and targets[successor]!=source):raise LocalStoreError('Widersprüchliche lokale Wartungsfolgen müssen geprüft werden.')
                    relations[source]=successor;targets[successor]=source
                links=[]
                for source,successor in relations.items():
                    mapped=db.execute("""SELECT a.external_id,b.external_id FROM sync_object_links a JOIN sync_object_links b ON b.object_type='maintenance_order' AND b.local_id=? JOIN linux_maintenance_sync_shadow sa ON sa.maintenance_id=a.local_id JOIN linux_maintenance_sync_shadow sb ON sb.maintenance_id=b.local_id WHERE a.object_type='maintenance_order' AND a.local_id=?""",(successor,source)).fetchone()
                    if mapped:links.append(mapped)
            for source,successor in links:
                op=uuid.uuid5(uuid.NAMESPACE_URL,encode([client_id,'maintenance-recurrence',source,successor])).hex
                try:command('-action','maintenance-recurrence-link','-payload',encode({'clientId':client_id,'operationId':op,'sourceId':source,'successorId':successor}))
                except RoomSyncError as exc:
                    if '409' in str(exc):raise LocalStoreError('Wartungsfolgekonflikt: Die zentrale Zuordnung widerspricht der lokalen Folge. Bitte die Aufträge prüfen.') from exc
                    raise
        snapshot=command('-action','sync-maintenance')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Wartungsliste vom Server.')
        recurrence_links=command('-action','maintenance-recurrence-links') if capabilities.get('maintenanceRecurrenceLink')==1 else []
        if not isinstance(recurrence_links,list) or len(recurrence_links)>100000:raise LocalStoreError('Ungültige Wartungsfolgeliste vom Server.')
        sources={};targets=set()
        for link in recurrence_links:
            if not isinstance(link,dict) or any(not isinstance(link.get(k),str) or not link[k].strip() or '\0' in link[k] for k in ('sourceId','successorId')) or link['sourceId']==link['successorId'] or link['sourceId'] in sources or link['successorId'] in targets:raise LocalStoreError('Ungültige oder widersprüchliche Wartungsfolgeliste.')
            sources[link['sourceId']]=link['successorId'];targets.add(link['successorId'])
        checked=set()
        for source in sources:
            seen_path=set();cursor=source
            while cursor in sources and cursor not in checked:
                if cursor in seen_path:raise LocalStoreError('Die Wartungsfolgeliste enthält einen Zyklus.')
                seen_path.add(cursor);cursor=sources[cursor]
            checked.update(seen_path)

        received=0
        with _db(store,lock,is_current) as db:
            _binding(db,url,actor)
            if db.execute('SELECT 1 FROM linux_maintenance_sync_outbox LIMIT 1').fetchone():raise LocalStoreError('Lokale Wartungsänderungen stehen noch aus.')
            db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                validate(remote)
                if remote['id'] in seen:raise LocalStoreError('Doppelter Wartungsauftrag im Serverbestand.')
                seen.add(remote['id']);asset=local_asset(db,remote['assetId'])
                if asset is None:continue
                row=db.execute("SELECT p.id,p.asset_id,p.title,p.status,p.due_at,p.recurrence_days,p.active,s.maintenance_json FROM sync_object_links l JOIN maintenance_orders p ON p.id=l.local_id LEFT JOIN linux_maintenance_sync_shadow s ON s.maintenance_id=p.id WHERE l.object_type='maintenance_order' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,local_parent,title,status,due,repeat,active,shadow=row;current=wire(db,{'id':remote['id'],'assetId':local_parent,'title':title,'status':status,'dueAt':due,'recurrenceDays':repeat,'active':bool(active)})
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Wartungskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+title)
                    if current!=remote:db.execute('UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(asset,remote['title'],remote['status'],remote['dueAt'],remote['recurrenceDays'],int(remote['active']),local));received+=1
                else:
                    local=db.execute('INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days,active) VALUES(?,?,?,?,?,?)',(asset,remote['title'],remote['status'],remote['dueAt'],remote['recurrenceDays'],int(remote['active']))).lastrowid;db.execute("INSERT INTO sync_object_links VALUES('maintenance_order',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_maintenance_sync_shadow VALUES(?,?) ON CONFLICT(maintenance_id) DO UPDATE SET maintenance_json=excluded.maintenance_json',(local,encode(remote)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_maintenance_sync_shadow s JOIN sync_object_links l ON l.object_type='maintenance_order' AND l.local_id=s.maintenance_id"):
                if external not in seen:raise LocalStoreError('Ein zuvor synchronisierter Wartungsauftrag fehlt auf dem Server.')
            for source_external,successor_external in sources.items():
                source=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_maintenance_sync_shadow s ON s.maintenance_id=l.local_id WHERE l.object_type='maintenance_order' AND l.external_id=?",(source_external,)).fetchone()
                successor=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_maintenance_sync_shadow s ON s.maintenance_id=l.local_id WHERE l.object_type='maintenance_order' AND l.external_id=?",(successor_external,)).fetchone()
                if not source:continue
                if not successor:raise LocalStoreError('Der zentrale Folgeauftrag fehlt im abgeglichenen Bestand; bitte erneut synchronisieren.')
                subject=str(source[0])+'->'+str(successor[0]);old=[v[0] for v in db.execute("SELECT subject FROM audit_log WHERE action='maintenance_order.recurrence.created' AND subject LIKE ?",(str(source[0])+'->%',))]
                if old and any(value!=subject for value in old):raise LocalStoreError('Wartungsfolgekonflikt: Lokal und zentral sind verschiedene Folgen zugeordnet.')
                if not old:db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('sync.remote','maintenance_order.recurrence.created',?)",(subject,))
            db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=0 WHERE singleton=1');db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','maintenance.sync.pulled',?)",(str(received),))
        return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Wartungsabgleich fehlgeschlagen: '+str(exc)) from exc

def _preview(db,url,actor):
    _binding(db,url,actor)
    if db.execute('SELECT server_url,server_actor FROM linux_maintenance_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Wartungswarteschlange gehört zu einem anderen Serverkonto.')
    row=db.execute('''SELECT o.seq,o.operation_id,p.id,l.external_id,p.asset_id,p.title,p.status,p.due_at,p.recurrence_days,p.active,s.maintenance_json
       FROM linux_maintenance_sync_outbox o JOIN maintenance_orders p ON p.id=o.maintenance_id JOIN sync_object_links l ON l.object_type='maintenance_order' AND l.local_id=p.id
       LEFT JOIN linux_maintenance_sync_shadow s ON s.maintenance_id=p.id ORDER BY o.seq LIMIT 1''').fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Wartungsänderung.')
    seq,operation,local,external,asset,title,status,due,repeat,active,shadow=row;value={'id':external,'assetId':asset,'title':title,'status':status,'dueAt':due,'recurrenceDays':repeat,'active':bool(active)}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,maintenance_json,expected_json,wire_json FROM linux_maintenance_sync_outbox WHERE maintenance_id=? ORDER BY seq',(local,))]
    due_next=due+repeat*86400 if due else None
    candidates=[]
    if status=='completed' and repeat:
        rows=db.execute("SELECT m.id,m.due_at,m.title,m.status,m.active,m.recurrence_days,o.seq,o.operation_id FROM maintenance_orders m JOIN linux_maintenance_sync_outbox o ON o.maintenance_id=m.id AND o.action='create' WHERE m.id<>? AND m.asset_id=? AND m.recurrence_days=? ORDER BY m.id,o.seq",(local,asset,repeat)).fetchall()
        candidates=[list(v) for v in rows if due_next is None or v[1]==due_next]
    successor_details=[]
    for candidate in candidates:
        cid=candidate[0]
        if any(v['localId']==cid for v in successor_details):continue
        row=db.execute("SELECT m.asset_id,m.title,m.status,m.due_at,m.recurrence_days,m.active,l.external_id,h.maintenance_json FROM maintenance_orders m JOIN sync_object_links l ON l.object_type='maintenance_order' AND l.local_id=m.id LEFT JOIN linux_maintenance_sync_shadow h ON h.maintenance_id=m.id WHERE m.id=?",(cid,)).fetchone()
        if not row:raise LocalStoreError('Eine mögliche Folgeauftragskennung fehlt.')
        casset,ctitle,cstatus,cdue,crepeat,cactive,external_id,cshadow=row
        item={'id':external_id,'assetId':casset,'title':ctitle,'status':cstatus,'dueAt':cdue,'recurrenceDays':crepeat,'active':bool(cactive)}
        ops=[list(v) for v in db.execute('SELECT seq,operation_id,action,maintenance_json,expected_json,wire_json FROM linux_maintenance_sync_outbox WHERE maintenance_id=? ORDER BY seq',(cid,))]
        successor_details.append({'localId':cid,'local':item,'localWire':wire(db,item),'shadow':json.loads(cshadow) if cshadow else None,'pending':ops})
    return {'seq':seq,'operationId':operation,'localId':local,'local':value,'localWire':wire(db,value),'shadow':json.loads(shadow) if shadow else None,'pending':pending,'localSuccessorCandidates':candidates,'successorDetails':successor_details}

def inspect_maintenance_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'maintenanceSync','canManageFacility','facility.manage')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,url,actor);local=preview['local'];candidate_due={v[1] for v in preview['localSuccessorCandidates']}
        values=command('-action','sync-maintenance')
        if not isinstance(values,list):raise LocalStoreError('Ungültige Wartungsliste vom Server.')
        matches=[validate(v) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für diesen Wartungsauftrag gibt es keinen eindeutig zugeordneten Servereintrag.')
        server_due=matches[0]['dueAt']+matches[0]['recurrenceDays']*86400 if matches[0]['dueAt'] else None
        server_successors=[]
        if candidate_due and matches[0]['status']=='completed' and matches[0]['recurrenceDays']>0:
            server_successors=sorted([validate(v) for v in values if isinstance(v,dict) and v.get('id')!=matches[0]['id'] and v.get('assetId')==matches[0]['assetId'] and v.get('recurrenceDays')==matches[0]['recurrenceDays'] and (v.get('dueAt')==server_due if server_due is not None else True)],key=lambda v:v['id'])
        if len({v['id'] for v in server_successors})!=len(server_successors):raise LocalStoreError('Doppelte Folgeauftragskennung im Serverbestand.')
        return {'preview':preview,'remote':matches[0],'url':url,'actor':actor,'possibleDuplicateSuccessor':bool(server_successors),'serverSuccessors':server_successors}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Wartungskonflikt konnte nicht geprüft werden: '+str(exc)) from exc

def resolve_maintenance_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server,link_successor=False):
    if inspect_maintenance_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    if reviewed['possibleDuplicateSuccessor'] and not link_successor:raise LocalStoreError('Für diesen wiederkehrenden Auftrag gibt es lokal und auf dem Server mögliche Folgeaufträge. Bitte diese zunächst gesondert prüfen; die Änderungen bleiben gespeichert.')
    if link_successor and (not reviewed['possibleDuplicateSuccessor'] or len(reviewed['preview']['successorDetails'])!=1 or len(reviewed['serverSuccessors'])!=1):raise LocalStoreError('Die Folgeaufträge können nicht eindeutig zugeordnet werden; die Änderungen bleiben gespeichert.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Der lokale Wartungsauftrag oder seine Warteschlange hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];asset=local_asset(db,remote['assetId'])
            if asset is None:raise LocalStoreError('Die Serveranlage besitzt keine bestätigte lokale Zuordnung.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_sync_conflicts(id INTEGER PRIMARY KEY,maintenance_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_maintenance_sync_conflicts(maintenance_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',encode(current),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_maintenance_sync_shadow VALUES(?,?) ON CONFLICT(maintenance_id) DO UPDATE SET maintenance_json=excluded.maintenance_json',(local,encode(remote)))
            if accept_server:
                db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=1 WHERE singleton=1');db.execute('UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(asset,remote['title'],remote['status'],remote['dueAt'],remote['recurrenceDays'],int(remote['active']),local));db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=0 WHERE singleton=1');db.execute('DELETE FROM linux_maintenance_sync_outbox WHERE maintenance_id=?',(local,))
            else:
                expected={**remote,'assetId':asset};action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_maintenance_sync_outbox WHERE maintenance_id=? AND seq<>?',(local,preview['seq']));db.execute('UPDATE linux_maintenance_sync_outbox SET operation_id=?,action=?,maintenance_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,encode(current),encode(expected),preview['seq'],preview['operationId']))
            if link_successor:
                child=preview['successorDetails'][0];server_child=reviewed['serverSuccessors'][0];child_id=child['localId']
                if child['shadow'] is not None or not child['pending'] or child['pending'][0][2]!='create' or any(op[5] is not None for op in child['pending']):raise LocalStoreError('Ein Folgeauftrag wurde bereits übertragen oder bestätigt und kann nicht automatisch zugeordnet werden.')
                if not child['local']['active'] or child['local']['status']!='open' or not server_child['active'] or server_child['status']!='open':raise LocalStoreError('Ein Folgeauftrag ist bereits bearbeitet oder archiviert und kann nicht automatisch zugeordnet werden.')
                existing=db.execute("SELECT local_id FROM sync_object_links WHERE object_type='maintenance_order' AND external_id=?",(server_child['id'],)).fetchone()
                if existing and existing[0]!=child_id:raise LocalStoreError('Der Serverfolgeauftrag ist bereits einem anderen lokalen Auftrag zugeordnet.')
                server_asset=local_asset(db,server_child['assetId'])
                if server_asset is None:raise LocalStoreError('Die Anlage des Serverfolgeauftrags ist nicht lokal bestätigt.')
                child_local=child['local'];child_wire={**child['localWire'],'id':server_child['id']}
                db.execute('CREATE TABLE IF NOT EXISTS linux_maintenance_successor_links(id INTEGER PRIMARY KEY,parent_id INTEGER NOT NULL,local_id INTEGER NOT NULL,old_external_id TEXT NOT NULL,server_json TEXT NOT NULL,local_json TEXT NOT NULL,choice TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                db.execute('INSERT INTO linux_maintenance_successor_links(parent_id,local_id,old_external_id,server_json,local_json,choice,previous_operations) VALUES(?,?,?,?,?,?,?)',(local,child_id,child_local['id'],encode(server_child),encode(child_local),'server' if accept_server else 'local',encode(child['pending'])))
                changed=db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='maintenance_order' AND local_id=? AND external_id=?",(server_child['id'],child_id,child_local['id'])).rowcount
                if changed!=1:raise LocalStoreError('Die Folgeauftragszuordnung hat sich geändert.')
                db.execute('INSERT INTO linux_maintenance_sync_shadow VALUES(?,?)',(child_id,encode(server_child)))
                db.execute('DELETE FROM linux_maintenance_sync_outbox WHERE maintenance_id=?',(child_id,))
                if accept_server:
                    db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=1 WHERE singleton=1')
                    db.execute('UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(server_asset,server_child['title'],server_child['status'],server_child['dueAt'],server_child['recurrenceDays'],int(server_child['active']),child_id))
                    db.execute('UPDATE linux_maintenance_sync_guard SET suppressed=0 WHERE singleton=1')
                elif child_wire!=server_child:
                    local_value={**child_local,'id':server_child['id']};expected={**server_child,'assetId':server_asset}
                    db.execute("INSERT INTO linux_maintenance_sync_outbox(operation_id,action,maintenance_id,maintenance_json,expected_json) VALUES(?,'update',?,?,?)",(uuid.uuid4().hex,child_id,encode(local_value),encode(expected)))
                db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','maintenance.successor.linked_server' if accept_server else 'maintenance.successor.linked_local',str(local)+':'+str(child_id)))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','maintenance.sync.accepted_server' if accept_server else 'maintenance.sync.rebased',str(local)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Wartungskonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def maintenance_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_maintenance_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_maintenance_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Wartungskonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

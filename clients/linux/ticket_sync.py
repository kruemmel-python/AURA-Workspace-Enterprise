"""Durable synchronization of local ticket records."""
import json
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account,local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server

class TicketAssigneePendingError(LocalStoreError):
    def __init__(self,message,received=0):
        super().__init__(message);self.received=received

def _only_unassigned_pending(db):
    rows=db.execute('''SELECT o.action,o.wire_json,json_extract(o.ticket_json,'$.assigneeId'),t.assignee_id
      FROM linux_ticket_sync_outbox o LEFT JOIN tickets t ON t.id=o.ticket_id
      WHERE o.seq=(SELECT min(z.seq) FROM linux_ticket_sync_outbox z WHERE z.ticket_id=o.ticket_id)''').fetchall()
    if any(action!='create' or frozen is not None or initial!=0 or current!=0 for action,frozen,initial,current in rows):
        raise LocalStoreError('Lokale Ticketänderungen stehen noch aus.')
    return bool(rows)


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def _value(prefix,active):
    return "json_object('id',l.external_id,'title',"+prefix+".title,'category',"+prefix+".category,'priority',"+prefix+".priority,'status',"+prefix+".status,'assigneeId',"+prefix+".assignee_id,'active',json("+active+"))"


def setup(db,url,actor):
    db.execute('CREATE TABLE IF NOT EXISTS linux_ticket_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_ticket_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,ticket_id INTEGER NOT NULL,ticket_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_ticket_sync_shadow(ticket_id INTEGER PRIMARY KEY,ticket_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_ticket_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_ticket_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_ticket_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Ticketwarteschlange ist an einen anderen Server oder ein anderes Serverkonto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_ticket_sync_config VALUES(1,?,?,?)',(client,url,actor))
    new=_value('NEW',"CASE WHEN NEW.active THEN 'true' ELSE 'false' END");old=_value('OLD',"CASE WHEN OLD.active THEN 'true' ELSE 'false' END");create=_value('NEW',"'true'")
    db.execute('''CREATE TRIGGER linux_ticket_sync_insert AFTER INSERT ON tickets WHEN (SELECT suppressed FROM linux_ticket_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'ticket',client_id||':ticket:'||NEW.id,NEW.id FROM linux_ticket_sync_config;
      INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+create+''',NULL FROM sync_object_links l WHERE object_type='ticket' AND local_id=NEW.id;
      INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+create+''' FROM sync_object_links l WHERE object_type='ticket' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_ticket_sync_update AFTER UPDATE OF title,category,priority,status,assignee_id,active ON tickets
      WHEN (SELECT suppressed FROM linux_ticket_sync_guard)=0 AND (OLD.title IS NOT NEW.title OR OLD.category IS NOT NEW.category OR OLD.priority IS NOT NEW.priority OR OLD.status IS NOT NEW.status OR OLD.assignee_id IS NOT NEW.assignee_id OR OLD.active IS NOT NEW.active) BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'ticket',client_id||':ticket:'||NEW.id,NEW.id FROM linux_ticket_sync_config;
      INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='ticket' AND local_id=NEW.id;
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'ticket',?||':ticket:'||id,id FROM tickets",(client,))
    rows=db.execute("SELECT t.id,l.external_id,t.title,t.category,t.priority,t.status,t.assignee_id,t.active FROM tickets t JOIN sync_object_links l ON l.object_type='ticket' AND l.local_id=t.id ORDER BY t.id").fetchall()
    for local,external,title,category,priority,status,assignee,active in rows:
        value={'id':external,'title':title,'category':category,'priority':priority,'status':status,'assigneeId':assignee,'active':True}
        db.execute("INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,encode(value)))
        if not active:db.execute("INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**value,'active':False}),encode(value)))
    return client


def wire(db,value,creator):return {**value,'assigneeId':account(db,value['assigneeId']),'createdBy':creator}


def validate(remote):
    if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','title','category','priority','status')) or remote['priority'] not in ('Niedrig','Mittel','Hoch','Kritisch') or remote['status'] not in ('open','in_progress','resolved','closed') or type(remote.get('assigneeId')) is not int or remote['assigneeId']<=0 or type(remote.get('createdBy')) is not int or remote['createdBy']<=0 or type(remote.get('active')) is not bool:raise LocalStoreError('Ungültiges Ticket im Serverbestand.')
    return remote


def sync_tickets(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'ticketSync','canManageTickets','tickets.manage')
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_ticket_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Ticketwarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('''SELECT o.seq,o.operation_id,o.action,o.ticket_id,o.ticket_json,o.expected_json,o.wire_json
                  FROM linux_ticket_sync_outbox o JOIN tickets t ON t.id=o.ticket_id
                  WHERE NOT EXISTS(SELECT 1 FROM linux_ticket_sync_outbox older WHERE older.ticket_id=o.ticket_id AND older.seq<o.seq)
                    AND NOT (o.action='create' AND o.wire_json IS NULL AND json_extract(o.ticket_json,'$.assigneeId')=0 AND t.assignee_id=0)
                  ORDER BY o.seq LIMIT 1''').fetchone()
                if row:
                    seq,op,action,local,payload,expected,frozen=row;original=json.loads(payload)
                    if action=='create' and frozen is None and original['assigneeId']==0 and not db.execute('SELECT 1 FROM linux_ticket_sync_outbox WHERE ticket_id=? AND wire_json IS NOT NULL',(local,)).fetchone():
                        current=db.execute('SELECT title,category,priority,status,assignee_id,active FROM tickets WHERE id=?',(local,)).fetchone()
                        if current and current[4]>0:
                            title,category,priority,status,assignee,active=current;initial={'id':original['id'],'title':title,'category':category,'priority':priority,'status':status,'assigneeId':assignee,'active':True}
                            db.execute('UPDATE linux_ticket_sync_outbox SET ticket_json=? WHERE seq=?',(encode(initial),seq));db.execute('DELETE FROM linux_ticket_sync_outbox WHERE ticket_id=? AND seq<>?',(local,seq))
                            if not active:db.execute("INSERT INTO linux_ticket_sync_outbox(operation_id,action,ticket_id,ticket_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,encode({**initial,'active':False}),encode(initial)))
                            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','tickets.sync.coalesced',?)",(str(local),));payload=encode(initial)
                    shadow=db.execute('SELECT ticket_json FROM linux_ticket_sync_shadow WHERE ticket_id=?',(local,)).fetchone();creator=json.loads(shadow[0])['createdBy'] if shadow else actor
                    value={'clientId':client_id,'operationId':op,'action':action,'ticket':wire(db,json.loads(payload),creator),'expected':wire(db,json.loads(expected),creator) if expected else None}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Die Zuordnung einer ausstehenden Ticketänderung hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_ticket_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-ticket-event','-payload',encode(value))
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT operation_id,wire_json FROM linux_ticket_sync_outbox WHERE seq=? AND NOT EXISTS(SELECT 1 FROM linux_ticket_sync_outbox older WHERE older.ticket_id=linux_ticket_sync_outbox.ticket_id AND older.seq<linux_ticket_sync_outbox.seq)',(seq,)).fetchone()!=(op,encode(value)):raise LocalStoreError('Die Ticketwarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_ticket_sync_shadow VALUES(?,?) ON CONFLICT(ticket_id) DO UPDATE SET ticket_json=excluded.ticket_json',(local,encode(value['ticket'])));db.execute('DELETE FROM linux_ticket_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-tickets')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Ticketliste vom Server.')
        received=0
        with _db(store,lock,is_current) as db:
            pending=_only_unassigned_pending(db)
            db.execute('UPDATE linux_ticket_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                validate(remote)
                if remote['id'] in seen:raise LocalStoreError('Doppeltes Ticket im Serverbestand.')
                seen.add(remote['id']);assigned=local_id(db,remote['assigneeId'])
                if assigned is None:raise LocalStoreError('Für die zuständige Serverperson fehlt eine eindeutige lokale Zuordnung: '+remote['title'])
                row=db.execute("SELECT t.id,t.title,t.category,t.priority,t.status,t.assignee_id,t.active,s.ticket_json FROM sync_object_links l JOIN tickets t ON t.id=l.local_id LEFT JOIN linux_ticket_sync_shadow s ON s.ticket_id=t.id WHERE l.object_type='ticket' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,title,category,priority,status,assignee,active,shadow=row
                    if db.execute('SELECT 1 FROM linux_ticket_sync_outbox WHERE ticket_id=?',(local,)).fetchone():continue
                    current={'id':remote['id'],'title':title,'category':category,'priority':priority,'status':status,'assigneeId':account(db,assignee),'active':bool(active),'createdBy':json.loads(shadow)['createdBy'] if shadow else 0}
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Ticketkonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+title)
                    if current!=remote:db.execute('UPDATE tickets SET title=?,category=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['title'],remote['category'],remote['priority'],remote['status'],assigned,int(remote['active']),local));received+=1
                else:
                    local=db.execute('INSERT INTO tickets(title,category,priority,status,assignee_id,active) VALUES(?,?,?,?,?,?)',(remote['title'],remote['category'],remote['priority'],remote['status'],assigned,int(remote['active']))).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('ticket',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_ticket_sync_shadow VALUES(?,?) ON CONFLICT(ticket_id) DO UPDATE SET ticket_json=excluded.ticket_json',(local,encode(remote)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_ticket_sync_shadow s JOIN sync_object_links l ON l.object_type='ticket' AND l.local_id=s.ticket_id"):
                if external not in seen:raise LocalStoreError('Ein zuvor synchronisiertes Ticket fehlt auf dem Server.')
            db.execute('UPDATE linux_ticket_sync_guard SET suppressed=0 WHERE singleton=1');db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','tickets.sync.pulled',?)",(str(received),))
        if pending:raise TicketAssigneePendingError('Ein neues Ticket wartet auf die Zuordnung einer zuständigen Person. Andere verfügbare Tickets wurden abgeglichen.',received)
        return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Ticketabgleich fehlgeschlagen: '+str(exc)) from exc


def _preview(db,url,actor):
    if db.execute('SELECT server_url,server_actor FROM linux_ticket_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Ticketwarteschlange gehört zu einem anderen Serverkonto.')
    row=db.execute('''SELECT o.seq,o.operation_id,t.id,l.external_id,t.title,t.category,t.priority,t.status,t.assignee_id,t.active,s.ticket_json
       FROM linux_ticket_sync_outbox o JOIN tickets t ON t.id=o.ticket_id JOIN sync_object_links l ON l.object_type='ticket' AND l.local_id=t.id
       LEFT JOIN linux_ticket_sync_shadow s ON s.ticket_id=t.id ORDER BY o.seq LIMIT 1''').fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Ticketänderung.')
    seq,operation,local,external,title,category,priority,status,assignee,active,shadow=row
    value={'id':external,'title':title,'category':category,'priority':priority,'status':status,'assigneeId':assignee,'active':bool(active)}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,ticket_json,expected_json,wire_json FROM linux_ticket_sync_outbox WHERE ticket_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'local':value,'localWire':wire(db,value,json.loads(shadow)['createdBy'] if shadow else actor),'shadow':json.loads(shadow) if shadow else None,'pending':pending}


def inspect_ticket_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'ticketSync','canManageTickets','tickets.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        values=command('-action','sync-tickets')
        if not isinstance(values,list):raise LocalStoreError('Ungültige Ticketliste vom Server.')
        matches=[validate(v) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für dieses Ticket gibt es keinen eindeutig zugeordneten Servereintrag.')
        return {'preview':preview,'remote':matches[0],'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Ticketkonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_ticket_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    if inspect_ticket_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Das lokale Ticket oder die Warteschlange hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];assigned=local_id(db,remote['assigneeId'])
            if assigned is None:raise LocalStoreError('Die zuständige Serverperson besitzt keine eindeutige lokale Zuordnung.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_ticket_sync_conflicts(id INTEGER PRIMARY KEY,ticket_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_ticket_sync_conflicts(ticket_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',encode(current),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_ticket_sync_shadow VALUES(?,?) ON CONFLICT(ticket_id) DO UPDATE SET ticket_json=excluded.ticket_json',(local,encode(remote)))
            if accept_server:
                db.execute('UPDATE linux_ticket_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE tickets SET title=?,category=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['title'],remote['category'],remote['priority'],remote['status'],assigned,int(remote['active']),local))
                db.execute('UPDATE linux_ticket_sync_guard SET suppressed=0 WHERE singleton=1');db.execute('DELETE FROM linux_ticket_sync_outbox WHERE ticket_id=?',(local,))
            else:
                expected={k:remote[k] for k in ('id','title','category','priority','status','assigneeId','active')};expected['assigneeId']=assigned
                action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_ticket_sync_outbox WHERE ticket_id=? AND seq<>?',(local,preview['seq']))
                db.execute('UPDATE linux_ticket_sync_outbox SET operation_id=?,action=?,ticket_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,encode(current),encode(expected),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','tickets.sync.accepted_server' if accept_server else 'tickets.sync.rebased',str(local)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Ticketkonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def ticket_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_ticket_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_ticket_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Ticketkonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

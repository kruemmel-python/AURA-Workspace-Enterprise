"""Durable, identity-attributed outbound local booking synchronization."""
import json,sqlite3,uuid
from leave_sync import transaction,account,local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server

class BookingPendingError(LocalStoreError):
    def __init__(self,message,received=0):
        super().__init__(message);self.received=received


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def setup(db,url):
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,booking_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,booking_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_shadow(booking_id INTEGER PRIMARY KEY,booking_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_booking_sync_guard VALUES(1,0)')
    db.execute("CREATE TABLE IF NOT EXISTS linux_booking_sync_links(booking_id INTEGER PRIMARY KEY,remote_client TEXT NOT NULL,remote_id INTEGER NOT NULL,reason TEXT NOT NULL DEFAULT '',UNIQUE(remote_client,remote_id))")
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_local_shadow(booking_id INTEGER PRIMARY KEY,booking_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_version(version INTEGER PRIMARY KEY)')
    config=db.execute('SELECT client_id,server_url FROM linux_booking_sync_config').fetchone()
    if config and config[1]!=url:raise LocalStoreError('Die Buchungswarteschlange ist an einen anderen Server gebunden.')
    client=config[0] if config else 'linux-'+uuid.uuid4().hex
    if not config:db.execute('INSERT INTO linux_booking_sync_config VALUES(1,?,?)',(client,url))
    if db.execute('SELECT 1 FROM linux_booking_sync_version WHERE version=3').fetchone():return client
    db.execute('DROP TRIGGER IF EXISTS linux_booking_sync_insert');db.execute('DROP TRIGGER IF EXISTS linux_booking_sync_update');db.execute('DROP TRIGGER IF EXISTS linux_booking_sync_block')
    new="json_object('bookingId',NEW.id,'roomId',NEW.room_id,'userId',NEW.user_id,'startsAt',NEW.starts_at,'endsAt',NEW.ends_at,'attendees',NEW.attendees,'status',NEW.status)"
    old=new.replace('NEW.','OLD.')
    db.execute('''CREATE TRIGGER linux_booking_sync_insert AFTER INSERT ON bookings WHEN NEW.status='confirmed' AND (SELECT suppressed FROM linux_booking_sync_guard)=0 BEGIN
       SELECT CASE WHEN (SELECT employee_id FROM linux_booking_sync_actor WHERE singleton=1)<>NEW.user_id OR NEW.user_id<=0 THEN RAISE(ABORT,'booking actor missing') END;
       INSERT INTO linux_booking_sync_outbox(operation_id,action,booking_id,actor_id,booking_json) VALUES(lower(hex(randomblob(16))),'create',NEW.id,NEW.user_id,'''+new+'''); END''')
    db.execute('''CREATE TRIGGER linux_booking_sync_update AFTER UPDATE OF room_id,user_id,starts_at,ends_at,attendees,status ON bookings
       WHEN (SELECT suppressed FROM linux_booking_sync_guard)=0 AND OLD.status='confirmed' AND NEW.status IN ('confirmed','cancelled') AND (OLD.room_id IS NOT NEW.room_id OR OLD.user_id IS NOT NEW.user_id OR OLD.starts_at IS NOT NEW.starts_at OR OLD.ends_at IS NOT NEW.ends_at OR OLD.attendees IS NOT NEW.attendees OR OLD.status IS NOT NEW.status) BEGIN
       SELECT CASE WHEN (SELECT employee_id FROM linux_booking_sync_actor WHERE singleton=1)<=0 THEN RAISE(ABORT,'booking actor missing') END;
       SELECT CASE WHEN EXISTS(SELECT 1 FROM linux_booking_sync_shadow WHERE booking_id=NEW.id AND (json_extract(booking_json,'$.roomId')<>1 OR json_extract(booking_json,'$.userId')<=0 OR json_extract(booking_json,'$.attendees')<=0)) THEN RAISE(ABORT,'legacy booking cannot be changed') END;
       INSERT INTO linux_booking_sync_outbox(operation_id,action,booking_id,actor_id,booking_json,expected_json) VALUES(lower(hex(randomblob(16))),CASE WHEN NEW.status='cancelled' THEN 'cancel' ELSE 'reschedule' END,NEW.id,(SELECT employee_id FROM linux_booking_sync_actor WHERE singleton=1),'''+new+','+old+'''); END''')
    db.execute('''CREATE TRIGGER linux_booking_sync_block AFTER INSERT ON booking_block_details WHEN (SELECT suppressed FROM linux_booking_sync_guard)=0 BEGIN
       SELECT CASE WHEN NEW.created_by<=0 OR (SELECT employee_id FROM linux_booking_sync_actor WHERE singleton=1)<>NEW.created_by THEN RAISE(ABORT,'block actor missing') END;
       INSERT INTO linux_booking_sync_outbox(operation_id,action,booking_id,actor_id,booking_json)
       SELECT lower(hex(randomblob(16))),'block',b.id,NEW.created_by,json_object('bookingId',b.id,'roomId',b.room_id,'userId',0,'startsAt',b.starts_at,'endsAt',b.ends_at,'attendees',b.attendees,'status',b.status,'reason',NEW.reason)
       FROM bookings b WHERE b.id=NEW.booking_id AND b.status='blocked'; END''')
    for id,room,actor,start,end,attendees,reason in db.execute("SELECT b.id,b.room_id,d.created_by,b.starts_at,b.ends_at,b.attendees,d.reason FROM bookings b JOIN booking_block_details d ON d.booking_id=b.id WHERE b.status='blocked' AND d.created_by>0 AND NOT EXISTS(SELECT 1 FROM linux_booking_sync_links l WHERE l.booking_id=b.id) AND NOT EXISTS(SELECT 1 FROM linux_booking_sync_outbox o WHERE o.booking_id=b.id)").fetchall():
        value={'bookingId':id,'roomId':room,'userId':0,'startsAt':start,'endsAt':end,'attendees':attendees,'status':'blocked','reason':reason}
        db.execute("INSERT INTO linux_booking_sync_outbox(operation_id,action,booking_id,actor_id,booking_json) VALUES(?,'block',?,?,?)",(uuid.uuid4().hex,id,actor,encode(value)))
    db.execute('INSERT INTO linux_booking_sync_version VALUES(3)')
    if config:
        db.execute("INSERT OR IGNORE INTO linux_booking_sync_links(booking_id,remote_client,remote_id) SELECT booking_id,json_extract(booking_json,'$.clientId'),json_extract(booking_json,'$.bookingId') FROM linux_booking_sync_shadow")
        return client
    rows=db.execute("SELECT id,room_id,user_id,starts_at,ends_at,attendees,status FROM bookings WHERE status='confirmed' ORDER BY id").fetchall()
    for id,room,user,start,end,attendees,status in rows:
        if user<=0:raise LocalStoreError('Eine bestehende Buchung hat keinen zugeordneten Mitarbeiter.')
        value={'bookingId':id,'roomId':room,'userId':user,'startsAt':start,'endsAt':end,'attendees':attendees,'status':status}
        db.execute("INSERT INTO linux_booking_sync_outbox(operation_id,action,booking_id,actor_id,booking_json) VALUES(?,'create',?,?,?)",(uuid.uuid4().hex,id,user,encode(value)))
    return client


def wire(db,client,value,acting=None):
    room=db.execute("SELECT l.external_id FROM sync_object_links l JOIN linux_room_sync_shadow s ON s.room_id=l.local_id WHERE l.object_type='room' AND l.local_id=?",(value['roomId'],)).fetchone()
    if not room:raise LocalStoreError('Der Raum der Buchung ist noch nicht synchronisiert.')
    link=db.execute('SELECT remote_client,remote_id,reason FROM linux_booking_sync_links WHERE booking_id=?',(value['bookingId'],)).fetchone()
    source,remote_id,reason=link if link else (client,value['bookingId'],value.get('reason',''))
    owner=acting if value['status']=='blocked' and value['userId']==0 else value['userId']
    if not owner:raise LocalStoreError('Für die Sperrzeit fehlt die zugeordnete handelnde Person.')
    return {**value,'bookingId':remote_id,'clientId':source,'roomId':1,'roomKey':room[0],'userId':account(db,owner),'reason':reason}


def sync_bookings(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'bookingSync','canBookRooms','rooms.book')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_room_sync_config'").fetchone() or not db.execute('SELECT 1 FROM linux_room_sync_config WHERE server_url=?',(url,)).fetchone():raise LocalStoreError('Bitte zuerst Räume für denselben Server abgleichen.')
            client_id=setup(db,url)
        sent=0;waiting=False
        while True:
            with transaction(store,lock,is_current) as (db,current):
                if current!=local_actor or account(db,current)!=actor:raise LocalStoreError('Sitzung oder Serverkonto-Zuordnung wurde geändert.')
                if db.execute('SELECT client_id,server_url FROM linux_booking_sync_config').fetchone()!=(client_id,url):raise LocalStoreError('Die Buchungswarteschlange gehört zu einem anderen Server.')
                # Preserve each booking's history while allowing an unrelated
                # booking to proceed if another person's earlier operation is
                # waiting for that person to synchronize.
                row=db.execute('''SELECT o.seq,o.operation_id,o.action,o.booking_id,o.booking_json,o.expected_json,o.wire_json
                    FROM linux_booking_sync_outbox o WHERE o.actor_id=?
                    AND NOT EXISTS(SELECT 1 FROM linux_booking_sync_outbox older WHERE older.booking_id=o.booking_id AND older.seq<o.seq)
                    AND EXISTS(SELECT 1 FROM linux_room_sync_shadow r WHERE r.room_id=json_extract(o.booking_json,'$.roomId'))
                    AND (o.expected_json IS NULL OR EXISTS(SELECT 1 FROM linux_room_sync_shadow r WHERE r.room_id=json_extract(o.expected_json,'$.roomId')))
                    ORDER BY o.seq LIMIT 1''',(local_actor,)).fetchone()
                if not row and db.execute('SELECT 1 FROM linux_booking_sync_outbox WHERE actor_id=? LIMIT 1',(local_actor,)).fetchone():
                    waiting=True
                if row:
                    seq,op,action,id,payload,expected,frozen=row
                    value={'clientId':client_id,'operationId':op,'action':action,'booking':wire(db,client_id,json.loads(payload),local_actor),'expected':wire(db,client_id,json.loads(expected),local_actor) if expected else None}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Eine Zuordnung einer ausstehenden Buchung hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_booking_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            if action=='block' and command('-action','capabilities').get('bookingBlockSync')!=1:raise LocalStoreError('Der Server unterstützt die Übertragung von Sperrzeiten nicht.')
            command('-action','sync-booking-event','-payload',encode(value))
            with transaction(store,lock,is_current) as (db,current):
                if current!=local_actor or account(db,current)!=actor:raise LocalStoreError('Sitzung oder Serverkonto-Zuordnung wurde geändert.')
                if db.execute('SELECT wire_json FROM linux_booking_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),):raise LocalStoreError('Die Buchungswarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_booking_sync_shadow VALUES(?,?) ON CONFLICT(booking_id) DO UPDATE SET booking_json=excluded.booking_json',(id,encode(value['booking'])))
                db.execute('INSERT OR IGNORE INTO linux_booking_sync_links(booking_id,remote_client,remote_id,reason) VALUES(?,?,?,?)',(id,value['booking']['clientId'],value['booking']['bookingId'],value['booking']['reason']))
                db.execute('INSERT INTO linux_booking_sync_local_shadow VALUES(?,?) ON CONFLICT(booking_id) DO UPDATE SET booking_json=excluded.booking_json',(id,encode({k:v for k,v in json.loads(payload).items() if k!='reason'})))
                db.execute('DELETE FROM linux_booking_sync_outbox WHERE seq=?',(seq,))
                db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',(str(local_actor),'booking.sync.sent',str(id)+':'+action))
                sent+=1
        snapshot=command('-action','sync-bookings')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Buchungsliste vom Server.')
        with transaction(store,lock,is_current) as (db,current):
            if current!=local_actor or account(db,current)!=actor:raise LocalStoreError('Sitzung oder Serverkonto-Zuordnung wurde geändert.')
            received,unmapped=apply_snapshot(db,snapshot,client_id)
        if waiting:raise BookingPendingError('Eine Buchung wartet auf den Raumabgleich oder eine ältere Änderung der zugehörigen Person. Andere verfügbare Änderungen wurden abgeglichen.',received)
        return {'sent':sent,'received':received,'unmapped':unmapped}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Buchungsabgleich fehlgeschlagen: '+str(exc)) from exc


def apply_snapshot(db,snapshot,client):
    seen=set();received=0;unmapped=0
    db.execute('UPDATE linux_booking_sync_guard SET suppressed=1 WHERE singleton=1')
    for raw in snapshot:
        if not isinstance(raw,dict) or not isinstance(raw.get('clientId'),str) or not raw['clientId'] or not isinstance(raw.get('roomKey'),str) or not raw['roomKey'] or any(type(raw.get(k)) is not int for k in ('bookingId','roomId','userId','attendees','startsAt','endsAt')) or raw['bookingId']<=0 or raw['roomId']<=0 or raw['startsAt']<=0 or raw['endsAt']<=raw['startsAt'] or raw['userId']<0 or raw['attendees']<0 or raw.get('status','') not in ('','confirmed','cancelled','blocked') or not isinstance(raw.get('reason',''),str):raise LocalStoreError('Ungültige Buchung vom Server.')
        remote={**raw,'status':raw.get('status') or 'confirmed','reason':raw.get('reason','')}
        key=(remote['clientId'],remote['bookingId'])
        if key in seen:raise LocalStoreError('Doppelte Buchungskennung vom Server.')
        seen.add(key)
        room=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_room_sync_shadow s ON s.room_id=l.local_id WHERE l.object_type='room' AND l.external_id=?",(remote['roomKey'],)).fetchone()
        if not room:unmapped+=1;continue
        owner=local_id(db,remote['userId']) if remote['userId'] else None
        if remote['roomId']!=1 or (remote['status']!='blocked' and owner is None):unmapped+=1
        local_user=0 if remote['status']=='blocked' else (owner or 0)
        desired={'roomId':room[0],'userId':local_user,'startsAt':remote['startsAt'],'endsAt':remote['endsAt'],'attendees':remote['attendees'] or 1,'status':remote['status']}
        row=db.execute("SELECT b.id,b.room_id,b.user_id,b.starts_at,b.ends_at,b.attendees,b.status,s.booking_json,ls.booking_json FROM linux_booking_sync_links l JOIN bookings b ON b.id=l.booking_id LEFT JOIN linux_booking_sync_shadow s ON s.booking_id=b.id LEFT JOIN linux_booking_sync_local_shadow ls ON ls.booking_id=b.id WHERE l.remote_client=? AND l.remote_id=?",key).fetchone()
        if not row and key[0]==client:
            pending=db.execute('SELECT 1 FROM linux_booking_sync_outbox WHERE booking_id=?',(key[1],)).fetchone()
            if pending:continue
            if db.execute('SELECT 1 FROM bookings WHERE id=?',(key[1],)).fetchone():raise LocalStoreError('Lokale Buchung ohne bestätigte Zuordnung; bitte Abgleich erneut prüfen.')
        if row:
            id,room_id,user,start,end,attendees,status,shadow,local_shadow=row
            if db.execute('SELECT 1 FROM linux_booking_sync_outbox WHERE booking_id=?',(id,)).fetchone():continue
            current={'bookingId':id,'roomId':room_id,'userId':user,'startsAt':start,'endsAt':end,'attendees':attendees,'status':status}
            if local_shadow:
                if current!=json.loads(local_shadow):raise LocalStoreError('Buchungskonflikt: Lokaler Stand wurde außerhalb der Warteschlange geändert.')
            elif not shadow or wire(db,client,current)!=json.loads(shadow):raise LocalStoreError('Buchungskonflikt: Gemeinsamer Ausgangsstand fehlt.')
        else:id=0
        local={'bookingId':id,**desired}
        if row:
            changed=any(current[k]!=local[k] for k in desired)
            previous_reason=json.loads(shadow).get('reason','') if shadow else ''
            if changed:
                db.execute('UPDATE bookings SET room_id=?,user_id=?,starts_at=?,ends_at=?,attendees=?,status=? WHERE id=?',(desired['roomId'],desired['userId'],desired['startsAt'],desired['endsAt'],desired['attendees'],desired['status'],id))
            if previous_reason!=remote['reason']:changed=True
            db.execute('UPDATE linux_booking_sync_links SET reason=? WHERE booking_id=?',(remote['reason'],id))
        else:
            id=db.execute('INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) VALUES(?,?,?,?,?,?)',(desired['roomId'],desired['userId'],desired['startsAt'],desired['endsAt'],desired['attendees'],desired['status'])).lastrowid
            local['bookingId']=id;db.execute('INSERT INTO linux_booking_sync_links VALUES(?,?,?,?)',(id,*key,remote['reason']));changed=True
        if changed:
            received+=1
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','booking.sync.received',str(id)+':'+remote['status']))
        db.execute('INSERT INTO linux_booking_sync_shadow VALUES(?,?) ON CONFLICT(booking_id) DO UPDATE SET booking_json=excluded.booking_json',(id,encode(remote)))
        db.execute('INSERT INTO linux_booking_sync_local_shadow VALUES(?,?) ON CONFLICT(booking_id) DO UPDATE SET booking_json=excluded.booking_json',(id,encode(local)))
    # Validate the final combined state, not intermediate order: a server can
    # move an existing reservation and then reuse its former time slot.
    if db.execute("SELECT 1 FROM bookings a JOIN bookings b ON b.room_id=a.room_id AND b.id>a.id AND b.status IN ('confirmed','blocked') AND b.starts_at<a.ends_at AND b.ends_at>a.starts_at WHERE a.status IN ('confirmed','blocked') LIMIT 1").fetchone():
        raise LocalStoreError('Buchungskonflikt: Ein lokaler Termin überschneidet sich mit einer Serverbuchung.')
    db.execute('UPDATE linux_booking_sync_guard SET suppressed=0 WHERE singleton=1')
    return received,unmapped


def _booking_conflict_preview(db,local_actor,url,actor):
    if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
    config=db.execute('SELECT client_id,server_url FROM linux_booking_sync_config').fetchone()
    if not config or config[1]!=url:raise LocalStoreError('Die Buchungswarteschlange gehört zu einem anderen Server.')
    client=config[0]
    row=db.execute("SELECT o.seq,o.operation_id,o.action,b.id,b.room_id,b.user_id,b.starts_at,b.ends_at,b.attendees,b.status FROM linux_booking_sync_outbox o JOIN bookings b ON b.id=o.booking_id WHERE o.actor_id=? ORDER BY o.seq LIMIT 1",(local_actor,)).fetchone()
    if not row:raise LocalStoreError('Es gibt keine eigene ausstehende Buchungsänderung.')
    seq,operation,action,id,room,user,start,end,attendees,status=row
    pending=db.execute('SELECT seq,operation_id,action,actor_id,booking_json,expected_json,wire_json FROM linux_booking_sync_outbox WHERE booking_id=? ORDER BY seq',(id,)).fetchall()
    if any(v[3]!=local_actor for v in pending):raise LocalStoreError('Für diese Buchung stehen Änderungen einer anderen Person aus. Diese müssen zuerst abgeglichen werden.')
    if pending[0][1]!=operation or any(v[6] is not None for v in pending[1:]):raise LocalStoreError('Die Reihenfolge der ausstehenden Änderungen muss zuerst geklärt werden.')
    linked=db.execute('SELECT 1 FROM linux_booking_sync_links WHERE booking_id=?',(id,)).fetchone()
    shadow=db.execute('SELECT 1 FROM linux_booking_sync_shadow WHERE booking_id=?',(id,)).fetchone()
    if action=='create':
        if status!='confirmed' or user!=local_actor or linked or shadow:raise LocalStoreError('Diese neue Buchung kann hier nicht entschieden werden.')
    elif action in ('reschedule','cancel'):
        if not linked or not shadow or not pending[0][5] or status not in ('confirmed','cancelled'):raise LocalStoreError('Ein gemeinsamer Ausgangsstand für diese Buchung fehlt.')
    else:raise LocalStoreError('Dieser Konflikt benötigt eine gesonderte Klärung.')
    local={'bookingId':id,'roomId':room,'userId':user,'startsAt':start,'endsAt':end,'attendees':attendees,'status':status}
    creation={'clientId':client,'operationId':operation,'action':action,'booking':wire(db,client,json.loads(pending[0][4]),local_actor),'expected':wire(db,client,json.loads(pending[0][5]),local_actor) if pending[0][5] else None}
    if pending[0][6] is not None and json.loads(pending[0][6])!=creation:raise LocalStoreError('Die Zuordnung der ursprünglichen Buchung hat sich geändert.')
    return {'seq':seq,'operationId':operation,'action':action,'localId':id,'local':local,'wire':wire(db,client,local),'creation':creation,'pending':[list(v) for v in pending]}


def inspect_booking_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'bookingSync','canBookRooms','rooms.book')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):preview=_booking_conflict_preview(db,local_actor,url,actor)
        snapshot=command('-action','sync-bookings')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Buchungsliste vom Server.')
        local=preview['wire'];requested=preview['creation']['booking'];matches=[]
        for item in snapshot:
            if not isinstance(item,dict):raise LocalStoreError('Ungültige Buchung vom Server.')
            same=item.get('clientId')==local['clientId'] and item.get('bookingId')==local['bookingId']
            if preview['action']=='create':
                if same:raise LocalStoreError('Diese Buchung existiert bereits auf dem Server. Bitte zuerst den normalen Abgleich erneut versuchen.')
                if item.get('roomKey')==requested['roomKey'] and item.get('status','') in ('','confirmed','blocked'):
                    if type(item.get('startsAt')) is not int or type(item.get('endsAt')) is not int or item['endsAt']<=item['startsAt']:raise LocalStoreError('Ungültiger Zeitraum im Serverbestand.')
                    if item['startsAt']<requested['endsAt'] and item['endsAt']>requested['startsAt']:matches.append(item)
            elif same:matches.append({**item,'status':item.get('status') or 'confirmed','reason':item.get('reason','')})
        if preview['action']=='create':
            if not matches:raise LocalStoreError('Es wurde keine überschneidende Serverbuchung gefunden. Bitte den Abgleich erneut versuchen.')
        else:
            expected=preview['creation']['expected']
            if len(matches)!=1 or matches[0]==expected:raise LocalStoreError('Ein abweichender Serverstand wurde nicht eindeutig gefunden. Bitte den Abgleich erneut versuchen.')
            if any(matches[0].get(k)!=expected.get(k) for k in ('clientId','bookingId','roomId','roomKey','userId')):raise LocalStoreError('Die Identität der Serverbuchung hat sich geändert und muss gesondert geklärt werden.')
        return {'preview':preview,'conflicts':matches,'snapshot':snapshot,'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Buchungskonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def accept_server_booking_conflict(store,lock,is_current,client,url,token,ca,reviewed):
    fresh=inspect_booking_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    checked_url,checked_actor,command=connect_sync_server(client,url,token,ca,'bookingSync','canBookRooms','rooms.book')
    if checked_url!=reviewed['url'] or checked_actor!=reviewed['actor']:raise LocalStoreError('Server oder Konto wurde geändert.')
    command('-action','abandon-booking-event','-payload',encode(reviewed['preview']['creation']))
    if inspect_booking_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich geändert. Bitte erneut prüfen; die ursprüngliche Übertragung bleibt serverseitig gesperrt.')
    try:
        with transaction(store,lock,is_current) as (db,local_actor):
            preview=_booking_conflict_preview(db,local_actor,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Buchung hat sich geändert. Bitte erneut prüfen.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_booking_sync_resolutions(id INTEGER PRIMARY KEY,booking_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,local_json TEXT NOT NULL,conflicts_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_booking_sync_resolutions(booking_id,actor_id,local_json,conflicts_json,previous_operations) VALUES(?,?,?,?,?)',(preview['localId'],local_actor,encode(preview['local']),encode(reviewed['conflicts']),encode(preview['pending'])))
            db.execute('UPDATE linux_booking_sync_guard SET suppressed=1 WHERE singleton=1')
            if preview['action']=='create':db.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'",(preview['localId'],))
            else:db.execute('INSERT INTO linux_booking_sync_local_shadow VALUES(?,?) ON CONFLICT(booking_id) DO UPDATE SET booking_json=excluded.booking_json',(preview['localId'],encode(preview['local'])))
            db.execute('DELETE FROM linux_booking_sync_outbox WHERE booking_id=? AND actor_id=?',(preview['localId'],local_actor))
            apply_snapshot(db,reviewed['snapshot'],preview['wire']['clientId'])
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'booking.sync.accepted_server',?)",(str(local_actor),str(preview['localId'])))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Serverbuchung konnte nicht übernommen werden: '+str(exc)) from exc


def inspect_legacy_booking(store,lock,is_current,client,url,token,ca,booking_id):
    """Prepare an explicit account mapping for a previously unowned booking."""
    url,actor,command=connect_sync_server(client,url,token,ca,'legacyBookingReconciliation','canManageRooms','rooms.manage und employees.manage')
    if not command('-action','identity').get('canManageEmployees'):raise LocalStoreError('Das Serverkonto benötigt zusätzlich employees.manage.')
    with transaction(store,lock,is_current) as (db,local_actor):
        local_identity=store.identity()
        if not local_identity.get('canManageEmployees') or not local_identity.get('canManageRooms') or account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Verwaltungssitzung benötigt Mitarbeiter- und Raumverwaltung und muss dem Serverkonto zugeordnet sein.')
        if db.execute('SELECT server_url FROM linux_booking_sync_config').fetchone()!=(url,):raise LocalStoreError('Bitte zuerst Buchungen für diesen Server abgleichen.')
        row=db.execute('''SELECT b.id,b.room_id,b.user_id,b.starts_at,b.ends_at,b.attendees,b.status,l.remote_client,l.remote_id,s.booking_json
          FROM bookings b JOIN linux_booking_sync_links l ON l.booking_id=b.id JOIN linux_booking_sync_shadow s ON s.booking_id=b.id WHERE b.id=?''',(booking_id,)).fetchone()
        if not row or row[2]!=0 or row[6] not in ('confirmed','cancelled') or db.execute('SELECT 1 FROM linux_booking_sync_outbox WHERE booking_id=?',(booking_id,)).fetchone():raise LocalStoreError('Diese Buchung hat keine klärbare unveränderte Zuordnung.')
        shadow=json.loads(row[9]);local={'id':row[0],'roomId':row[1],'userId':row[2],'startsAt':row[3],'endsAt':row[4],'attendees':row[5],'status':row[6]}
        local_shadow=db.execute('SELECT booking_json FROM linux_booking_sync_local_shadow WHERE booking_id=?',(booking_id,)).fetchone()
        expected_local={'bookingId':row[0],**{k:local[k] for k in ('roomId','userId','startsAt','endsAt','attendees','status')}}
        if not local_shadow or json.loads(local_shadow[0])!=expected_local:raise LocalStoreError('Die lokale Buchung wurde seit dem letzten Abgleich geändert.')
        if shadow.get('userId')!=0 or shadow.get('clientId')!=row[7] or shadow.get('bookingId')!=row[8]:raise LocalStoreError('Die historische Buchung hat bereits eine Zuordnung.')
        preview={'url':url,'actor':actor,'localActor':local_actor,'local':local,'shadow':shadow}
    values=command('-action','sync-bookings');employees=command('-action','sync-employees')
    matches=[v for v in values if isinstance(v,dict) and v.get('clientId')==row[7] and v.get('bookingId')==row[8]] if isinstance(values,list) else []
    if len(matches)!=1 or not isinstance(employees,list):raise LocalStoreError('Die zentrale Buchung oder das Mitarbeiterverzeichnis ist nicht eindeutig verfügbar.')
    expected=matches[0];normalized={**expected,'status':expected.get('status') or 'confirmed','reason':expected.get('reason','')}
    if normalized!=shadow:raise LocalStoreError('Die zentrale Buchung hat sich geändert. Bitte erneut abgleichen.')
    people=[p for p in employees if isinstance(p,dict) and isinstance(p.get('id'),str) and p['id'] and isinstance(p.get('name'),str) and p.get('active') is True and type(p.get('accountEmployeeId')) is int and p['accountEmployeeId']>0]
    if not people:raise LocalStoreError('Es sind keine aktiven Mitarbeiter mit Serverkonto zugeordnet.')
    return {'preview':preview,'expected':expected,'employees':people}


def reconcile_legacy_booking(store,lock,is_current,client,url,token,ca,reviewed,employee_id,attendees):
    if not isinstance(reviewed,dict) or not isinstance(employee_id,str) or type(attendees) is not int or attendees<=0:raise LocalStoreError('Ungültige Auswahl.')
    fresh=inspect_legacy_booking(store,lock,is_current,client,url,token,ca,reviewed['preview']['local']['id'])
    if fresh!=reviewed:raise LocalStoreError('Buchung oder Mitarbeiterliste hat sich geändert. Bitte erneut prüfen.')
    if not any(p['id']==employee_id for p in reviewed['employees']):raise LocalStoreError('Die ausgewählte Person ist nicht mehr verfügbar.')
    _,actor,command=connect_sync_server(client,url,token,ca,'legacyBookingReconciliation','canManageRooms','rooms.manage und employees.manage')
    if actor!=reviewed['preview']['actor'] or not is_current():raise LocalStoreError('Sitzung oder Serverkonto wurde geändert.')
    result=command('-action','legacy-booking-reconcile','-payload',encode({'expected':reviewed['expected'],'employeeExternalId':employee_id,'attendees':attendees}))
    if not is_current():raise LocalStoreError('Zuordnung auf dem Server gespeichert; die lokale Sitzung wurde gewechselt. Bitte erneut abgleichen.')
    return result

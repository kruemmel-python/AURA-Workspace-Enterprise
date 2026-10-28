"""Durable, explicit synchronization for local task records and schedules."""
import json
import re
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account, local_id
from local_store import LocalStoreError
from room_sync import connect_sync_server
import subtask_sync
import dependency_sync

class TaskAssigneePendingError(LocalStoreError):
    """A new task cannot be sent until it has an assignee."""
    def __init__(self,message,received=0):
        super().__init__(message);self.received=received

def _only_unassigned_pending(db):
    rows=db.execute('''SELECT o.action,o.wire_json,json_extract(o.task_json,'$.assigneeId'),t.assignee_id
      FROM linux_task_sync_outbox o LEFT JOIN tasks t ON t.id=o.task_id
      WHERE o.seq=(SELECT min(z.seq) FROM linux_task_sync_outbox z WHERE z.task_id=o.task_id)''').fetchall()
    if any(action!='create' or wire is not None or initial!=0 or current!=0 for action,wire,initial,current in rows):
        raise LocalStoreError('Lokale Aufgabenänderungen stehen noch aus.')
    return bool(rows)


def _json(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))

# Queue snapshots use local employee IDs. Server IDs are frozen before first send.
def _task_json(prefix,due,repeat):
    return "json_object('id',l.external_id,'title',"+prefix+".title,'department',"+prefix+".department,'priority',"+prefix+".priority,'status',"+prefix+".status,'assigneeId',"+prefix+".assignee_id,'active',json(CASE WHEN "+prefix+".active THEN 'true' ELSE 'false' END),'dueAt',"+due+",'recurrenceDays',"+repeat+")"

def _setup(db,url,actor):
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,task_id INTEGER NOT NULL,task_json TEXT NOT NULL,expected_json TEXT,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_sync_shadow(task_id INTEGER PRIMARY KEY,task_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_task_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_task_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Aufgabenwarteschlange ist an einen anderen Server oder ein anderes Serverkonto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_task_sync_config VALUES(1,?,?,?)',(client,url,actor))
    due="COALESCE((SELECT due_at FROM task_schedules WHERE task_id=NEW.id),0)";rep="COALESCE((SELECT recurrence_days FROM task_schedules WHERE task_id=NEW.id),0)"
    new=_task_json('NEW',due,rep);old=_task_json('OLD',due,rep)
    created=new.replace("json(CASE WHEN NEW.active THEN 'true' ELSE 'false' END)","json('true')")
    db.execute('''CREATE TRIGGER linux_task_sync_insert AFTER INSERT ON tasks WHEN (SELECT suppressed FROM linux_task_sync_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'task',client_id||':task:'||NEW.id,NEW.id FROM linux_task_sync_config;
      INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json) SELECT lower(hex(randomblob(16))),'create',NEW.id,'''+created+''',NULL FROM sync_object_links l WHERE object_type='task' AND local_id=NEW.id;
      INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',NEW.id,'''+new+','+created+''' FROM sync_object_links l WHERE object_type='task' AND local_id=NEW.id AND NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER linux_task_sync_update AFTER UPDATE OF title,department,priority,status,assignee_id,active ON tasks
      WHEN (SELECT suppressed FROM linux_task_sync_guard)=0 AND (OLD.title IS NOT NEW.title OR OLD.department IS NOT NEW.department OR OLD.priority IS NOT NEW.priority OR OLD.status IS NOT NEW.status OR OLD.assignee_id IS NOT NEW.assignee_id OR OLD.active IS NOT NEW.active)
      BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'task',client_id||':task:'||NEW.id,NEW.id FROM linux_task_sync_config;
      INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json) SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,'''+new+','+old+''' FROM sync_object_links l WHERE object_type='task' AND local_id=NEW.id;
    END''')
    for event in ('INSERT','UPDATE'):
        old_due='0' if event=='INSERT' else 'OLD.due_at';old_rep='0' if event=='INSERT' else 'OLD.recurrence_days'
        newval=_task_json('t','NEW.due_at','NEW.recurrence_days');oldval=_task_json('t',old_due,old_rep)
        db.execute('''CREATE TRIGGER linux_task_sync_schedule_'''+event.lower()+''' AFTER '''+event+''' ON task_schedules
          WHEN (SELECT suppressed FROM linux_task_sync_guard)=0 AND ('''+old_due+''' IS NOT NEW.due_at OR '''+old_rep+''' IS NOT NEW.recurrence_days)
          BEGIN INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json)
          SELECT lower(hex(randomblob(16))),'update',t.id,'''+newval+','+oldval+''' FROM tasks t JOIN sync_object_links l ON l.object_type='task' AND l.local_id=t.id WHERE t.id=NEW.task_id; END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'task',?||':task:'||id,id FROM tasks",(client,))
    rows=db.execute("SELECT t.id,l.external_id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t JOIN sync_object_links l ON l.object_type='task' AND l.local_id=t.id LEFT JOIN task_schedules s ON s.task_id=t.id ORDER BY t.id").fetchall()
    for local,external,title,department,priority,status,assignee,active,due,repeat in rows:
        value={'id':external,'title':title,'department':department,'priority':priority,'status':status,'assigneeId':assignee,'active':True,'dueAt':due,'recurrenceDays':repeat}
        db.execute("INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json) VALUES(?,'create',?,?)",(uuid.uuid4().hex,local,_json(value)))
        if not active:db.execute("INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,_json({**value,'active':False}),_json(value)))
    return client


def _wire(db,value,creator):
    return {**value,'assigneeId':account(db,value['assigneeId']),'createdBy':creator}


def sync_tasks(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'taskSync','canManageTasks','tasks.manage')
    capabilities=command('-action','capabilities')
    if capabilities.get('subtaskSync')!=1 or capabilities.get('dependencySync')!=1:raise LocalStoreError('Der Server unterstützt den Abgleich von Unteraufgaben oder Voraussetzungen nicht.')
    try:
        with _db(store,lock,is_current) as db:
            client_id=_setup(db,url,actor);subtask_sync.setup(db);dependency_sync.setup(db)
        sent=dependency_sync.drain(store,lock,is_current,command,client_id,url,actor)
        sent+=subtask_sync.drain(store,lock,is_current,command,client_id,url,actor)
        while True:
            with _db(store,lock,is_current) as db:
                config=db.execute('SELECT client_id,server_url,server_actor FROM linux_task_sync_config').fetchone()
                if config!=(client_id,url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Serverkonto.')
                row=db.execute('''SELECT o.seq,o.operation_id,o.action,o.task_id,o.task_json,o.expected_json,o.wire_json
                    FROM linux_task_sync_outbox o JOIN tasks t ON t.id=o.task_id
                    WHERE NOT EXISTS(SELECT 1 FROM linux_task_sync_outbox older WHERE older.task_id=o.task_id AND older.seq<o.seq)
                      AND NOT (o.action='create' AND o.wire_json IS NULL AND json_extract(o.task_json,'$.assigneeId')=0 AND t.assignee_id=0)
                    ORDER BY o.seq LIMIT 1''').fetchone()
                if row:
                    seq,op,action,local,payload,expected,wire=row
                    # A newly created, never transmitted unassigned task can be
                    # assigned later. Fold its unsent edits into its first create.
                    original=json.loads(payload)
                    if action=='create' and wire is None and original['assigneeId']==0 and not db.execute('SELECT 1 FROM linux_task_sync_outbox WHERE task_id=? AND wire_json IS NOT NULL',(local,)).fetchone():
                        current=db.execute('SELECT t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t LEFT JOIN task_schedules s ON s.task_id=t.id WHERE t.id=?',(local,)).fetchone()
                        if current and current[4]>0:
                            title,department,priority,status,assignee,active,due,repeat=current
                            create={'id':original['id'],'title':title,'department':department,'priority':priority,'status':status,'assigneeId':assignee,'active':True,'dueAt':due,'recurrenceDays':repeat}
                            db.execute('UPDATE linux_task_sync_outbox SET task_json=? WHERE seq=?',(_json(create),seq))
                            db.execute('DELETE FROM linux_task_sync_outbox WHERE task_id=? AND seq<>?',(local,seq))
                            if not active:db.execute("INSERT INTO linux_task_sync_outbox(operation_id,action,task_id,task_json,expected_json) VALUES(?,'archive',?,?,?)",(uuid.uuid4().hex,local,_json({**create,'active':False}),_json(create)))
                            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','tasks.sync.coalesced',?)",(str(local),))
                            payload=_json(create)
                    shadow=db.execute('SELECT task_json FROM linux_task_sync_shadow WHERE task_id=?',(local,)).fetchone()
                    creator=json.loads(shadow[0])['createdBy'] if shadow else actor
                    value={'clientId':client_id,'operationId':op,'action':action,'task':_wire(db,json.loads(payload),creator),'expected':_wire(db,json.loads(expected),creator) if expected else None}
                    frozen=json.loads(wire) if wire is not None else None
                    preflight=None
                    needs_reservation=(frozen is not None and 'recurrenceSuccessorId' in frozen) or (frozen is None and capabilities.get('taskRecurrenceReservation')==1) or (frozen is not None and 'recurrenceSuccessorId' not in frozen and capabilities.get('taskRecurrencePreflight')==1)
                    if needs_reservation and value['task']['status']=='done' and value['task']['recurrenceDays']>0:
                        successors=set()
                        for (subject,) in db.execute("SELECT subject FROM audit_log WHERE action='task.recurrence.created' AND subject LIKE ?",(str(local)+'->%',)):
                            match=re.fullmatch(r'([1-9][0-9]*)->([1-9][0-9]*)',subject)
                            if not match or int(match.group(1))!=local:raise LocalStoreError('Ungültiges Wiederholungsprotokoll.')
                            successor=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='task' AND local_id=?",(int(match.group(2)),)).fetchone()
                            if not successor:raise LocalStoreError('Die Kennung der Folgeaufgabe fehlt.')
                            successors.add(successor[0])
                        if len(successors)>1:raise LocalStoreError('Mehrere Folgeaufgaben sind derselben Aufgabe zugeordnet.')
                        if successors:
                            successor=next(iter(successors))
                            if frozen is not None and 'recurrenceSuccessorId' not in frozen:
                                preflight={'clientId':client_id,'operationId':uuid.uuid5(uuid.NAMESPACE_URL,_json([client_id,op,successor,'recurrence-preflight'])).hex,'original':value,'successorId':successor}
                            else:value['recurrenceSuccessorId']=successor
                    if wire is not None and json.loads(wire)!=value:raise LocalStoreError('Die Mitarbeiterzuordnung einer ausstehenden Aufgabenänderung hat sich geändert.')
                    if wire is None:db.execute('UPDATE linux_task_sync_outbox SET wire_json=? WHERE seq=?',(_json(value),seq))
            if not row:break
            if preflight:command('-action','task-recurrence-reserve','-payload',_json(preflight))
            command('-action','sync-task-event','-payload',_json(value))
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT operation_id,wire_json FROM linux_task_sync_outbox WHERE seq=? AND NOT EXISTS(SELECT 1 FROM linux_task_sync_outbox older WHERE older.task_id=linux_task_sync_outbox.task_id AND older.seq<linux_task_sync_outbox.seq)',(seq,)).fetchone()!=(op,_json(value)):raise LocalStoreError('Die Aufgabenwarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_task_sync_shadow VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET task_json=excluded.task_json',(local,_json(value['task'])))
                db.execute('DELETE FROM linux_task_sync_outbox WHERE seq=?',(seq,));sent+=1
            sent+=dependency_sync.drain(store,lock,is_current,command,client_id,url,actor)
            sent+=subtask_sync.drain(store,lock,is_current,command,client_id,url,actor)
        sent+=dependency_sync.drain(store,lock,is_current,command,client_id,url,actor)
        sent+=subtask_sync.drain(store,lock,is_current,command,client_id,url,actor)
        if capabilities.get('taskRecurrenceLink')==1:
            with _db(store,lock,is_current) as db:
                links=[]
                for (subject,) in db.execute("SELECT subject FROM audit_log WHERE action='task.recurrence.created' ORDER BY id"):
                    match=re.fullmatch(r'([1-9][0-9]*)->([1-9][0-9]*)',subject)
                    if not match:raise LocalStoreError('Ungültiges Wiederholungsprotokoll; die Folgeaufgabe muss geprüft werden.')
                    source,successor=map(int,match.groups())
                    mapped=db.execute("""SELECT a.external_id,b.external_id FROM sync_object_links a JOIN sync_object_links b ON b.object_type='task' AND b.local_id=? JOIN linux_task_sync_shadow sa ON sa.task_id=a.local_id JOIN linux_task_sync_shadow sb ON sb.task_id=b.local_id WHERE a.object_type='task' AND a.local_id=?""",(successor,source)).fetchone()
                    if mapped:links.append(mapped)
            for source,successor in links:
                op=uuid.uuid5(uuid.NAMESPACE_URL,_json([client_id,source,successor])).hex
                command('-action','task-recurrence-link','-payload',_json({'clientId':client_id,'operationId':op,'sourceId':source,'successorId':successor}))
        with _db(store,lock,is_current) as db:
            pending=_only_unassigned_pending(db)
            if not pending and db.execute('SELECT 1 FROM linux_subtask_sync_outbox LIMIT 1').fetchone():raise LocalStoreError('Eine Unteraufgabe wartet auf eine noch nicht übertragene Hauptaufgabe.')
            if not pending and db.execute('SELECT 1 FROM linux_dependency_sync_outbox LIMIT 1').fetchone():raise LocalStoreError('Eine Voraussetzung wartet auf noch nicht übertragene Aufgaben.')
        dep_received=dependency_sync.pull(store,lock,is_current,command,defer_blocked=True)
        sub_received=subtask_sync.pull(store,lock,is_current,command)
        snapshot=command('-action','sync-tasks')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Aufgabenliste vom Server.')
        recurrence_links=command('-action','task-recurrence-links') if capabilities.get('taskRecurrenceLink')==1 else []
        if not isinstance(recurrence_links,list) or len(recurrence_links)>100000:raise LocalStoreError('Ungültige Folgeaufgabenliste vom Server.')
        seen_sources=set();seen_successors=set()
        for link in recurrence_links:
            if not isinstance(link,dict) or any(not isinstance(link.get(k),str) or not link[k].strip() or '\0' in link[k] for k in ('sourceId','successorId')) or link['sourceId']==link['successorId'] or link['sourceId'] in seen_sources or link['successorId'] in seen_successors:raise LocalStoreError('Ungültige oder widersprüchliche Folgeaufgabenliste vom Server.')
            seen_sources.add(link['sourceId']);seen_successors.add(link['successorId'])
        received=0
        with _db(store,lock,is_current) as db:
            pending=_only_unassigned_pending(db)
            db.execute('UPDATE linux_task_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','title','department','priority','status')) or remote['id'] in seen or remote['priority'] not in ('Niedrig','Mittel','Hoch','Kritisch') or remote['status'] not in ('open','in_progress','done','cancelled') or type(remote.get('assigneeId')) is not int or remote['assigneeId']<=0 or type(remote.get('createdBy')) is not int or remote['createdBy']<=0 or type(remote.get('active')) is not bool or type(remote.get('dueAt')) is not int or remote['dueAt']<0 or type(remote.get('recurrenceDays')) is not int or not 0<=remote['recurrenceDays']<=3660:raise LocalStoreError('Ungültige Aufgabe im Serverbestand.')
                seen.add(remote['id']);assigned=local_id(db,remote['assigneeId'])
                if assigned is None:raise LocalStoreError('Für die zuständige Serverperson fehlt eine eindeutige lokale Zuordnung: '+remote['title'])
                row=db.execute("SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0),h.task_json FROM sync_object_links l JOIN tasks t ON t.id=l.local_id LEFT JOIN task_schedules s ON s.task_id=t.id LEFT JOIN linux_task_sync_shadow h ON h.task_id=t.id WHERE l.object_type='task' AND l.external_id=?",(remote['id'],)).fetchone()
                if row:
                    local,title,department,priority,status,assignee,active,due,repeat,shadow=row
                    if db.execute('SELECT 1 FROM linux_task_sync_outbox WHERE task_id=?',(local,)).fetchone():continue
                    current={'id':remote['id'],'title':title,'department':department,'priority':priority,'status':status,'assigneeId':account(db,assignee),'active':bool(active),'dueAt':due,'recurrenceDays':repeat,'createdBy':json.loads(shadow)['createdBy'] if shadow else 0}
                    if not shadow or current!=json.loads(shadow):raise LocalStoreError('Aufgabenkonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand: '+title)
                    if current!=remote:
                        if remote['status']=='done' and (db.execute("SELECT 1 FROM task_subtasks WHERE task_id=? AND active=1 AND status NOT IN ('done','cancelled') LIMIT 1",(local,)).fetchone() or db.execute("SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_task_id WHERE d.task_id=? AND p.active=1 AND p.status<>'done' LIMIT 1",(local,)).fetchone()):raise LocalStoreError('Lokale Unteraufgaben oder Voraussetzungen verhindern die Übernahme des Abschlusses: '+title)
                        db.execute('UPDATE tasks SET title=?,department=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['title'],remote['department'],remote['priority'],remote['status'],assigned,int(remote['active']),local));received+=1
                else:
                    local=db.execute('INSERT INTO tasks(title,department,priority,status,assignee_id,active) VALUES(?,?,?,?,?,?)',(remote['title'],remote['department'],remote['priority'],remote['status'],assigned,int(remote['active']))).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('task',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET due_at=excluded.due_at,recurrence_days=excluded.recurrence_days',(local,remote['dueAt'],remote['recurrenceDays']))
                db.execute('INSERT INTO linux_task_sync_shadow VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET task_json=excluded.task_json',(local,_json(remote)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_task_sync_shadow s JOIN sync_object_links l ON l.object_type='task' AND l.local_id=s.task_id"):
                if external not in seen:raise LocalStoreError('Eine zuvor synchronisierte Aufgabe fehlt auf dem Server.')
            for link in recurrence_links:
                rows=db.execute("""SELECT a.local_id,b.local_id FROM sync_object_links a JOIN sync_object_links b ON b.object_type='task' AND b.external_id=? JOIN linux_task_sync_shadow sa ON sa.task_id=a.local_id JOIN linux_task_sync_shadow sb ON sb.task_id=b.local_id WHERE a.object_type='task' AND a.external_id=?""",(link['successorId'],link['sourceId'])).fetchone()
                if not rows:continue
                source,successor=rows;subject=str(source)+'->'+str(successor)
                old=[v[0] for v in db.execute("SELECT subject FROM audit_log WHERE action='task.recurrence.created' AND subject LIKE ?",(str(source)+'->%',))]
                if old and any(value!=subject for value in old):raise LocalStoreError('Folgeaufgabenkonflikt: Lokal und zentral sind unterschiedliche Folgeaufgaben zugeordnet.')
                if not old:db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('sync.remote','task.recurrence.created',?)",(subject,));received+=1
            db.execute('UPDATE linux_task_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','tasks.sync.pulled',?)",(str(received),))
        sub_received+=subtask_sync.pull(store,lock,is_current,command)
        dep_received+=dependency_sync.pull(store,lock,is_current,command)
        if pending:raise TaskAssigneePendingError('Eine neue Aufgabe wartet auf die Zuordnung einer zuständigen Person. Andere verfügbare Aufgaben wurden abgeglichen.',received+sub_received+dep_received)
        return {'sent':sent,'received':received+sub_received+dep_received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Aufgabenabgleich fehlgeschlagen: '+str(exc)) from exc


def _preview(db,url,actor):
    config=db.execute('SELECT server_url,server_actor FROM linux_task_sync_config').fetchone()
    if config!=(url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Server oder Serverkonto.')
    row=db.execute("""SELECT o.seq,o.operation_id,o.task_id,l.external_id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,
        COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0),h.task_json
        FROM linux_task_sync_outbox o JOIN tasks t ON t.id=o.task_id
        JOIN sync_object_links l ON l.object_type='task' AND l.local_id=t.id
        LEFT JOIN task_schedules s ON s.task_id=t.id LEFT JOIN linux_task_sync_shadow h ON h.task_id=t.id
        ORDER BY o.seq LIMIT 1""").fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Aufgabenänderung.')
    seq,operation,local,external,title,department,priority,status,assignee,active,due,repeat,shadow=row
    current={'id':external,'title':title,'department':department,'priority':priority,'status':status,'assigneeId':assignee,'active':bool(active),'dueAt':due,'recurrenceDays':repeat}
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,task_json,expected_json,wire_json FROM linux_task_sync_outbox WHERE task_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'local':current,'localServerAccount':account(db,assignee),'shadow':json.loads(shadow) if shadow else None,'pending':pending}


def _validate_remote(remote):
    if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','title','department','priority','status')) or remote['priority'] not in ('Niedrig','Mittel','Hoch','Kritisch') or remote['status'] not in ('open','in_progress','done','cancelled') or type(remote.get('assigneeId')) is not int or remote['assigneeId']<=0 or type(remote.get('createdBy')) is not int or remote['createdBy']<=0 or type(remote.get('active')) is not bool or type(remote.get('dueAt')) is not int or remote['dueAt']<0 or type(remote.get('recurrenceDays')) is not int or not 0<=remote['recurrenceDays']<=3660:
        raise LocalStoreError('Ungültige Aufgabe im Serverbestand.')
    return remote


def _validate_task_identity_mapping(db,preview,remote):
    config=db.execute('SELECT client_id FROM linux_task_sync_config WHERE singleton=1').fetchone()
    if not config or preview['local']['id']!=config[0]+':task:'+str(preview['localId']):
        raise LocalStoreError('Die lokale Aufgabe besitzt bereits eine fremde oder zuvor zugeordnete Kennung.')
    if db.execute("SELECT 1 FROM sync_object_links WHERE object_type='task' AND external_id=?",(remote['id'],)).fetchone():
        raise LocalStoreError('Die Serveraufgabe ist bereits einer lokalen Aufgabe zugeordnet.')
    # A desktop outbox can contain externally transmitted references unknown to
    # the Linux outbox. Never silently reassign such a shared database.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_subtask_sync_config'").fetchone() and db.execute('SELECT 1 FROM desktop_subtask_sync_config LIMIT 1').fetchone():
        raise LocalStoreError('Diese Datenbank ist auch für den Windows-Abgleich eingerichtet; die Aufgabenkennung kann hier nicht sicher zugeordnet werden.')

def inspect_task_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'taskSync','canManageTasks','tasks.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        tasks=command('-action','sync-tasks')
        if not isinstance(tasks,list):raise LocalStoreError('Ungültige Aufgabenliste vom Server.')
        matches=[_validate_remote(t) for t in tasks if isinstance(t,dict) and t.get('id')==preview['local']['id']]
        identity_conflict=False
        if not matches:
            pending=preview['pending']
            if preview['shadow'] is not None or not pending or pending[0][2]!='create' or any(op[5] is not None for op in pending[1:]):
                raise LocalStoreError('Diese Aufgabenanlage wurde bereits bestätigt oder eine spätere Übertragung vorbereitet.')
            frozen=json.loads(pending[0][5]) if pending[0][5] is not None else None
            if frozen is not None and (not isinstance(frozen,dict) or frozen.get('action')!='create' or frozen.get('expected') is not None or frozen.get('operationId')!=pending[0][1] or not isinstance(frozen.get('task'),dict) or frozen['task'].get('id')!=preview['local']['id']):raise LocalStoreError('Die vorbereitete Aufgabenübertragung ist ungültig.')
            matches=[_validate_remote(t) for t in tasks if isinstance(t,dict) and isinstance(t.get('title'),str) and t['title'].strip().casefold()==preview['local']['title'].strip().casefold() and t.get('id')!=preview['local']['id']]
            if len(matches)!=1:raise LocalStoreError('Für diese Aufgabe gibt es keine eindeutig gleichnamige Serveraufgabe zur Zuordnung.')
            with _db(store,lock,is_current) as db:
                if _preview(db,url,actor)!=preview:raise LocalStoreError('Die lokale Aufgabenänderung hat sich geändert. Bitte erneut prüfen.')
                _validate_task_identity_mapping(db,preview,matches[0])
            identity_conflict=True
        if len(matches)!=1:raise LocalStoreError('Die Serveraufgabe ist nicht eindeutig.')
        result={'preview':preview,'remote':matches[0],'url':url,'actor':actor}
        if identity_conflict:
            result['identityConflict']=True
            if frozen is not None:result['preparedOperation']=frozen
        return result
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Aufgabenkonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_task_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    fresh=inspect_task_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    if reviewed.get('preparedOperation') is not None:
        normalized,actor,command=connect_sync_server(client,url,token,ca,'taskIdentityReconciliation','canManageTasks','tasks.manage')
        if normalized!=reviewed['url'] or actor!=reviewed['actor']:raise LocalStoreError('Die Serverbindung hat sich geändert.')
        with _db(store,lock,is_current) as db:
            if _preview(db,normalized,actor)!=reviewed['preview']:raise LocalStoreError('Die lokale Aufgabenänderung hat sich geändert. Bitte erneut prüfen.')
            _validate_task_identity_mapping(db,reviewed['preview'],reviewed['remote'])
        command('-action','abandon-task-event','-payload',_json({'operation':reviewed['preparedOperation'],'remote':reviewed['remote']}))
        if inspect_task_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich nach der Serversperre geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Aufgabenänderung hat sich geändert. Bitte erneut prüfen.')
            local=preview['localId'];current=preview['local'];remote=reviewed['remote'];assigned=local_id(db,remote['assigneeId'])
            if reviewed.get('identityConflict'):
                if preview['shadow'] is not None or not preview['pending'] or preview['pending'][0][2]!='create' or any(op[5] is not None for op in preview['pending'][1:]) or (preview['pending'][0][5] is not None and reviewed.get('preparedOperation') is None):raise LocalStoreError('Die Aufgabenanlage wurde zwischenzeitlich vorbereitet oder bestätigt.')
                _validate_task_identity_mapping(db,preview,remote)
                changed=db.execute("UPDATE sync_object_links SET external_id=? WHERE object_type='task' AND local_id=? AND external_id=?",(remote['id'],local,current['id'])).rowcount
                if changed!=1:raise LocalStoreError('Die lokale Aufgabenkennung hat sich geändert.')
                current={**current,'id':remote['id']}
            if assigned is None:raise LocalStoreError('Die zuständige Serverperson besitzt keine eindeutige lokale Zuordnung.')
            if accept_server and remote['active'] and remote['status']=='done' and (db.execute("SELECT 1 FROM task_subtasks WHERE task_id=? AND active=1 AND status NOT IN ('done','cancelled') LIMIT 1",(local,)).fetchone() or db.execute("SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_task_id WHERE d.task_id=? AND p.active=1 AND p.status<>'done' LIMIT 1",(local,)).fetchone()):
                raise LocalStoreError('Lokale Unteraufgaben oder Voraussetzungen verhindern die Übernahme des Abschlusses.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_task_sync_resolutions(id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_task_sync_resolutions(task_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(local,'server' if accept_server else 'local',_json(current),_json(remote),_json(preview['pending'])))
            db.execute('INSERT INTO linux_task_sync_shadow VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET task_json=excluded.task_json',(local,_json(remote)))
            if accept_server:
                db.execute('UPDATE linux_task_sync_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE tasks SET title=?,department=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['title'],remote['department'],remote['priority'],remote['status'],assigned,int(remote['active']),local))
                db.execute('INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET due_at=excluded.due_at,recurrence_days=excluded.recurrence_days',(local,remote['dueAt'],remote['recurrenceDays']))
                db.execute('UPDATE linux_task_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM linux_task_sync_outbox WHERE task_id=?',(local,))
            else:
                expected={k:remote[k] for k in ('id','title','department','priority','status','assigneeId','active','dueAt','recurrenceDays')};expected['assigneeId']=assigned
                action='archive' if remote['active'] and not current['active'] else 'update'
                db.execute('DELETE FROM linux_task_sync_outbox WHERE task_id=? AND seq<>?',(local,preview['seq']))
                db.execute('UPDATE linux_task_sync_outbox SET operation_id=?,action=?,task_json=?,expected_json=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,action,_json(current),_json(expected),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync',('tasks.sync.identity_mapped' if reviewed.get('identityConflict') else ('tasks.sync.accepted_server' if accept_server else 'tasks.sync.rebased')),str(local)+(':'+preview['local']['id']+' -> '+remote['id'] if reviewed.get('identityConflict') else '')))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Aufgabenkonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def task_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_task_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_task_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Aufgabenkonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

"""Durable synchronization of local project/task relationships."""
import json
import sqlite3
import uuid
from employee_sync import _db
from local_store import LocalStoreError
from room_sync import connect_sync_server


class AssignmentParentsPendingError(LocalStoreError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _binding(db, url, actor):
    for name, label in (('linux_project_sync_config', 'Projekt'), ('linux_task_sync_config', 'Aufgaben')):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
            raise LocalStoreError('Bitte zuerst den '+label+'abgleich für dieses Serverkonto einrichten.')
        if db.execute('SELECT server_url,server_actor FROM '+name+' WHERE singleton=1').fetchone() != (url, actor):
            raise LocalStoreError('Projekt- und Aufgabenabgleich müssen an denselben Server und dasselbe Serverkonto gebunden sein.')


def setup(db, url, actor):
    _binding(db, url, actor)
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_assignment_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_assignment_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,expected INTEGER NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_assignment_sync_shadow(project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(project_id,task_id))')
    db.execute('CREATE TABLE IF NOT EXISTS linux_project_assignment_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_project_assignment_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_project_assignment_sync_config').fetchone()
    if config:
        if config[1:] != (url,actor):raise LocalStoreError('Die Zuordnungswarteschlange ist an einen anderen Server oder ein anderes Konto gebunden.')
        return config[0]
    client='linux-'+uuid.uuid4().hex
    db.execute('INSERT INTO linux_project_assignment_sync_config VALUES(1,?,?,?)',(client,url,actor))
    db.execute('''CREATE TRIGGER linux_project_assignment_sync_insert AFTER INSERT ON project_tasks WHEN (SELECT suppressed FROM linux_project_assignment_sync_guard)=0 BEGIN
      INSERT INTO linux_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected) VALUES(lower(hex(randomblob(16))),NEW.project_id,NEW.task_id,1,0);
    END''')
    db.execute('''CREATE TRIGGER linux_project_assignment_sync_delete AFTER DELETE ON project_tasks WHEN (SELECT suppressed FROM linux_project_assignment_sync_guard)=0 BEGIN
      INSERT INTO linux_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected) VALUES(lower(hex(randomblob(16))),OLD.project_id,OLD.task_id,0,1);
    END''')
    for project,task in db.execute('SELECT project_id,task_id FROM project_tasks ORDER BY project_id,task_id').fetchall():
        db.execute('INSERT INTO linux_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected) VALUES(?,?,?,1,0)',(uuid.uuid4().hex,project,task))
    return client


def connect(client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'projectAssignmentSync','canManageProjects','projects.manage')
    if not command('-action','identity').get('canManageTasks'):raise LocalStoreError('Das Serverkonto benötigt die Berechtigung tasks.manage.')
    return url,actor,command


def sync_project_assignments(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect(client,url,token,ca)
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                _binding(db,url,actor)
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_project_assignment_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Zuordnungswarteschlange gehört zu einem anderen Serverkonto.')
                # Strict global order is needed for moves (remove old, then add new).
                row=db.execute('''SELECT o.seq,o.operation_id,o.project_id,o.task_id,o.present,o.expected,o.wire_json,p.external_id,t.external_id
                  FROM linux_project_assignment_sync_outbox o LEFT JOIN linux_project_sync_shadow ps ON ps.project_id=o.project_id
                  LEFT JOIN linux_task_sync_shadow ts ON ts.task_id=o.task_id
                  LEFT JOIN sync_object_links p ON p.object_type='project' AND p.local_id=o.project_id
                  LEFT JOIN sync_object_links t ON t.object_type='task' AND t.local_id=o.task_id
                  WHERE o.seq=(SELECT min(seq) FROM linux_project_assignment_sync_outbox) AND ps.project_id IS NOT NULL AND ts.task_id IS NOT NULL''').fetchone()
                if row:
                    seq,op,project,task,present,expected,frozen,ext_project,ext_task=row
                    if not ext_project or not ext_task:raise LocalStoreError('Eine Projekt- oder Aufgabenkennung fehlt.')
                    data={'clientId':client_id,'operationId':op,'projectId':ext_project,'taskId':ext_task,'present':bool(present),'expected':bool(expected)}
                    if frozen is not None and json.loads(frozen)!=data:raise LocalStoreError('Eine ausstehende Zuordnung hat sich geändert.')
                    if frozen is None:db.execute('UPDATE linux_project_assignment_sync_outbox SET wire_json=? WHERE seq=?',(encode(data),seq))
                elif db.execute('SELECT 1 FROM linux_project_assignment_sync_outbox LIMIT 1').fetchone():
                    raise AssignmentParentsPendingError('Zuerst die beteiligten Projekte und Aufgaben synchronisieren. Die Zuordnung bleibt vorgemerkt.')
            if not row:break
            command('-action','sync-project-assignment-event','-payload',encode(data))
            with _db(store,lock,is_current) as db:
                _binding(db,url,actor)
                if db.execute('SELECT wire_json FROM linux_project_assignment_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(data),):raise LocalStoreError('Die Zuordnungswarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_project_assignment_sync_shadow VALUES(?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET present=excluded.present',(project,task,present))
                db.execute('DELETE FROM linux_project_assignment_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-project-assignments')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Projektzuordnungsliste vom Server.')
        changed=0
        with _db(store,lock,is_current) as db:
            _binding(db,url,actor)
            db.execute('UPDATE linux_project_assignment_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for edge in snapshot:
                if not isinstance(edge,dict) or any(not isinstance(edge.get(k),str) or not edge[k].strip() or '\0' in edge[k] for k in ('projectId','taskId')) or (edge['projectId'],edge['taskId']) in seen:raise LocalStoreError('Ungültige oder doppelte Projektzuordnung im Serverbestand.')
                seen.add((edge['projectId'],edge['taskId']))
                project=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_project_sync_shadow s ON s.project_id=l.local_id WHERE l.object_type='project' AND l.external_id=?",(edge['projectId'],)).fetchone()
                task=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_task_sync_shadow s ON s.task_id=l.local_id WHERE l.object_type='task' AND l.external_id=?",(edge['taskId'],)).fetchone()
                if not project or not task:continue
                project,task=project[0],task[0]
                if db.execute('SELECT 1 FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=?',(project,task)).fetchone():continue
                current=bool(db.execute('SELECT 1 FROM project_tasks WHERE project_id=? AND task_id=?',(project,task)).fetchone())
                shadow=db.execute('SELECT present FROM linux_project_assignment_sync_shadow WHERE project_id=? AND task_id=?',(project,task)).fetchone()
                if (shadow is not None and current!=bool(shadow[0])) or (shadow is None and current):raise LocalStoreError('Projektzuordnungskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand.')
                if not current:db.execute('INSERT INTO project_tasks(project_id,task_id) VALUES(?,?)',(project,task));changed+=1
                db.execute('INSERT INTO linux_project_assignment_sync_shadow VALUES(?,?,1) ON CONFLICT(project_id,task_id) DO UPDATE SET present=1',(project,task))
            known=db.execute('''SELECT s.project_id,s.task_id,s.present,p.external_id,t.external_id FROM linux_project_assignment_sync_shadow s
              JOIN sync_object_links p ON p.object_type='project' AND p.local_id=s.project_id JOIN sync_object_links t ON t.object_type='task' AND t.local_id=s.task_id''').fetchall()
            for project,task,shadow,ep,et in known:
                if (ep,et) in seen or db.execute('SELECT 1 FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=?',(project,task)).fetchone():continue
                current=bool(db.execute('SELECT 1 FROM project_tasks WHERE project_id=? AND task_id=?',(project,task)).fetchone())
                if current!=bool(shadow):raise LocalStoreError('Projektzuordnungskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand.')
                if current:db.execute('DELETE FROM project_tasks WHERE project_id=? AND task_id=?',(project,task));changed+=1
                db.execute('UPDATE linux_project_assignment_sync_shadow SET present=0 WHERE project_id=? AND task_id=?',(project,task))
            db.execute('UPDATE linux_project_assignment_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','project_assignments.sync.pulled',?)",(str(changed),))
        return {'sent':sent,'received':changed}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Projektzuordnungsabgleich fehlgeschlagen: '+str(exc)) from exc


def _preview(db,url,actor):
    _binding(db,url,actor)
    if db.execute('SELECT server_url,server_actor FROM linux_project_assignment_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Zuordnungswarteschlange gehört zu einem anderen Serverkonto.')
    row=db.execute('''SELECT o.seq,o.operation_id,o.project_id,o.task_id,p.external_id,t.external_id,pr.name,ta.title,s.present
       FROM linux_project_assignment_sync_outbox o JOIN linux_project_sync_shadow ps ON ps.project_id=o.project_id
       JOIN linux_task_sync_shadow ts ON ts.task_id=o.task_id
       JOIN sync_object_links p ON p.object_type='project' AND p.local_id=o.project_id
       JOIN sync_object_links t ON t.object_type='task' AND t.local_id=o.task_id
       JOIN projects pr ON pr.id=o.project_id JOIN tasks ta ON ta.id=o.task_id
       LEFT JOIN linux_project_assignment_sync_shadow s ON s.project_id=o.project_id AND s.task_id=o.task_id
       WHERE o.seq=(SELECT min(seq) FROM linux_project_assignment_sync_outbox)''').fetchone()
    if not row:raise LocalStoreError('Keine ausstehende Zuordnung mit bestätigtem Projekt und bestätigter Aufgabe vorhanden.')
    seq,op,project,task,ep,et,name,title,shadow=row
    current=bool(db.execute('SELECT 1 FROM project_tasks WHERE project_id=? AND task_id=?',(project,task)).fetchone())
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,present,expected,wire_json FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=? ORDER BY seq',(project,task))]
    return {'seq':seq,'operationId':op,'project':project,'task':task,'projectId':ep,'taskId':et,'projectName':name,'taskTitle':title,'localPresent':current,'shadow':None if shadow is None else bool(shadow),'pending':pending}


def inspect_assignment_conflict(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect(client,url,token,ca)
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        edges=command('-action','sync-project-assignments')
        if not isinstance(edges,list) or len(edges)>100000:raise LocalStoreError('Ungültige Projektzuordnungsliste vom Server.')
        seen=set()
        for edge in edges:
            if not isinstance(edge,dict) or any(not isinstance(edge.get(k),str) or not edge[k].strip() or '\0' in edge[k] for k in ('projectId','taskId')) or (edge['projectId'],edge['taskId']) in seen:raise LocalStoreError('Ungültige oder doppelte Projektzuordnung im Serverbestand.')
            seen.add((edge['projectId'],edge['taskId']))
        return {'preview':preview,'remotePresent':(preview['projectId'],preview['taskId']) in seen,'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Zuordnungskonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_assignment_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    if inspect_assignment_conflict(store,lock,is_current,client,url,token,ca)!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Zuordnung oder Warteschlange hat sich geändert. Bitte erneut prüfen.')
            project,task=preview['project'],preview['task'];local=preview['localPresent'];remote=reviewed['remotePresent']
            db.execute('CREATE TABLE IF NOT EXISTS linux_project_assignment_sync_conflicts(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,choice TEXT NOT NULL,local_present INTEGER NOT NULL,remote_present INTEGER NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_project_assignment_sync_conflicts(project_id,task_id,choice,local_present,remote_present,previous_operations) VALUES(?,?,?,?,?,?)',(project,task,'server' if accept_server else 'local',int(local),int(remote),encode(preview['pending'])))
            db.execute('INSERT INTO linux_project_assignment_sync_shadow VALUES(?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET present=excluded.present',(project,task,int(remote)))
            if accept_server:
                db.execute('UPDATE linux_project_assignment_sync_guard SET suppressed=1 WHERE singleton=1')
                if remote and not local:db.execute('INSERT INTO project_tasks(project_id,task_id) VALUES(?,?)',(project,task))
                if not remote and local:db.execute('DELETE FROM project_tasks WHERE project_id=? AND task_id=?',(project,task))
                db.execute('UPDATE linux_project_assignment_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=?',(project,task))
            elif local==remote:db.execute('DELETE FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=?',(project,task))
            else:
                db.execute('DELETE FROM linux_project_assignment_sync_outbox WHERE project_id=? AND task_id=? AND seq<>?',(project,task,preview['seq']))
                db.execute('UPDATE linux_project_assignment_sync_outbox SET operation_id=?,present=?,expected=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,int(local),int(remote),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','project_assignments.sync.accepted_server' if accept_server else 'project_assignments.sync.rebased',str(project)+':'+str(task)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Zuordnungskonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def assignment_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_project_assignment_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_project_assignment_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Zuordnungskonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

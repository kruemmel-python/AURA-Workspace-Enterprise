"""Durable synchronization of task prerequisite edges."""
import json
import sqlite3
import uuid
from employee_sync import _db
from local_store import LocalStoreError

def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS linux_dependency_sync_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1))')
    db.execute('CREATE TABLE IF NOT EXISTS linux_dependency_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,present INTEGER NOT NULL,expected INTEGER NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_dependency_sync_shadow(task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(task_id,dependency_id))')
    db.execute('CREATE TABLE IF NOT EXISTS linux_dependency_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_dependency_sync_guard VALUES(1,0)')
    if db.execute('SELECT 1 FROM linux_dependency_sync_meta').fetchone():return
    db.execute('INSERT INTO linux_dependency_sync_meta VALUES(1)')
    db.execute('''CREATE TRIGGER linux_dependency_sync_insert AFTER INSERT ON task_dependencies WHEN (SELECT suppressed FROM linux_dependency_sync_guard)=0 BEGIN
      INSERT INTO linux_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected) VALUES(lower(hex(randomblob(16))),NEW.task_id,NEW.depends_on_task_id,1,0);
    END''')
    db.execute('''CREATE TRIGGER linux_dependency_sync_delete AFTER DELETE ON task_dependencies WHEN (SELECT suppressed FROM linux_dependency_sync_guard)=0 BEGIN
      INSERT INTO linux_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected) VALUES(lower(hex(randomblob(16))),OLD.task_id,OLD.depends_on_task_id,0,1);
    END''')
    for task,dependency in db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies ORDER BY task_id,depends_on_task_id').fetchall():
        db.execute('INSERT INTO linux_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected) VALUES(?,?,?,1,0)',(uuid.uuid4().hex,task,dependency))


def drain(store,lock,is_current,command,client_id,url,actor):
    sent=0
    while True:
        with _db(store,lock,is_current) as db:
            if db.execute('SELECT client_id,server_url,server_actor FROM linux_task_sync_config').fetchone()!=(client_id,url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Serverkonto.')
            row=db.execute("""SELECT o.seq,o.operation_id,o.task_id,o.dependency_id,o.present,o.expected,o.wire_json,a.external_id,b.external_id
              FROM linux_dependency_sync_outbox o JOIN linux_task_sync_shadow x ON x.task_id=o.task_id JOIN linux_task_sync_shadow y ON y.task_id=o.dependency_id
              JOIN sync_object_links a ON a.object_type='task' AND a.local_id=o.task_id JOIN sync_object_links b ON b.object_type='task' AND b.local_id=o.dependency_id
              WHERE o.seq=(SELECT min(z.seq) FROM linux_dependency_sync_outbox z WHERE z.task_id=o.task_id AND z.dependency_id=o.dependency_id) ORDER BY o.seq LIMIT 1""").fetchone()
            if row:
                seq,op,task,dependency,present,expected,frozen,external,external_dep=row
                data={'clientId':client_id,'operationId':op,'taskId':external,'dependencyId':external_dep,'present':bool(present),'expected':bool(expected)}
                if frozen is not None and json.loads(frozen)!=data:raise LocalStoreError('Eine Aufgabenzuordnung der ausstehenden Voraussetzung hat sich geändert.')
                if frozen is None:db.execute('UPDATE linux_dependency_sync_outbox SET wire_json=? WHERE seq=?',(encode(data),seq))
        if not row:return sent
        command('-action','sync-dependency-event','-payload',encode(data))
        with _db(store,lock,is_current) as db:
            if db.execute('SELECT wire_json FROM linux_dependency_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(data),):raise LocalStoreError('Die Voraussetzungenwarteschlange wurde geändert.')
            db.execute('INSERT INTO linux_dependency_sync_shadow VALUES(?,?,?) ON CONFLICT(task_id,dependency_id) DO UPDATE SET present=excluded.present',(task,dependency,present))
            db.execute('DELETE FROM linux_dependency_sync_outbox WHERE seq=?',(seq,));sent+=1


def pull(store,lock,is_current,command,defer_blocked=False):
    snapshot=command('-action','sync-dependencies')
    if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Voraussetzungenliste vom Server.')
    changed=0
    with _db(store,lock,is_current) as db:
        db.execute('UPDATE linux_dependency_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
        for edge in snapshot:
            if not isinstance(edge,dict) or any(not isinstance(edge.get(k),str) or not edge[k].strip() or '\0' in edge[k] for k in ('taskId','dependencyId')) or edge['taskId']==edge['dependencyId'] or (edge['taskId'],edge['dependencyId']) in seen:raise LocalStoreError('Ungültige oder doppelte Voraussetzung im Serverbestand.')
            seen.add((edge['taskId'],edge['dependencyId']))
            def local(external):
                row=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_task_sync_shadow h ON h.task_id=l.local_id WHERE l.object_type='task' AND l.external_id=?",(external,)).fetchone()
                return row[0] if row else None
            task,dependency=local(edge['taskId']),local(edge['dependencyId'])
            if task is None or dependency is None:continue
            if db.execute('SELECT 1 FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=?',(task,dependency)).fetchone():continue
            current=bool(db.execute('SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(task,dependency)).fetchone())
            shadow=db.execute('SELECT present FROM linux_dependency_sync_shadow WHERE task_id=? AND dependency_id=?',(task,dependency)).fetchone()
            if (shadow is not None and current!=bool(shadow[0])) or (shadow is None and current):raise LocalStoreError('Voraussetzungskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand.')
            if not current:
                if db.execute("SELECT 1 FROM tasks t JOIN tasks p ON p.id=? WHERE t.id=? AND t.active=1 AND t.status='done' AND p.active=1 AND p.status<>'done'",(dependency,task)).fetchone():
                    if defer_blocked:continue
                    raise LocalStoreError('Eine offene Voraussetzung kann nicht zu einer lokal abgeschlossenen Aufgabe übernommen werden.')
                if db.execute('WITH RECURSIVE reach(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN reach r ON d.task_id=r.id) SELECT 1 FROM reach WHERE id=? LIMIT 1',(dependency,task)).fetchone():
                    if defer_blocked:continue
                    raise LocalStoreError('Die Servervoraussetzung würde lokal einen Zyklus erzeugen.')
                db.execute('INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)',(task,dependency));changed+=1
            db.execute('INSERT INTO linux_dependency_sync_shadow VALUES(?,?,1) ON CONFLICT(task_id,dependency_id) DO UPDATE SET present=1',(task,dependency))
        known=db.execute("""SELECT s.task_id,s.dependency_id,s.present,a.external_id,b.external_id FROM linux_dependency_sync_shadow s
          JOIN sync_object_links a ON a.object_type='task' AND a.local_id=s.task_id JOIN sync_object_links b ON b.object_type='task' AND b.local_id=s.dependency_id""").fetchall()
        for task,dependency,shadow,ext,ext_dep in known:
            if (ext,ext_dep) in seen:continue
            if db.execute('SELECT 1 FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=?',(task,dependency)).fetchone():continue
            current=bool(db.execute('SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(task,dependency)).fetchone())
            if current!=bool(shadow):raise LocalStoreError('Voraussetzungskonflikt: Der lokale Stand hat keinen gemeinsamen Ausgangszustand.')
            if current:db.execute('DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(task,dependency));changed+=1
            db.execute('UPDATE linux_dependency_sync_shadow SET present=0 WHERE task_id=? AND dependency_id=?',(task,dependency))
        db.execute('UPDATE linux_dependency_sync_guard SET suppressed=0 WHERE singleton=1')
        db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','dependencies.sync.pulled',?)",(str(changed),))
    return changed


def _preview(db,url,actor):
    if db.execute('SELECT server_url,server_actor FROM linux_task_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Aufgabenwarteschlange gehört zu einem anderen Server oder Serverkonto.')
    row=db.execute("""SELECT o.seq,o.operation_id,o.task_id,o.dependency_id,a.external_id,b.external_id,
            t.title,t.status,t.active,p.title,p.status,p.active,s.present
        FROM linux_dependency_sync_outbox o
        JOIN linux_task_sync_shadow x ON x.task_id=o.task_id JOIN linux_task_sync_shadow y ON y.task_id=o.dependency_id
        JOIN sync_object_links a ON a.object_type='task' AND a.local_id=o.task_id
        JOIN sync_object_links b ON b.object_type='task' AND b.local_id=o.dependency_id
        JOIN tasks t ON t.id=o.task_id JOIN tasks p ON p.id=o.dependency_id
        LEFT JOIN linux_dependency_sync_shadow s ON s.task_id=o.task_id AND s.dependency_id=o.dependency_id
        WHERE o.seq=(SELECT min(z.seq) FROM linux_dependency_sync_outbox z WHERE z.task_id=o.task_id AND z.dependency_id=o.dependency_id)
        ORDER BY o.seq LIMIT 1""").fetchone()
    if not row:raise LocalStoreError('Es gibt keine ausstehende Voraussetzung mit zwei bestätigten Aufgaben.')
    seq,op,task,dep,task_ext,dep_ext,title,status,active,dep_title,dep_status,dep_active,shadow=row
    current=bool(db.execute('SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(task,dep)).fetchone())
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,present,expected,wire_json FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=? ORDER BY seq',(task,dep))]
    return {'seq':seq,'operationId':op,'localTaskId':task,'localDependencyId':dep,'taskId':task_ext,'dependencyId':dep_ext,'title':title,'status':status,'active':bool(active),'dependencyTitle':dep_title,'dependencyStatus':dep_status,'dependencyActive':bool(dep_active),'localPresent':current,'shadow':None if shadow is None else bool(shadow),'pending':pending}


def inspect_dependency_conflict(store,lock,is_current,client,url,token,ca):
    from room_sync import connect_sync_server
    url,actor,command=connect_sync_server(client,url,token,ca,'dependencySync','canManageTasks','tasks.manage')
    try:
        with _db(store,lock,is_current) as db:preview=_preview(db,url,actor)
        edges=command('-action','sync-dependencies');tasks=command('-action','sync-tasks')
        if not isinstance(edges,list) or not isinstance(tasks,list):raise LocalStoreError('Ungültiger Serverbestand.')
        seen=set()
        for edge in edges:
            if not isinstance(edge,dict) or any(not isinstance(edge.get(k),str) or not edge[k].strip() or '\0' in edge[k] for k in ('taskId','dependencyId')) or edge['taskId']==edge['dependencyId'] or (edge['taskId'],edge['dependencyId']) in seen:raise LocalStoreError('Ungültige oder doppelte Voraussetzung im Serverbestand.')
            seen.add((edge['taskId'],edge['dependencyId']))
        source=[v for v in tasks if isinstance(v,dict) and v.get('id')==preview['taskId']]
        target=[v for v in tasks if isinstance(v,dict) and v.get('id')==preview['dependencyId']]
        if len(source)!=1 or len(target)!=1:raise LocalStoreError('Für die beteiligten Aufgaben gibt es keine eindeutige Serverzuordnung.')
        for item in (source[0],target[0]):
            if not isinstance(item.get('title'),str) or not item['title'].strip() or item.get('status') not in ('open','in_progress','done','cancelled') or type(item.get('active')) is not bool:raise LocalStoreError('Ungültiger Aufgabenstand vom Server.')
        graph={}
        for a,b in seen:graph.setdefault(a,[]).append(b)
        stack=[preview['dependencyId']];visited=set();would_cycle=False
        while stack:
            current=stack.pop()
            if current==preview['taskId']:would_cycle=True;break
            if current in visited:continue
            visited.add(current);stack.extend(graph.get(current,()))
        remote={'present':(preview['taskId'],preview['dependencyId']) in seen,'task':{k:source[0][k] for k in ('id','title','status','active')},'dependency':{k:target[0][k] for k in ('id','title','status','active')},'wouldCycle':would_cycle}
        return {'preview':preview,'remote':remote,'url':url,'actor':actor}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Voraussetzungskonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_dependency_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    fresh=inspect_dependency_conflict(store,lock,is_current,client,url,token,ca)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    try:
        with _db(store,lock,is_current) as db:
            preview=_preview(db,reviewed['url'],reviewed['actor'])
            if preview!=reviewed['preview']:raise LocalStoreError('Die lokale Voraussetzung oder Warteschlange hat sich geändert. Bitte erneut prüfen.')
            task,dep=preview['localTaskId'],preview['localDependencyId'];local=preview['localPresent'];remote=reviewed['remote']['present']
            if accept_server and remote and not local:
                if preview['active'] and preview['status']=='done' and preview['dependencyActive'] and preview['dependencyStatus']!='done':raise LocalStoreError('Eine offene Voraussetzung kann nicht zu einer lokal abgeschlossenen Aufgabe übernommen werden.')
                if db.execute('WITH RECURSIVE reach(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN reach r ON d.task_id=r.id) SELECT 1 FROM reach WHERE id=? LIMIT 1',(dep,task)).fetchone():raise LocalStoreError('Die Servervoraussetzung würde lokal einen Zyklus erzeugen.')
            if not accept_server and local and not remote:
                if reviewed['remote']['wouldCycle']:raise LocalStoreError('Der lokale Stand würde auf dem Server einen Zyklus erzeugen. Bitte zuerst die andere Voraussetzung klären.')
                task_state=reviewed['remote']['task'];dep_state=reviewed['remote']['dependency']
                if task_state['active'] and task_state['status']=='done' and dep_state['active'] and dep_state['status']!='done':raise LocalStoreError('Der Server lässt eine offene Voraussetzung für eine abgeschlossene Aufgabe nicht zu.')
            db.execute('CREATE TABLE IF NOT EXISTS linux_dependency_sync_conflicts(id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,choice TEXT NOT NULL,local_present INTEGER NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO linux_dependency_sync_conflicts(task_id,dependency_id,choice,local_present,remote_json,previous_operations) VALUES(?,?,?,?,?,?)',(task,dep,'server' if accept_server else 'local',int(local),encode(reviewed['remote']),encode(preview['pending'])))
            db.execute('INSERT INTO linux_dependency_sync_shadow VALUES(?,?,?) ON CONFLICT(task_id,dependency_id) DO UPDATE SET present=excluded.present',(task,dep,int(remote)))
            if accept_server:
                db.execute('UPDATE linux_dependency_sync_guard SET suppressed=1 WHERE singleton=1')
                if remote and not local:db.execute('INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)',(task,dep))
                if not remote and local:db.execute('DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?',(task,dep))
                db.execute('UPDATE linux_dependency_sync_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=?',(task,dep))
            elif local==remote:
                db.execute('DELETE FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=?',(task,dep))
            else:
                db.execute('DELETE FROM linux_dependency_sync_outbox WHERE task_id=? AND dependency_id=? AND seq<>?',(task,dep,preview['seq']))
                db.execute('UPDATE linux_dependency_sync_outbox SET operation_id=?,present=?,expected=?,wire_json=NULL WHERE seq=? AND operation_id=?',(uuid.uuid4().hex,int(local),int(remote),preview['seq'],preview['operationId']))
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync','dependencies.sync.accepted_server' if accept_server else 'dependencies.sync.rebased',str(task)+':'+str(dep)))
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Voraussetzungskonflikt konnte nicht übernommen werden: '+str(exc)) from exc

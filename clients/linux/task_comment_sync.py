"""Explicit, durable import and export of task comments."""
import datetime
import json
import sqlite3
import uuid
from employee_sync import _db
from leave_sync import account,local_id,employee_external_id,transaction
from local_store import LocalStoreError
from room_sync import connect_sync_server
from personal_comment_pull import pull_personal_comments


class CommentTaskPendingError(LocalStoreError):
    pass


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def setup(db,url,actor):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_task_sync_config'").fetchone() or db.execute('SELECT server_url,server_actor FROM linux_task_sync_config').fetchone()!=(url,actor):
        raise LocalStoreError('Bitte zuerst Aufgaben für denselben Server und dasselbe Serverkonto abgleichen.')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,comment_id INTEGER NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_personal_claim(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_sync_shadow(comment_id INTEGER PRIMARY KEY,comment_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO linux_task_comment_sync_guard VALUES(1,0)')
    config=db.execute('SELECT client_id,server_url,server_actor FROM linux_task_comment_sync_config').fetchone()
    if config:
        if config[1:]!=(url,actor):raise LocalStoreError('Die Kommentarwarteschlange gehört zu einem anderen Serverkonto.')
        return config[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO linux_task_comment_sync_config VALUES(1,?,?,?)',(client,url,actor))
    db.execute('''CREATE TRIGGER linux_task_comment_sync_insert AFTER INSERT ON comments WHEN NEW.subject_type='task' AND (SELECT suppressed FROM linux_task_comment_sync_guard)=0 BEGIN
       INSERT OR IGNORE INTO sync_object_links SELECT 'comment',client_id||':comment:'||NEW.id,NEW.id FROM linux_task_comment_sync_config;
       INSERT INTO linux_task_comment_sync_outbox(operation_id,comment_id) VALUES(lower(hex(randomblob(16))),NEW.id);
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'comment',?||':comment:'||id,id FROM comments WHERE subject_type='task'",(client,))
    for (comment,) in db.execute("SELECT id FROM comments WHERE subject_type='task' ORDER BY id").fetchall():
        db.execute('INSERT INTO linux_task_comment_sync_outbox(operation_id,comment_id) VALUES(?,?)',(uuid.uuid4().hex,comment))
    return client


def _timestamp(value):
    try:
        date=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None:date=date.replace(tzinfo=datetime.timezone.utc)
        return date.astimezone(datetime.timezone.utc).isoformat().replace('+00:00','Z')
    except (ValueError,TypeError,OverflowError) as exc:raise LocalStoreError('Ungültiger Kommentarzeitpunkt.') from exc


def sync_task_comments(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'taskCommentSync','canManageTasks','tasks.manage')
    if not command('-action','identity').get('canManageEmployees'):raise LocalStoreError('Das Serverkonto benötigt die Berechtigung employees.manage.')
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,url,actor)
        sent=0
        while True:
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT client_id,server_url,server_actor FROM linux_task_comment_sync_config').fetchone()!=(client_id,url,actor) or db.execute('SELECT server_url,server_actor FROM linux_task_sync_config').fetchone()!=(url,actor):raise LocalStoreError('Die Kommentarwarteschlange gehört zu einem anderen Serverkonto.')
                if db.execute('SELECT 1 FROM linux_task_comment_personal_claim LIMIT 1').fetchone():raise LocalStoreError('Ein persönlicher Kommentarabgleich ist noch nicht abgeschlossen.')
                row=db.execute('''SELECT o.seq,o.operation_id,c.id,c.subject_id,c.author_id,c.body,c.created_at,o.wire_json,l.external_id,t.external_id,h.task_id
                    FROM linux_task_comment_sync_outbox o JOIN comments c ON c.id=o.comment_id
                    JOIN sync_object_links l ON l.object_type='comment' AND l.local_id=c.id
                    LEFT JOIN sync_object_links t ON t.object_type='task' AND t.local_id=c.subject_id
                    LEFT JOIN linux_task_sync_shadow h ON h.task_id=c.subject_id WHERE (t.external_id IS NOT NULL AND h.task_id IS NOT NULL) OR o.wire_json IS NOT NULL ORDER BY o.seq LIMIT 1''').fetchone()
                if row:
                    seq,op,local,task,author,body,created,frozen,external,task_external,confirmed=row
                    if not task_external or not confirmed:raise CommentTaskPendingError('Der Kommentar wartet auf eine synchronisierte Aufgabe.')
                    author_account=str(account(db,author))
                    author_link=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(author,)).fetchone()
                    if not author_link or not author_link[0]:raise LocalStoreError('Für den Kommentarautor fehlt die Mitarbeiterkennung.')
                    author_external=author_link[0]
                    # Preserve a numeric identity frozen by an earlier client version.
                    author_wire=author_external
                    if frozen is not None:
                        previous=json.loads(frozen).get('comment',{}).get('authorId')
                        if previous==author_account:author_wire=author_account
                    value={'clientId':client_id,'operationId':op,'comment':{'id':external,'subjectType':'task','subjectId':task_external,'authorId':author_wire,'body':body,'createdAt':_timestamp(created)}}
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Ein ausstehender Kommentar oder seine Autorenzuordnung wurde geändert.')
                    if frozen is None:db.execute('UPDATE linux_task_comment_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            if not row:break
            command('-action','sync-task-comment-event','-payload',encode(value))
            with _db(store,lock,is_current) as db:
                if db.execute('SELECT wire_json FROM linux_task_comment_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),):raise LocalStoreError('Die Kommentarwarteschlange wurde geändert.')
                # importedBy is added by the server; reconcile it with the snapshot below.
                db.execute('INSERT INTO linux_task_comment_sync_shadow VALUES(?,?) ON CONFLICT(comment_id) DO UPDATE SET comment_json=excluded.comment_json',(local,encode(value['comment'])))
                db.execute('DELETE FROM linux_task_comment_sync_outbox WHERE seq=?',(seq,));sent+=1
        snapshot=command('-action','sync-task-comments')
        if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Kommentarliste vom Server.')
        received=0
        with _db(store,lock,is_current) as db:
            pending=db.execute("SELECT 1 FROM linux_task_comment_sync_outbox LIMIT 1").fetchone() is not None
            if db.execute("""SELECT 1 FROM linux_task_comment_sync_outbox o LEFT JOIN comments c ON c.id=o.comment_id LEFT JOIN sync_object_links t ON t.object_type='task' AND t.local_id=c.subject_id LEFT JOIN linux_task_sync_shadow h ON h.task_id=c.subject_id WHERE c.id IS NULL OR o.wire_json IS NOT NULL OR (t.external_id IS NOT NULL AND h.task_id IS NOT NULL) LIMIT 1""").fetchone():raise LocalStoreError('Die Kommentarwarteschlange wurde während des Abgleichs geändert.')
            db.execute('UPDATE linux_task_comment_sync_guard SET suppressed=1 WHERE singleton=1');seen=set()
            for remote in snapshot:
                if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','subjectId','authorId','body','createdAt')) or remote.get('subjectType')!='task' or remote['id'] in seen or len(remote['body'].encode('utf-8'))>16000:raise LocalStoreError('Ungültiger oder doppelter Aufgabenkommentar vom Server.')
                seen.add(remote['id']);_timestamp(remote['createdAt'])
                task=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_task_sync_shadow s ON s.task_id=l.local_id WHERE l.object_type='task' AND l.external_id=?",(remote['subjectId'],)).fetchone()
                if not task:continue
                try:author_num=int(remote['authorId'])
                except ValueError:author_num=0
                if author_num>0 and str(author_num)==remote['authorId']:
                    author=local_id(db,author_num)
                else:
                    linked=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_employee_sync_shadow s ON s.employee_id=l.local_id WHERE l.object_type='employee' AND l.external_id=?",(remote['authorId'],)).fetchone()
                    author=linked[0] if linked else None
                if author is None:raise LocalStoreError('Für einen Kommentarautor fehlt eine eindeutige lokale Konto-Zuordnung: '+remote['id'])
                row=db.execute("SELECT c.id,c.subject_id,c.author_id,c.body,c.created_at,s.comment_json FROM sync_object_links l JOIN comments c ON c.id=l.local_id LEFT JOIN linux_task_comment_sync_shadow s ON s.comment_id=c.id WHERE l.object_type='comment' AND l.external_id=?",(remote['id'],)).fetchone()
                comparable={k:remote[k] for k in ('id','subjectType','subjectId','authorId','body','createdAt')}
                if row:
                    local,subject,local_author,body,created,shadow=row
                    if subject!=task[0] or local_author!=author or body!=remote['body'] or _timestamp(created)!=_timestamp(remote['createdAt']) or not shadow or {k:json.loads(shadow)[k] for k in comparable}!=comparable:raise LocalStoreError('Ein bereits abgeglichener Kommentar wurde verändert: '+remote['id'])
                else:
                    local=db.execute("INSERT INTO comments(subject_type,subject_id,author_id,body,created_at) VALUES('task',?,?,?,?)",(task[0],author,remote['body'],remote['createdAt'])).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('comment',?,?)",(remote['id'],local));received+=1
                db.execute('INSERT INTO linux_task_comment_sync_shadow VALUES(?,?) ON CONFLICT(comment_id) DO UPDATE SET comment_json=excluded.comment_json',(local,encode(comparable)))
            for (external,) in db.execute("SELECT l.external_id FROM linux_task_comment_sync_shadow s JOIN sync_object_links l ON l.object_type='comment' AND l.local_id=s.comment_id"):
                if external not in seen:raise LocalStoreError('Ein zuvor synchronisierter Kommentar fehlt auf dem Server.')
            db.execute('UPDATE linux_task_comment_sync_guard SET suppressed=0 WHERE singleton=1')
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync','task_comments.sync.pulled',?)",(str(received),))
        if pending:raise CommentTaskPendingError('Der Kommentar wartet auf eine synchronisierte Aufgabe.')
        return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Kommentarabgleich fehlgeschlagen: '+str(exc)) from exc


def sync_personal_task_comments(store,lock,is_current,client,url,token,ca,subject_id=None):
    url,actor,command=connect_sync_server(client,url,token,ca,'personalTaskCommentSync',None,'Leserechte')
    if subject_id is not None and command('-action','capabilities').get('personalCommentPull')!=1:raise LocalStoreError('Der Server unterstützt den persönlichen Kommentarempfang noch nicht.')
    identity=command('-action','identity')
    if not identity.get('canReadTasks'):raise LocalStoreError('Das Serverkonto benötigt tasks.read.')
    if subject_id is not None:
        try:
            with transaction(store,lock,is_current) as (db,person):
                local_identity=store.identity()
                if not local_identity.get('canReadTasks') or account(db,person)!=actor:raise LocalStoreError('Die lokale Person oder ihre Leseberechtigung stimmt nicht mit dem Serverkonto überein.')
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required={'linux_task_comment_sync_config','linux_task_comment_sync_outbox','linux_task_comment_sync_shadow','linux_task_comment_sync_guard','linux_task_sync_config','linux_task_sync_shadow'}
                if not required.issubset(tables):raise LocalStoreError('Bitte zuerst den Vorgangs- und Kommentarabgleich durch die Administration einrichten.')
                if not identity.get('canCollaborate') or not local_identity.get('canCollaborate'):read_only=True
                else:read_only=False
            if read_only:return {'sent':0,'received':pull_personal_comments(store,lock,is_current,command,url,actor,'task',subject_id,_timestamp,encode)}
        except sqlite3.Error as exc:raise LocalStoreError('Persönlicher Kommentarabgleich fehlgeschlagen: '+str(exc)) from exc
    elif not identity.get('canCollaborate'):raise LocalStoreError('Das Serverkonto benötigt collaboration.comment für den Versand.')
    try:
        sent=0
        while True:
            with transaction(store,lock,is_current) as (db,local_actor):
                local_identity=store.identity()
                if not local_identity.get('canReadTasks') or not local_identity.get('canCollaborate'):raise LocalStoreError('Die lokale Sitzung benötigt Aufgaben-Lese- und Kommentarrechte.')
                if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required={'linux_task_comment_sync_config','linux_task_comment_sync_outbox','linux_task_comment_sync_shadow','linux_task_sync_config','linux_task_sync_shadow'}
                if not required.issubset(tables):raise LocalStoreError('Bitte zuerst Aufgaben- und Aufgabenkommentarabgleich durch die Administration einrichten.')
                config=db.execute('SELECT client_id,server_url FROM linux_task_comment_sync_config').fetchone()
                if not config or config[1]!=url or db.execute('SELECT server_url FROM linux_task_sync_config').fetchone()!=(url,):raise LocalStoreError('Der Aufgabenkommentarabgleich gehört zu einem anderen Server.')
                client_id=config[0];db.execute('CREATE TABLE IF NOT EXISTS linux_task_comment_personal_claim(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,server_actor INTEGER NOT NULL)')
                row=db.execute('''SELECT o.seq,o.operation_id,c.id,c.subject_id,c.body,c.created_at,o.wire_json,l.external_id,t.external_id,h.task_id,k.actor_id,k.server_actor
                    FROM linux_task_comment_sync_outbox o JOIN comments c ON c.id=o.comment_id
                    JOIN sync_object_links l ON l.object_type='comment' AND l.local_id=c.id
                    LEFT JOIN sync_object_links t ON t.object_type='task' AND t.local_id=c.subject_id
                    LEFT JOIN linux_task_sync_shadow h ON h.task_id=c.subject_id
                    LEFT JOIN linux_task_comment_personal_claim k ON k.seq=o.seq
                    WHERE c.author_id=? AND (k.seq IS NOT NULL OR o.wire_json IS NULL) AND ((t.external_id IS NOT NULL AND h.task_id IS NOT NULL) OR k.seq IS NOT NULL) ORDER BY o.seq LIMIT 1''',(local_actor,)).fetchone()
                if not row:
                    if db.execute("SELECT 1 FROM linux_task_comment_sync_outbox o JOIN comments c ON c.id=o.comment_id WHERE c.author_id=? LIMIT 1",(local_actor,)).fetchone():raise CommentTaskPendingError('Eigene Kommentare warten auf den Aufgabenabgleich oder einen bereits begonnenen administrativen Versand.')
                    break
                seq,op,local,task,body,created,frozen,external,task_external,confirmed,claim_actor,claim_server=row
                if claim_actor is not None and (claim_actor!=local_actor or claim_server!=actor):raise LocalStoreError('Der Kommentar wurde für ein anderes persönliches Serverkonto vorgemerkt.')
                if not task_external or not confirmed:raise CommentTaskPendingError('Der Kommentar wartet auf eine synchronisierte Aufgabe.')
                value={'clientId':client_id,'operationId':op,'comment':{'id':external,'subjectType':'task','subjectId':task_external,'authorId':employee_external_id(db,local_actor),'body':body,'createdAt':_timestamp(created)}}
                if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Ein persönlicher Kommentar oder seine Zuordnung wurde geändert.')
                if frozen is None:
                    db.execute('INSERT INTO linux_task_comment_personal_claim VALUES(?,?,?)',(seq,local_actor,actor))
                    db.execute('UPDATE linux_task_comment_sync_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
            command('-action','sync-personal-task-comment-event','-payload',encode(value))
            with transaction(store,lock,is_current) as (db,current_actor):
                if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
                if db.execute('SELECT wire_json FROM linux_task_comment_sync_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),) or db.execute('SELECT actor_id,server_actor FROM linux_task_comment_personal_claim WHERE seq=?',(seq,)).fetchone()!=(local_actor,actor):raise LocalStoreError('Die persönliche Kommentarwarteschlange wurde geändert.')
                db.execute('INSERT INTO linux_task_comment_sync_shadow VALUES(?,?) ON CONFLICT(comment_id) DO UPDATE SET comment_json=excluded.comment_json',(local,encode(value['comment'])))
                db.execute('DELETE FROM linux_task_comment_sync_outbox WHERE seq=?',(seq,));db.execute('DELETE FROM linux_task_comment_personal_claim WHERE seq=?',(seq,))
                db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'task_comment.sync.sent_personal',?)",(str(local_actor),str(local)));sent+=1
        if subject_id is not None:return {'sent':sent,'received':pull_personal_comments(store,lock,is_current,command,url,actor,'task',subject_id,_timestamp,encode)}
        return {'sent':sent}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Persönlicher Kommentarabgleich fehlgeschlagen: '+str(exc)) from exc

def task_comment_sync_config(store,lock,is_current):
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_task_comment_sync_config'").fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM linux_task_comment_sync_config WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Kommentarkonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

"""Import the visible comment history of one confirmed task or ticket."""
import json,sqlite3
from leave_sync import account,local_id,employee_external_id,transaction
from local_store import LocalStoreError


def pull_personal_comments(store,lock,is_current,command,url,actor,kind,subject_id,timestamp,encode):
    if kind not in ('task','ticket') or type(subject_id) is not int or subject_id<=0:raise LocalStoreError('Ungültiger Vorgang.')
    config='linux_'+kind+'_sync_config';shadow='linux_'+kind+'_sync_shadow';comment_shadow='linux_'+kind+'_comment_sync_shadow';guard='linux_'+kind+'_comment_sync_guard';outbox='linux_'+kind+'_comment_sync_outbox'
    def baseline(db,local_actor):
        if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
        if db.execute('SELECT server_url FROM '+config).fetchone()!=(url,):raise LocalStoreError('Der Vorgang gehört zu einem anderen Server.')
        row=db.execute("SELECT l.external_id FROM sync_object_links l JOIN "+shadow+" s ON s."+kind+"_id=l.local_id WHERE l.object_type=? AND l.local_id=?",(kind,subject_id)).fetchone()
        if not row or not row[0]:raise LocalStoreError('Der Vorgang muss zuerst mit dem Server abgeglichen werden.')
        return row[0]
    with transaction(store,lock,is_current) as (db,local_actor):
        # The native access check enforces the current local session's read rights.
        store.comments(kind,subject_id);external=baseline(db,local_actor);viewer=employee_external_id(db,local_actor)
    snapshot=command('-action','personal-comments','-subject-type',kind,'-subject-id',external,'-author-id',viewer)
    if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültiger Kommentarverlauf vom Server.')
    received=0;seen=set()
    try:
        with transaction(store,lock,is_current) as (db,current):
            if current!=local_actor or baseline(db,current)!=external or employee_external_id(db,current)!=viewer:raise LocalStoreError('Sitzung oder Vorgangszuordnung wurde geändert.')
            db.execute('UPDATE '+guard+' SET suppressed=1 WHERE singleton=1')
            for raw in snapshot:
                if not isinstance(raw,dict) or any(not isinstance(raw.get(k),str) or not raw[k].strip() or '\0' in raw[k] for k in ('id','subjectId','authorId','body','createdAt')) or raw['subjectType']!=kind or raw['subjectId']!=external or raw['id'] in seen or len(raw['body'].encode('utf-8'))>16000:raise LocalStoreError('Ungültiger oder doppelter Kommentar vom Server.')
                seen.add(raw['id']);timestamp(raw['createdAt'])
                try:num=int(raw['authorId'])
                except ValueError:num=0
                if num>0 and str(num)==raw['authorId']:author=local_id(db,num)
                else:
                    match=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_employee_sync_shadow s ON s.employee_id=l.local_id WHERE l.object_type='employee' AND l.external_id=?",(raw['authorId'],)).fetchone();author=match[0] if match else None
                if author is None:raise LocalStoreError('Für einen Kommentarautor fehlt eine eindeutige Mitarbeiterzuordnung.')
                if raw.get('authorExternalId')!=employee_external_id(db,author):raise LocalStoreError('Die Serverzuordnung eines Kommentarautors hat sich geändert. Bitte zuerst Mitarbeiter abgleichen.')
                comparable={k:raw[k] for k in ('id','subjectType','subjectId','authorId','body','createdAt')}
                row=db.execute("SELECT c.id,c.subject_id,c.author_id,c.body,c.created_at,s.comment_json FROM sync_object_links l JOIN comments c ON c.id=l.local_id LEFT JOIN "+comment_shadow+" s ON s.comment_id=c.id WHERE l.object_type='comment' AND l.external_id=?",(raw['id'],)).fetchone()
                if row:
                    local,subject,local_author,body,created,previous=row
                    if db.execute('SELECT 1 FROM '+outbox+' WHERE comment_id=?',(local,)).fetchone():raise LocalStoreError('Ein Serverkommentar besitzt noch eine ausstehende lokale Übertragung.')
                    if subject!=subject_id or local_author!=author or body!=raw['body'] or timestamp(created)!=timestamp(raw['createdAt']) or not previous or {k:json.loads(previous)[k] for k in comparable}!=comparable:raise LocalStoreError('Ein bereits abgeglichener Kommentar wurde verändert.')
                else:
                    local=db.execute('INSERT INTO comments(subject_type,subject_id,author_id,body,created_at) VALUES(?,?,?,?,?)',(kind,subject_id,author,raw['body'],raw['createdAt'])).lastrowid
                    db.execute("INSERT INTO sync_object_links VALUES('comment',?,?)",(raw['id'],local));received+=1
                db.execute('INSERT INTO '+comment_shadow+' VALUES(?,?) ON CONFLICT(comment_id) DO UPDATE SET comment_json=excluded.comment_json',(local,encode(comparable)))
            db.execute('UPDATE '+guard+' SET suppressed=0 WHERE singleton=1')
            db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)",(str(local_actor),kind+'_comments.sync.received_personal',str(received)))
        return received
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Persönlicher Kommentarempfang fehlgeschlagen: '+str(exc)) from exc

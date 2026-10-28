"""Durable task/ticket attachment synchronization with verified file transfers."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from pathlib import Path
from employee_sync import _db
from leave_sync import account,local_id,employee_external_id,transaction
from local_store import LocalStoreError
from room_sync import connect_sync_server


class AttachmentParentPendingError(LocalStoreError):
    pass


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def setup(db,kind,url,actor):
    prefix='linux_'+kind+'_attachment_sync'
    config='linux_'+kind+'_sync_config'
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(config,)).fetchone() or db.execute('SELECT server_url,server_actor FROM '+config).fetchone()!=(url,actor):raise LocalStoreError('Bitte zuerst '+('Aufgaben' if kind=='task' else 'Tickets')+' für denselben Server und dasselbe Konto abgleichen.')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,attachment_id INTEGER NOT NULL,wire_json TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_personal_claim(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,server_actor INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_archive_actor(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_shadow(attachment_id INTEGER PRIMARY KEY,attachment_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO '+prefix+'_guard VALUES(1,0)')
    row=db.execute('SELECT client_id,server_url,server_actor FROM '+prefix+'_config').fetchone()
    if row:
        if row[1:]!=(url,actor):raise LocalStoreError('Die Anhangswarteschlange gehört zu einem anderen Serverkonto.')
        return row[0]
    client='linux-'+uuid.uuid4().hex;db.execute('INSERT INTO '+prefix+'_config VALUES(1,?,?,?)',(client,url,actor))
    db.execute('''CREATE TRIGGER '''+prefix+'''_insert AFTER INSERT ON attachments WHEN NEW.subject_type='''+repr(kind)+''' AND (SELECT suppressed FROM '''+prefix+'''_guard)=0 BEGIN
      INSERT OR IGNORE INTO sync_object_links SELECT 'attachment',client_id||':attachment:'||NEW.id,NEW.id FROM '''+prefix+'''_config;
      INSERT INTO '''+prefix+'''_outbox(operation_id,action,attachment_id) VALUES(lower(hex(randomblob(16))),'create',NEW.id);
      INSERT INTO '''+prefix+'''_outbox(operation_id,action,attachment_id) SELECT lower(hex(randomblob(16))),'archive',NEW.id WHERE NEW.active=0;
    END''')
    db.execute('''CREATE TRIGGER '''+prefix+'''_archive AFTER UPDATE OF active ON attachments WHEN NEW.subject_type='''+repr(kind)+''' AND OLD.active=1 AND NEW.active=0 AND (SELECT suppressed FROM '''+prefix+'''_guard)=0 BEGIN
      INSERT INTO '''+prefix+'''_outbox(operation_id,action,attachment_id) VALUES(lower(hex(randomblob(16))),'archive',NEW.id);
    END''')
    db.execute("INSERT OR IGNORE INTO sync_object_links SELECT 'attachment',?||':attachment:'||id,id FROM attachments WHERE subject_type=?",(client,kind))
    for local,active in db.execute('SELECT id,active FROM attachments WHERE subject_type=? ORDER BY id',(kind,)).fetchall():
        db.execute('INSERT INTO '+prefix+"_outbox(operation_id,action,attachment_id) VALUES(?,'create',?)",(uuid.uuid4().hex,local))
        if not active:db.execute('INSERT INTO '+prefix+"_outbox(operation_id,action,attachment_id) VALUES(?,'archive',?)",(uuid.uuid4().hex,local))
    return client


def _staged_copy(store,kind,subject,path,size,digest,destination):
    path=store.attachment_path(kind,subject,path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as inp,open(destination,'wb') as out:
        info=os.fstat(inp.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size!=size or size>100*1024*1024:raise LocalStoreError('Anhang hat eine ungültige Größe.')
        h=hashlib.sha256();total=0
        while True:
            chunk=inp.read(1024*1024)
            if not chunk:break
            total+=len(chunk)
            if total>100*1024*1024:raise LocalStoreError('Anhang überschreitet 100 MB.')
            h.update(chunk);out.write(chunk)
        if total!=size or h.hexdigest()!=digest:raise LocalStoreError('Integritätsprüfung des lokalen Anhangs fehlgeschlagen.')


def _validate(remote,kind):
    if not isinstance(remote,dict) or any(not isinstance(remote.get(k),str) or not remote[k].strip() or '\0' in remote[k] for k in ('id','subjectId','originalName','sha256','uploadedBy')) or remote.get('subjectType')!=kind or '/' in remote['originalName'] or '\\' in remote['originalName'] or len(remote['originalName'].encode('utf-8'))>255 or not re.fullmatch('[0-9a-f]{64}',remote['sha256']) or type(remote.get('sizeBytes')) is not int or not 0<=remote['sizeBytes']<=100*1024*1024 or type(remote.get('active')) is not bool or type(remote.get('importedBy',0)) is not int or remote.get('importedBy',0)<0:raise LocalStoreError('Ungültige Anhangsdaten vom Server.')
    return remote


def sync_attachments(store,lock,is_current,client,url,token,ca,kind):
    if kind not in ('task','ticket'):raise LocalStoreError('Ungültiger Anhangsbereich.')
    url,actor,command=connect_sync_server(client,url,token,ca,'attachmentSync','canManageTasks' if kind=='task' else 'canManageTickets',('tasks' if kind=='task' else 'tickets')+'.manage')
    if not command('-action','identity').get('canManageEmployees'):raise LocalStoreError('Das Serverkonto benötigt employees.manage.')
    prefix='linux_'+kind+'_attachment_sync';parent='linux_'+kind+'_sync_config';shadow='linux_'+kind+'_sync_shadow'
    def bound(db,client_id):
        if db.execute('SELECT client_id,server_url,server_actor FROM '+prefix+'_config').fetchone()!=(client_id,url,actor) or db.execute('SELECT server_url,server_actor FROM '+parent).fetchone()!=(url,actor):raise LocalStoreError('Die Anhangswarteschlange gehört zu einem anderen Serverkonto.')
    created_paths=[]
    try:
        with _db(store,lock,is_current) as db:client_id=setup(db,kind,url,actor)
        with tempfile.TemporaryDirectory(prefix='aura-attachment-sync-') as temp:
            sent=0
            while True:
                with _db(store,lock,is_current) as db:
                    bound(db,client_id)
                    if db.execute('SELECT 1 FROM '+prefix+'_personal_claim LIMIT 1').fetchone():raise LocalStoreError('Ein persönlicher Anhangsabgleich ist noch nicht abgeschlossen.')
                    row=db.execute('''SELECT o.seq,o.operation_id,o.action,a.id,a.subject_id,a.original_name,a.stored_path,a.size_bytes,a.sha256,a.uploaded_by,o.wire_json,l.external_id,p.external_id,h.attachment_json
                      FROM '''+prefix+'''_outbox o JOIN attachments a ON a.id=o.attachment_id
                      JOIN sync_object_links l ON l.object_type='attachment' AND l.local_id=a.id
                      LEFT JOIN sync_object_links p ON p.object_type=? AND p.local_id=a.subject_id
                      LEFT JOIN '''+shadow+''' ps ON ps.'''+kind+'''_id=a.subject_id
                      LEFT JOIN '''+prefix+'''_shadow h ON h.attachment_id=a.id
                      WHERE ps.'''+kind+'''_id IS NOT NULL ORDER BY o.seq LIMIT 1''',(kind,)).fetchone()
                    if row:
                        seq,op,action,local,subject,name,path,size,digest,uploader,frozen,external,parent_external,prior=row
                        if not parent_external:raise LocalStoreError('Für den Vorgang fehlt die eindeutige Serverkennung.')
                        account_id=account(db,uploader)
                        author=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(uploader,)).fetchone()
                        if not author or not author[0]:raise LocalStoreError('Für den Anhangsautor fehlt die Mitarbeiterkennung.')
                        record={'id':external,'subjectType':kind,'subjectId':parent_external,'originalName':name,'sizeBytes':size,'sha256':digest,'uploadedBy':author[0],'active':True}
                        if action=='create':expected=None
                        else:
                            if not prior:raise LocalStoreError('Der Anhang wartet auf die bestätigte Anlage.')
                            expected=json.loads(prior)
                            if any(record[k]!=expected[k] for k in ('id','subjectType','subjectId','originalName','sizeBytes','sha256')) or expected['uploadedBy'] not in (author[0],str(account_id)):raise LocalStoreError('Die lokalen Anhangsmetadaten wurden verändert.')
                            if action not in ('archive','restore'):raise LocalStoreError('Ungültige Anhangsoperation.')
                            record={**expected,'active':action=='restore'}
                        value={'clientId':client_id,'operationId':op,'action':action,'attachment':record,'expected':expected}
                        if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Ein ausstehender Anhang oder seine Zuordnung wurde geändert.')
                        if frozen is None:db.execute('UPDATE '+prefix+'_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
                if not row:break
                if action=='restore' and command('-action','capabilities').get('attachmentRestoreSync')!=1:raise LocalStoreError('Der Server unterstützt die Wiederherstellung von Anhängen noch nicht.')
                if action in ('create','restore'):
                    staged=os.path.join(temp,'upload');_staged_copy(store,kind,subject,path,size,digest,staged)
                    uploaded=command('-action','upload-attachment','-file',staged)
                    if uploaded.get('sha256')!=digest:raise LocalStoreError('Die Prüfsumme des übertragenen Anhangs stimmt nicht.')
                command('-action','sync-attachment-event','-subject-type',kind,'-payload',encode(value))
                with _db(store,lock,is_current) as db:
                    bound(db,client_id)
                    if db.execute('SELECT wire_json FROM '+prefix+'_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),):raise LocalStoreError('Die Anhangswarteschlange wurde geändert.')
                    saved={**record,'importedBy':actor} if action=='create' else record
                    db.execute('INSERT INTO '+prefix+'_shadow VALUES(?,?) ON CONFLICT(attachment_id) DO UPDATE SET attachment_json=excluded.attachment_json',(local,encode(saved)))
                    db.execute('DELETE FROM '+prefix+'_outbox WHERE seq=?',(seq,));sent+=1
                    db.execute('DELETE FROM '+prefix+'_archive_actor WHERE seq=?',(seq,))
            snapshot=command('-action','sync-attachments','-subject-type',kind)
            if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Anhangsliste vom Server.')
            seen=set();downloads={}
            for remote in snapshot:
                _validate(remote,kind)
                if remote['id'] in seen:raise LocalStoreError('Doppelte Anhangskennung vom Server.')
                seen.add(remote['id'])
                with _db(store,lock,is_current) as db:
                    bound(db,client_id)
                    known=db.execute("SELECT 1 FROM sync_object_links WHERE object_type='attachment' AND external_id=?",(remote['id'],)).fetchone()
                    parent_known=db.execute("SELECT l.local_id FROM sync_object_links l JOIN "+shadow+" s ON s."+kind+"_id=l.local_id WHERE l.object_type=? AND l.external_id=?",(kind,remote['subjectId'])).fetchone()
                if not known and parent_known:
                    path=os.path.join(temp,'download-'+uuid.uuid4().hex)
                    command('-action','download-attachment','-id',remote['sha256'],'-file',path)
                    if not os.path.isfile(path) or os.path.getsize(path)!=remote['sizeBytes']:raise LocalStoreError('Die Größe des übertragenen Anhangs stimmt nicht.')
                    downloads[remote['id']]=path
            received=0
            try:
                with _db(store,lock,is_current) as db:
                    bound(db,client_id)
                    pending=db.execute('SELECT 1 FROM '+prefix+'_outbox LIMIT 1').fetchone() is not None
                    # Only never-sent operations whose parent is still absent may
                    # coexist with an incoming snapshot. Any other change is a race.
                    if db.execute('SELECT 1 FROM '+prefix+'_outbox o LEFT JOIN attachments a ON a.id=o.attachment_id LEFT JOIN '+shadow+' ps ON ps.'+kind+'_id=a.subject_id WHERE a.id IS NULL OR o.wire_json IS NOT NULL OR ps.'+kind+'_id IS NOT NULL LIMIT 1').fetchone():raise LocalStoreError('Die Anhangswarteschlange wurde während des Abgleichs geändert.')
                    db.execute('UPDATE '+prefix+'_guard SET suppressed=1 WHERE singleton=1')
                    for remote in snapshot:
                        parent_row=db.execute("SELECT l.local_id FROM sync_object_links l JOIN "+shadow+" s ON s."+kind+"_id=l.local_id WHERE l.object_type=? AND l.external_id=?",(kind,remote['subjectId'])).fetchone()
                        if not parent_row:continue
                        author_id=remote['uploadedBy']
                        try:number=int(author_id)
                        except ValueError:number=0
                        if number>0 and str(number)==author_id:uploader=local_id(db,number)
                        else:
                            a=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_employee_sync_shadow s ON s.employee_id=l.local_id WHERE l.object_type='employee' AND l.external_id=?",(author_id,)).fetchone();uploader=a[0] if a else None
                        if uploader is None:raise LocalStoreError('Für den Anhangsautor fehlt eine eindeutige Mitarbeiterzuordnung: '+remote['id'])
                        row=db.execute("SELECT a.id,a.subject_id,a.original_name,a.stored_path,a.size_bytes,a.sha256,a.uploaded_by,a.active,h.attachment_json FROM sync_object_links l JOIN attachments a ON a.id=l.local_id LEFT JOIN "+prefix+"_shadow h ON h.attachment_id=a.id WHERE l.object_type='attachment' AND l.external_id=?",(remote['id'],)).fetchone()
                        if row:
                            local,subject,name,path,size,digest,author,active,old=row
                            if not old:raise LocalStoreError('Anhangskonflikt: kein gemeinsamer Ausgangsstand.')
                            previous=json.loads(old)
                            if subject!=parent_row[0] or name!=previous['originalName'] or size!=previous['sizeBytes'] or digest!=previous['sha256'] or author!=uploader or bool(active)!=previous['active']:raise LocalStoreError('Anhangskonflikt: Der lokale Stand wurde geändert.')
                            if any(remote.get(k,0)!=previous.get(k,0) for k in ('id','subjectType','subjectId','originalName','sizeBytes','sha256','uploadedBy','importedBy')):raise LocalStoreError('Anhangskonflikt: Die Servermetadaten wurden geändert.')
                            if bool(active)!=remote['active']:db.execute('UPDATE attachments SET active=? WHERE id=?',(int(remote['active']),local));received+=1
                        else:
                            staged=downloads.get(remote['id'])
                            if not staged:raise LocalStoreError('Ein neuer Anhang muss erneut heruntergeladen werden.')
                            folder=store.path.parent.resolve(strict=True)
                            for part in ('attachments',kind,str(parent_row[0])):
                                folder=folder/part;folder.mkdir(mode=0o700,exist_ok=True)
                                if folder.is_symlink() or not folder.is_dir():raise LocalStoreError('Ungültiger Anhangsordner.')
                            dest=folder/(uuid.uuid4().hex+'.bin');fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created_paths.append(dest)
                            with open(staged,'rb') as src,os.fdopen(fd,'wb') as out:shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
                            local=db.execute('INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,active) VALUES(?,?,?,?,?,?,?,?)',(kind,parent_row[0],remote['originalName'],str(dest),remote['sizeBytes'],remote['sha256'],uploader,int(remote['active']))).lastrowid
                            db.execute("INSERT INTO sync_object_links VALUES('attachment',?,?)",(remote['id'],local));received+=1
                        db.execute('INSERT INTO '+prefix+'_shadow VALUES(?,?) ON CONFLICT(attachment_id) DO UPDATE SET attachment_json=excluded.attachment_json',(local,encode(remote)))
                    for (external,) in db.execute("SELECT l.external_id FROM "+prefix+"_shadow s JOIN sync_object_links l ON l.object_type='attachment' AND l.local_id=s.attachment_id"):
                        if external not in seen:raise LocalStoreError('Ein zuvor synchronisierter Anhang fehlt auf dem Server.')
                    db.execute('UPDATE '+prefix+'_guard SET suppressed=0 WHERE singleton=1');db.execute("INSERT INTO audit_log(actor,action,subject) VALUES('linux-sync',?,?)",(kind+'_attachments.sync.pulled',str(received)))
            except Exception:
                for path in created_paths:path.unlink(missing_ok=True)
                raise
            if pending:raise AttachmentParentPendingError('Ein Anhang wartet auf die Synchronisation des Vorgangs.')
            return {'sent':sent,'received':received}
    except (sqlite3.Error,ValueError,KeyError,OSError) as exc:raise LocalStoreError('Anhangsabgleich fehlgeschlagen: '+str(exc)) from exc


def _conflict_preview(db,kind,url,actor):
    if kind not in ('task','ticket'):raise LocalStoreError('Ungültiger Anhangsbereich.')
    prefix='linux_'+kind+'_attachment_sync';parent='linux_'+kind+'_sync_config';parent_shadow='linux_'+kind+'_sync_shadow'
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(prefix+'_config',)).fetchone() or db.execute('SELECT server_url,server_actor FROM '+prefix+'_config').fetchone()!=(url,actor) or db.execute('SELECT server_url,server_actor FROM '+parent).fetchone()!=(url,actor):raise LocalStoreError('Die Anhangswarteschlange gehört zu einem anderen Serverkonto.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(prefix+'_personal_claim',)).fetchone() and db.execute('SELECT 1 FROM '+prefix+'_personal_claim LIMIT 1').fetchone():raise LocalStoreError('Ein persönlicher Anhangsabgleich ist noch nicht abgeschlossen.')
    row=db.execute('''SELECT o.seq,o.operation_id,a.id,a.subject_id,a.original_name,a.stored_path,a.size_bytes,a.sha256,a.uploaded_by,a.active,l.external_id,p.external_id,h.attachment_json
      FROM '''+prefix+'''_outbox o JOIN attachments a ON a.id=o.attachment_id
      JOIN sync_object_links l ON l.object_type='attachment' AND l.local_id=a.id
      JOIN sync_object_links p ON p.object_type=? AND p.local_id=a.subject_id
      JOIN '''+parent_shadow+''' ps ON ps.'''+kind+'''_id=a.subject_id
      LEFT JOIN '''+prefix+'''_shadow h ON h.attachment_id=a.id ORDER BY o.seq LIMIT 1''',(kind,)).fetchone()
    if not row:raise LocalStoreError('Es gibt keinen ausstehenden Anhang mit einem bestätigten Vorgang.')
    seq,operation,local,subject,name,path,size,digest,uploader,active,external,parent_external,shadow=row
    account_id=account(db,uploader);author=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(uploader,)).fetchone()
    if not author:raise LocalStoreError('Für den Anhangsautor fehlt die Mitarbeiterkennung.')
    pending=[list(v) for v in db.execute('SELECT seq,operation_id,action,wire_json FROM '+prefix+'_outbox WHERE attachment_id=? ORDER BY seq',(local,))]
    return {'seq':seq,'operationId':operation,'localId':local,'subject':subject,'path':path,'uploader':uploader,'accountId':account_id,'authorId':author[0],'local':{'id':external,'subjectType':kind,'subjectId':parent_external,'originalName':name,'sizeBytes':size,'sha256':digest,'uploadedBy':author[0],'active':bool(active)},'shadow':json.loads(shadow) if shadow else None,'pending':pending}


def inspect_attachment_conflict(store,lock,is_current,client,url,token,ca,kind):
    if kind not in ('task','ticket'):raise LocalStoreError('Ungültiger Anhangsbereich.')
    url,actor,command=connect_sync_server(client,url,token,ca,'attachmentSync','canManageTasks' if kind=='task' else 'canManageTickets',('tasks' if kind=='task' else 'tickets')+'.manage')
    if not command('-action','identity').get('canManageEmployees'):raise LocalStoreError('Das Serverkonto benötigt employees.manage.')
    try:
        with _db(store,lock,is_current) as db:preview=_conflict_preview(db,kind,url,actor)
        values=command('-action','sync-attachments','-subject-type',kind)
        if not isinstance(values,list) or len(values)>100000:raise LocalStoreError('Ungültige Anhangsliste vom Server.')
        matches=[_validate(v,kind) for v in values if isinstance(v,dict) and v.get('id')==preview['local']['id']]
        if len(matches)!=1:raise LocalStoreError('Für diesen Anhang gibt es keinen eindeutig zugeordneten Servereintrag.')
        return {'preview':preview,'remote':matches[0],'url':url,'actor':actor,'kind':kind}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Anhangskonflikt konnte nicht geprüft werden: '+str(exc)) from exc


def resolve_attachment_conflict(store,lock,is_current,client,url,token,ca,reviewed,accept_server):
    kind=reviewed.get('kind');fresh=inspect_attachment_conflict(store,lock,is_current,client,url,token,ca,kind)
    if fresh!=reviewed:raise LocalStoreError('Der Konflikt hat sich während der Prüfung geändert. Bitte erneut prüfen.')
    prefix='linux_'+kind+'_attachment_sync'
    try:
        preview=reviewed['preview'];local=preview['local'];remote=reviewed['remote'];prior=preview['shadow']
        immutable=('id','subjectType','subjectId','originalName','sizeBytes','sha256')
        if any(local[k]!=remote[k] for k in immutable) or remote['uploadedBy'] not in (preview['authorId'],str(preview['accountId'])):raise LocalStoreError('Die Anhangsmetadaten oder Autoren sind unterschiedlich; eine automatische Zuordnung ist nicht möglich.')
        if prior and any(prior.get(k,0)!=remote.get(k,0) for k in immutable+('uploadedBy','importedBy')):raise LocalStoreError('Die unveränderlichen Servermetadaten haben sich geändert.')
        if not accept_server and local['active'] and not remote['active']:
            url,actor,command=connect_sync_server(client,url,token,ca,'attachmentSync','canManageTasks' if kind=='task' else 'canManageTickets',('tasks' if kind=='task' else 'tickets')+'.manage')
            if command('-action','capabilities').get('attachmentRestoreSync')!=1:raise LocalStoreError('Der Server unterstützt die Wiederherstellung von Anhängen noch nicht.')
        if (accept_server and remote['active']) or (not accept_server and local['active'] and not remote['active']):
            with tempfile.TemporaryDirectory(prefix='aura-attachment-check-') as temp:
                _staged_copy(store,kind,preview['subject'],preview['path'],local['sizeBytes'],local['sha256'],os.path.join(temp,'verify'))
        with _db(store,lock,is_current) as db:
            if _conflict_preview(db,kind,reviewed['url'],reviewed['actor'])!=preview:raise LocalStoreError('Der lokale Anhang oder die Warteschlange hat sich geändert. Bitte erneut prüfen.')
            db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_conflicts(id INTEGER PRIMARY KEY,attachment_id INTEGER NOT NULL,choice TEXT NOT NULL,local_json TEXT NOT NULL,remote_json TEXT NOT NULL,previous_operations TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO '+prefix+'_conflicts(attachment_id,choice,local_json,remote_json,previous_operations) VALUES(?,?,?,?,?)',(preview['localId'],'server' if accept_server else 'local',encode(local),encode(remote),encode(preview['pending'])))
            db.execute('INSERT INTO '+prefix+'_shadow VALUES(?,?) ON CONFLICT(attachment_id) DO UPDATE SET attachment_json=excluded.attachment_json',(preview['localId'],encode(remote)))
            if accept_server:
                db.execute('UPDATE '+prefix+'_guard SET suppressed=1 WHERE singleton=1')
                db.execute('UPDATE attachments SET active=? WHERE id=?',(int(remote['active']),preview['localId']))
                db.execute('UPDATE '+prefix+'_guard SET suppressed=0 WHERE singleton=1')
                db.execute('DELETE FROM '+prefix+'_outbox WHERE attachment_id=?',(preview['localId'],))
            elif local['active']==remote['active']:db.execute('DELETE FROM '+prefix+'_outbox WHERE attachment_id=?',(preview['localId'],))
            else:
                db.execute('DELETE FROM '+prefix+'_outbox WHERE attachment_id=? AND seq<>?',(preview['localId'],preview['seq']))
                db.execute('UPDATE '+prefix+"_outbox SET operation_id=?,action=?,wire_json=NULL WHERE seq=? AND operation_id=?",(uuid.uuid4().hex,'restore' if local['active'] else 'archive',preview['seq'],preview['operationId']))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(prefix+'_archive_actor',)).fetchone():db.execute('DELETE FROM '+prefix+'_archive_actor WHERE seq=?',(preview['seq'],))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(prefix+'_archive_actor',)).fetchone():db.execute('DELETE FROM '+prefix+'_archive_actor WHERE seq NOT IN (SELECT seq FROM '+prefix+'_outbox)')
            db.execute('INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)',('linux-sync',kind+'_attachments.sync.accepted_server' if accept_server else kind+'_attachments.sync.rebased',str(preview['localId'])))
    except (sqlite3.Error,ValueError,KeyError,OSError) as exc:raise LocalStoreError('Anhangskonflikt konnte nicht übernommen werden: '+str(exc)) from exc


def sync_personal_attachments(store,lock,is_current,client,url,token,ca,kind,subject_id=None):
    if kind not in ('task','ticket'):raise LocalStoreError('Ungültiger Anhangsbereich.')
    url,actor,command=connect_sync_server(client,url,token,ca,'personalAttachmentSync',None,'Leserechte')
    read='canReadTasks' if kind=='task' else 'canReadTickets'
    command_identity=command('-action','identity')
    if not command_identity.get(read):raise LocalStoreError('Das Serverkonto benötigt Leserechte für diesen Bereich.')
    capabilities=command('-action','capabilities')
    if subject_id is not None and capabilities.get('personalAttachmentPull')!=1:raise LocalStoreError('Der Server unterstützt den persönlichen Anhangsempfang noch nicht.')
    prefix='linux_'+kind+'_attachment_sync';parent='linux_'+kind+'_sync_config';shadow='linux_'+kind+'_sync_shadow'
    if subject_id is not None:
        try:
            with transaction(store,lock,is_current) as (db,person):
                local_identity=store.identity()
                if not local_identity.get(read) or account(db,person)!=actor:raise LocalStoreError('Die lokale Person oder ihre Leseberechtigung stimmt nicht mit dem Serverkonto überein.')
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {prefix+'_config',prefix+'_outbox',prefix+'_shadow',prefix+'_guard',parent,shadow}.issubset(tables):raise LocalStoreError('Bitte zuerst den Vorgangs- und Anhangsabgleich durch die Administration einrichten.')
                read_only=not command_identity.get('canCollaborate') or not local_identity.get('canCollaborate')
            if read_only:return {'sent':0,'received':pull_personal_attachments(store,lock,is_current,command,url,actor,kind,subject_id,capabilities.get('personalAttachmentStates')==1)}
        except sqlite3.Error as exc:raise LocalStoreError('Persönlicher Anhangsabgleich fehlgeschlagen: '+str(exc)) from exc
    elif not command_identity.get('canCollaborate'):raise LocalStoreError('Das Serverkonto benötigt collaboration.comment für den Versand.')
    try:
        sent=0
        with tempfile.TemporaryDirectory(prefix='aura-personal-attachment-') as temp:
            while True:
                with transaction(store,lock,is_current) as (db,local_actor):
                    local_identity=store.identity()
                    if not local_identity.get(read) or not local_identity.get('canCollaborate'):raise LocalStoreError('Die lokale Sitzung benötigt Lese- und Zusammenarbeitsrechte.')
                    if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
                    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {prefix+'_config',prefix+'_outbox',prefix+'_shadow',parent,shadow}.issubset(tables):raise LocalStoreError('Bitte zuerst Vorgangs- und Anhangsabgleich durch die Administration einrichten.')
                    config=db.execute('SELECT client_id,server_url FROM '+prefix+'_config').fetchone()
                    if not config or config[1]!=url or db.execute('SELECT server_url FROM '+parent).fetchone()!=(url,):raise LocalStoreError('Der Anhangsabgleich gehört zu einem anderen Server.')
                    client_id=config[0];db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_personal_claim(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,server_actor INTEGER NOT NULL)')
                    db.execute('CREATE TABLE IF NOT EXISTS '+prefix+'_archive_actor(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL)')
                    row=db.execute('''SELECT o.seq,o.operation_id,a.id,a.subject_id,a.original_name,a.stored_path,a.size_bytes,a.sha256,o.wire_json,l.external_id,p.external_id,ps.'''+kind+'''_id,k.actor_id,k.server_actor,o.action,h.attachment_json,a.uploaded_by
                      FROM '''+prefix+'''_outbox o JOIN attachments a ON a.id=o.attachment_id
                      JOIN sync_object_links l ON l.object_type='attachment' AND l.local_id=a.id
                      LEFT JOIN sync_object_links p ON p.object_type=? AND p.local_id=a.subject_id
                      LEFT JOIN '''+shadow+''' ps ON ps.'''+kind+'''_id=a.subject_id
                      LEFT JOIN '''+prefix+'''_personal_claim k ON k.seq=o.seq
                      LEFT JOIN '''+prefix+'''_archive_actor aa ON aa.seq=o.seq
                      LEFT JOIN '''+prefix+'''_shadow h ON h.attachment_id=a.id
                      WHERE ((o.action='create' AND a.uploaded_by=?) OR (o.action='archive' AND aa.actor_id=?)) AND (k.seq IS NOT NULL OR o.wire_json IS NULL)
                      AND ((p.external_id IS NOT NULL AND ps.'''+kind+'''_id IS NOT NULL) OR k.seq IS NOT NULL)
                      AND NOT EXISTS (SELECT 1 FROM '''+prefix+'''_outbox earlier WHERE earlier.attachment_id=o.attachment_id AND earlier.seq<o.seq)
                      ORDER BY o.seq LIMIT 1''',(kind,local_actor,local_actor)).fetchone()
                    if not row:
                        if db.execute('SELECT 1 FROM '+prefix+'_outbox o JOIN attachments a ON a.id=o.attachment_id LEFT JOIN '+prefix+"_archive_actor aa ON aa.seq=o.seq WHERE (o.action='create' AND a.uploaded_by=?) OR (o.action='archive' AND aa.actor_id=?) LIMIT 1",(local_actor,local_actor)).fetchone():raise AttachmentParentPendingError('Eigene Anhangsänderungen warten auf den Vorgangsabgleich oder einen begonnenen administrativen Versand.')
                        break
                    seq,op,local,subject,name,path,size,digest,frozen,external,parent_external,confirmed,claim_actor,claim_server,action,prior,uploader=row
                    if claim_actor is not None and (claim_actor!=local_actor or claim_server!=actor):raise LocalStoreError('Der Anhang wurde für ein anderes persönliches Serverkonto vorgemerkt.')
                    if not parent_external or not confirmed:raise AttachmentParentPendingError('Der Anhang wartet auf einen synchronisierten Vorgang.')
                    author=employee_external_id(db,local_actor)
                    if action=='create':
                        record={'id':external,'subjectType':kind,'subjectId':parent_external,'originalName':name,'sizeBytes':size,'sha256':digest,'uploadedBy':author,'active':True};expected=None
                    else:
                        if capabilities.get('personalAttachmentArchive')!=1:raise LocalStoreError('Der Server unterstützt die persönliche Anhangsarchivierung noch nicht.')
                        if not prior:raise AttachmentParentPendingError('Die Archivierung wartet auf die bestätigte Anlage des Anhangs.')
                        expected=json.loads(prior)
                        uploader_account=str(account(db,uploader));uploader_external=employee_external_id(db,uploader)
                        if expected.get('uploadedBy') not in (uploader_account,uploader_external) or any(expected.get(k)!=v for k,v in {'id':external,'subjectType':kind,'subjectId':parent_external,'originalName':name,'sizeBytes':size,'sha256':digest,'active':True}.items()):raise LocalStoreError('Die Anhangsmetadaten oder der gemeinsame Ausgangsstand wurden geändert.')
                        record={**expected,'active':False}
                    value={'clientId':client_id,'operationId':op,'action':action,'attachment':record,'expected':expected}
                    if action=='archive':value['actorExternalId']=author
                    if frozen is not None and json.loads(frozen)!=value:raise LocalStoreError('Ein ausstehender Anhang oder seine Zuordnung wurde geändert.')
                    if frozen is None:
                        db.execute('INSERT INTO '+prefix+'_personal_claim VALUES(?,?,?)',(seq,local_actor,actor))
                        db.execute('UPDATE '+prefix+'_outbox SET wire_json=? WHERE seq=?',(encode(value),seq))
                if action=='create':
                    staged=os.path.join(temp,'upload');_staged_copy(store,kind,subject,path,size,digest,staged)
                    uploaded=command('-action','upload-personal-attachment','-subject-type',kind,'-subject-id',parent_external,'-author-id',author,'-file',staged)
                    if uploaded.get('sha256')!=digest:raise LocalStoreError('Die Prüfsumme des übertragenen Anhangs stimmt nicht.')
                command('-action','sync-personal-attachment-event','-subject-type',kind,'-payload',encode(value))
                with transaction(store,lock,is_current) as (db,current_actor):
                    if current_actor!=local_actor or account(db,current_actor)!=actor:raise LocalStoreError('Sitzung oder Konto-Zuordnung wurde geändert.')
                    if db.execute('SELECT wire_json FROM '+prefix+'_outbox WHERE seq=? AND operation_id=?',(seq,op)).fetchone()!=(encode(value),) or db.execute('SELECT actor_id,server_actor FROM '+prefix+'_personal_claim WHERE seq=?',(seq,)).fetchone()!=(local_actor,actor):raise LocalStoreError('Die persönliche Anhangswarteschlange wurde geändert.')
                    saved={**record,'importedBy':actor} if action=='create' else record
                    db.execute('INSERT INTO '+prefix+'_shadow VALUES(?,?) ON CONFLICT(attachment_id) DO UPDATE SET attachment_json=excluded.attachment_json',(local,encode(saved)))
                    db.execute('DELETE FROM '+prefix+'_outbox WHERE seq=?',(seq,));db.execute('DELETE FROM '+prefix+'_personal_claim WHERE seq=?',(seq,))
                    db.execute('DELETE FROM '+prefix+'_archive_actor WHERE seq=?',(seq,))
                    db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'attachment.sync.sent_personal',?)",(str(local_actor),str(local)));sent+=1
        if subject_id is not None:return {'sent':sent,'received':pull_personal_attachments(store,lock,is_current,command,url,actor,kind,subject_id,capabilities.get('personalAttachmentStates')==1)}
        return {'sent':sent}
    except (sqlite3.Error,ValueError,KeyError,OSError) as exc:raise LocalStoreError('Persönlicher Anhangsabgleich fehlgeschlagen: '+str(exc)) from exc

def pull_personal_attachments(store,lock,is_current,command,url,actor,kind,subject_id,check_states=False):
    if type(subject_id) is not int or subject_id<=0:raise LocalStoreError('Ungültiger Vorgang.')
    prefix='linux_'+kind+'_attachment_sync';parent='linux_'+kind+'_sync_config';shadow='linux_'+kind+'_sync_shadow'
    def baseline(db,person):
        if account(db,person)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
        if db.execute('SELECT server_url FROM '+parent).fetchone()!=(url,):raise LocalStoreError('Der Vorgang gehört zu einem anderen Server.')
        row=db.execute("SELECT l.external_id FROM sync_object_links l JOIN "+shadow+" s ON s."+kind+"_id=l.local_id WHERE l.object_type=? AND l.local_id=?",(kind,subject_id)).fetchone()
        if not row or not row[0]:raise LocalStoreError('Der Vorgang muss zuerst mit dem Server abgeglichen werden.')
        return row[0],employee_external_id(db,person)
    with transaction(store,lock,is_current) as (db,local_actor):
        store.attachments(kind,subject_id);external,viewer=baseline(db,local_actor)
        known=[]
        if check_states:
            rows=db.execute('SELECT h.attachment_json FROM '+prefix+'_shadow h JOIN attachments a ON a.id=h.attachment_id WHERE a.subject_type=? AND a.subject_id=? AND a.active=1 AND NOT EXISTS (SELECT 1 FROM '+prefix+'_outbox o WHERE o.attachment_id=a.id) ORDER BY a.id',(kind,subject_id)).fetchall()
            known=[json.loads(row[0]) for row in rows]
            if len(known)>10000:raise LocalStoreError('Zu viele bekannte Anhänge für den persönlichen Abgleich.')
    snapshot=command('-action','personal-subject-attachments','-subject-type',kind,'-subject-id',external,'-author-id',viewer)
    if not isinstance(snapshot,list) or len(snapshot)>100000:raise LocalStoreError('Ungültige Anhangsliste vom Server.')
    states=[]
    if check_states:
        for offset in range(0,len(known),50):
            batch=command('-action','personal-attachment-states','-subject-type',kind,'-subject-id',external,'-author-id',viewer,'-payload',encode(known[offset:offset+50]))
            if not isinstance(batch,list):raise LocalStoreError('Ungültige Anhangszustände vom Server.')
            states.extend(batch)
    if not isinstance(states,list) or len(states)!=len(known) or any(not isinstance(v,dict) or not isinstance(v.get('id'),str) or type(v.get('active')) is not bool for v in states) or {v['id'] for v in states}!={v.get('id') for v in known}:raise LocalStoreError('Ungültige Anhangszustände vom Server.')
    seen=set();downloads={};created_paths=[];received=0
    try:
        with tempfile.TemporaryDirectory(prefix='aura-personal-download-') as temp:
            for raw in snapshot:
                _validate(raw,kind)
                if not raw['active'] or raw['subjectId']!=external or raw['id'] in seen:raise LocalStoreError('Ungültiger oder doppelter Anhang vom Server.')
                seen.add(raw['id'])
                with transaction(store,lock,is_current) as (db,current):
                    if current!=local_actor or baseline(db,current)!=(external,viewer):raise LocalStoreError('Sitzung oder Vorgangszuordnung wurde geändert.')
                    known_attachment=db.execute("SELECT 1 FROM sync_object_links WHERE object_type='attachment' AND external_id=?",(raw['id'],)).fetchone()
                if not known_attachment:
                    staged=os.path.join(temp,uuid.uuid4().hex)
                    command('-action','personal-subject-attachment-download','-subject-type',kind,'-subject-id',external,'-id',raw['id'],'-author-id',viewer,'-sha256',raw['sha256'],'-size',str(raw['sizeBytes']),'-file',staged)
                    downloads[raw['id']]=staged
            with transaction(store,lock,is_current) as (db,current):
                if current!=local_actor or baseline(db,current)!=(external,viewer):raise LocalStoreError('Sitzung oder Vorgangszuordnung wurde geändert.')
                db.execute('UPDATE '+prefix+'_guard SET suppressed=1 WHERE singleton=1')
                for raw in snapshot:
                    try:number=int(raw['uploadedBy'])
                    except ValueError:number=0
                    if number>0 and str(number)==raw['uploadedBy']:uploader=local_id(db,number)
                    else:
                        match=db.execute("SELECT l.local_id FROM sync_object_links l JOIN linux_employee_sync_shadow s ON s.employee_id=l.local_id WHERE l.object_type='employee' AND l.external_id=?",(raw['uploadedBy'],)).fetchone();uploader=match[0] if match else None
                    if uploader is None or raw.get('uploaderExternalId')!=employee_external_id(db,uploader):raise LocalStoreError('Die Serverzuordnung eines Anhangsautors hat sich geändert. Bitte zuerst Mitarbeiter abgleichen.')
                    remote={k:v for k,v in raw.items() if k!='uploaderExternalId'}
                    row=db.execute("SELECT a.id,a.subject_id,a.original_name,a.stored_path,a.size_bytes,a.sha256,a.uploaded_by,a.active,h.attachment_json FROM sync_object_links l JOIN attachments a ON a.id=l.local_id LEFT JOIN "+prefix+"_shadow h ON h.attachment_id=a.id WHERE l.object_type='attachment' AND l.external_id=?",(raw['id'],)).fetchone()
                    if row:
                        local,subject,name,path,size,digest,author,active,old=row
                        if db.execute('SELECT 1 FROM '+prefix+'_outbox WHERE attachment_id=?',(local,)).fetchone():continue
                        if not old:raise LocalStoreError('Anhangskonflikt: kein gemeinsamer Ausgangsstand.')
                        previous=json.loads(old)
                        if subject!=subject_id or name!=previous['originalName'] or size!=previous['sizeBytes'] or digest!=previous['sha256'] or author!=uploader or bool(active)!=previous['active'] or any(remote.get(k,0)!=previous.get(k,0) for k in ('id','subjectType','subjectId','originalName','sizeBytes','sha256','uploadedBy','importedBy')):raise LocalStoreError('Anhangskonflikt: Der gemeinsame Stand wurde geändert.')
                        if not active:
                            _staged_copy(store,kind,subject_id,path,size,digest,os.path.join(temp,uuid.uuid4().hex))
                            db.execute('UPDATE attachments SET active=1 WHERE id=?',(local,));received+=1
                    else:
                        staged=downloads.get(raw['id'])
                        if not staged:raise LocalStoreError('Ein neuer Anhang muss erneut heruntergeladen werden.')
                        folder=store.path.parent.resolve(strict=True)
                        for part in ('attachments',kind,str(subject_id)):
                            folder=folder/part;folder.mkdir(mode=0o700,exist_ok=True)
                            if folder.is_symlink() or not folder.is_dir():raise LocalStoreError('Ungültiger Anhangsordner.')
                        dest=folder/(uuid.uuid4().hex+'.bin');fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created_paths.append(dest)
                        with open(staged,'rb') as src,os.fdopen(fd,'wb') as out:shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
                        local=db.execute('INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,active) VALUES(?,?,?,?,?,?,?,1)',(kind,subject_id,raw['originalName'],str(dest),raw['sizeBytes'],raw['sha256'],uploader)).lastrowid
                        db.execute("INSERT INTO sync_object_links VALUES('attachment',?,?)",(raw['id'],local));received+=1
                    db.execute('INSERT INTO '+prefix+'_shadow VALUES(?,?) ON CONFLICT(attachment_id) DO UPDATE SET attachment_json=excluded.attachment_json',(local,encode(remote)))
                known_by_id={v['id']:v for v in known}
                for status in states:
                    if status['active']:continue
                    if status['id'] in seen:raise LocalStoreError('Der Serverstand hat sich während des Anhangsabgleichs geändert.')
                    previous=known_by_id[status['id']]
                    row=db.execute("SELECT a.id,a.subject_id,a.original_name,a.size_bytes,a.sha256,a.uploaded_by,a.active,h.attachment_json FROM sync_object_links l JOIN attachments a ON a.id=l.local_id JOIN "+prefix+"_shadow h ON h.attachment_id=a.id WHERE l.object_type='attachment' AND l.external_id=?",(status['id'],)).fetchone()
                    if not row:raise LocalStoreError('Ein bekannter Anhang fehlt lokal.')
                    local,subject,name,size,digest,uploader,active,current_shadow=row
                    if db.execute('SELECT 1 FROM '+prefix+'_outbox WHERE attachment_id=?',(local,)).fetchone():continue
                    if json.loads(current_shadow)!=previous or subject!=subject_id or not active or previous.get('active') is not True or name!=previous['originalName'] or size!=previous['sizeBytes'] or digest!=previous['sha256'] or status.get('uploaderExternalId')!=employee_external_id(db,uploader):raise LocalStoreError('Die lokale Anhangs- oder Autorenzuordnung hat sich geändert.')
                    db.execute('UPDATE attachments SET active=0 WHERE id=?',(local,))
                    db.execute('UPDATE '+prefix+'_shadow SET attachment_json=? WHERE attachment_id=?',(encode({**previous,'active':False}),local));received+=1
                db.execute('UPDATE '+prefix+'_guard SET suppressed=0 WHERE singleton=1')
                db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'attachment.sync.received_personal',?)",(str(local_actor),str(received)))
            created_paths.clear()
        return received
    except (sqlite3.Error,ValueError,KeyError,OSError) as exc:raise LocalStoreError('Persönlicher Anhangsempfang fehlgeschlagen: '+str(exc)) from exc
    finally:
        for path in created_paths:
            try:path.unlink()
            except OSError:pass

def attachment_sync_config(store,lock,is_current,kind):
    if kind not in ('task','ticket'):raise LocalStoreError('Ungültiger Anhangsbereich.')
    table='linux_'+kind+'_attachment_sync_config'
    try:
        with _db(store,lock,is_current) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return None
            row=db.execute('SELECT server_url,server_actor FROM '+table+' WHERE singleton=1').fetchone()
            return {'serverUrl':row[0],'actor':row[1]} if row else None
    except sqlite3.Error as exc:raise LocalStoreError('Anhangskonfiguration konnte nicht gelesen werden: '+str(exc)) from exc

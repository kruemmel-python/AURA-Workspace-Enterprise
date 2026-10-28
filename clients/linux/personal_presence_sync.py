"""Synchronize only an authenticated person's queued presence changes."""
import json,sqlite3
from leave_sync import transaction,account
from room_sync import connect_sync_server
from local_store import LocalStoreError


def _pull_own_presence(store,lock,is_current,command,url,actor):
    def baseline(db,local_actor):
        if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
        if db.execute('SELECT server_url FROM linux_employee_sync_config').fetchone()!=(url,):raise LocalStoreError('Die Mitarbeiterwarteschlange gehört zu einem anderen Server.')
        if db.execute('SELECT 1 FROM linux_employee_sync_outbox WHERE employee_id=?',(local_actor,)).fetchone():raise LocalStoreError('Eine lokale Mitarbeiteränderung steht noch aus.')
        row=db.execute("SELECT e.name,e.role,e.department,e.presence,e.active,l.external_id,s.employee_json FROM employees e JOIN sync_object_links l ON l.object_type='employee' AND l.local_id=e.id JOIN linux_employee_sync_shadow s ON s.employee_id=e.id WHERE e.id=?",(local_actor,)).fetchone()
        if not row:raise LocalStoreError('Der gemeinsame Mitarbeiterstand fehlt.')
        name,role,department,presence,active,external,shadow=row;shared=json.loads(shadow)
        current={'id':external,'name':name,'role':role,'department':department,'presence':presence,'active':bool(active),'accountEmployeeId':actor}
        if current!=shared:raise LocalStoreError('Der lokale Mitarbeiterstand weicht vom gemeinsamen Ausgangsstand ab.')
        return current
    with transaction(store,lock,is_current) as (db,local_actor):before=baseline(db,local_actor)
    remote=command('-action','my-presence-details')
    if remote.get('employeeId')!=before['id']:raise LocalStoreError('Das Serverkonto ist einem anderen Mitarbeiter zugeordnet.')
    if remote.get('presence') not in ('office','remote','meeting','away'):raise LocalStoreError('Ungültiger Präsenzstatus vom Server.')
    with transaction(store,lock,is_current) as (db,current):
        if current!=local_actor or baseline(db,current)!=before:raise LocalStoreError('Der lokale Mitarbeiterstand hat sich geändert. Bitte erneut abgleichen.')
        if remote['presence']==before['presence']:return 0
        db.execute('UPDATE linux_employee_sync_guard SET suppressed=1 WHERE singleton=1')
        db.execute('UPDATE employees SET presence=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(remote['presence'],local_actor))
        db.execute('UPDATE linux_employee_sync_guard SET suppressed=0 WHERE singleton=1')
        after={**before,'presence':remote['presence']}
        db.execute('UPDATE linux_employee_sync_shadow SET employee_json=? WHERE employee_id=?',(json.dumps(after,ensure_ascii=False,separators=(',',':')),local_actor))
        db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'employee.presence.received',?)",(str(local_actor),str(local_actor)))
    return 1


def sync_personal_presence(store,lock,is_current,client,url,token,ca):
    url,actor,command=connect_sync_server(client,url,token,ca,'personalPresenceSync',None,'persönliches Serverkonto')
    sent=0
    try:
        while True:
            with transaction(store,lock,is_current) as (db,local_actor):
                if account(db,local_actor)!=actor:raise LocalStoreError('Die lokale Person ist nicht diesem Serverkonto zugeordnet.')
                config=db.execute('SELECT server_url FROM linux_employee_sync_config').fetchone()
                if not config or config[0]!=url:raise LocalStoreError('Bitte zuerst das Mitarbeiterverzeichnis für diesen Server abgleichen.')
                row=db.execute('SELECT seq,operation_id,action,employee_json,expected_json,actor_id FROM linux_employee_sync_outbox WHERE employee_id=? ORDER BY seq LIMIT 1',(local_actor,)).fetchone()
                if not row:break
                seq,op,action,payload,expected,acting=row
                if acting!=local_actor:raise LocalStoreError('Eine Änderung einer anderen Person muss zuerst administrativ abgeglichen werden.')
                if action!='update' or not expected:raise LocalStoreError('Eine ältere Mitarbeiteränderung muss zuerst administrativ abgeglichen werden.')
                desired=json.loads(payload);base=json.loads(expected)
                if any(desired.get(k)!=base.get(k) for k in ('id','name','role','department','active','accountEmployeeId')) or desired.get('presence')==base.get('presence'):
                    raise LocalStoreError('Diese Änderung enthält mehr als den eigenen Präsenzstatus und muss administrativ abgeglichen werden.')
                shadow=db.execute('SELECT employee_json FROM linux_employee_sync_shadow WHERE employee_id=?',(local_actor,)).fetchone()
                if not shadow or json.loads(shadow[0])!=base or base.get('accountEmployeeId')!=actor:
                    raise LocalStoreError('Der gemeinsame Mitarbeiterstand hat sich geändert. Bitte den Mitarbeiterabgleich prüfen.')
                link=db.execute("SELECT external_id FROM sync_object_links WHERE object_type='employee' AND local_id=?",(local_actor,)).fetchone()
                if not link or link[0]!=base.get('id'):raise LocalStoreError('Die Mitarbeiterzuordnung hat sich geändert.')
            remote=command('-action','my-presence-details')
            if remote.get('employeeId')!=base['id']:raise LocalStoreError('Das Serverkonto ist einem anderen Mitarbeiter zugeordnet.')
            if remote.get('presence')==base['presence']:
                remote=command('-action','set-my-presence-checked','-id',base['id'],'-status',base['presence'],'-presence',desired['presence'])
            if remote.get('employeeId')!=base['id'] or remote.get('presence')!=desired['presence']:
                raise LocalStoreError('Der Serverstatus hat sich geändert. Bitte den Mitarbeiterkonflikt prüfen.')
            with transaction(store,lock,is_current) as (db,current):
                if current!=local_actor or account(db,current)!=actor:raise LocalStoreError('Sitzung oder Serverkonto-Zuordnung wurde geändert.')
                current_row=db.execute('SELECT seq,operation_id,action,employee_json,expected_json,actor_id FROM linux_employee_sync_outbox WHERE employee_id=? ORDER BY seq LIMIT 1',(local_actor,)).fetchone()
                shadow=db.execute('SELECT employee_json FROM linux_employee_sync_shadow WHERE employee_id=?',(local_actor,)).fetchone()
                if current_row!=row or not shadow or json.loads(shadow[0])!=base:
                    raise LocalStoreError('Die Mitarbeiterwarteschlange wurde geändert. Bitte erneut abgleichen.')
                db.execute('UPDATE linux_employee_sync_shadow SET employee_json=? WHERE employee_id=?',(payload,local_actor))
                db.execute('DELETE FROM linux_employee_sync_outbox WHERE seq=? AND operation_id=?',(seq,op))
                db.execute("INSERT INTO audit_log(actor,action,subject) VALUES(?,'employee.presence.synced',?)",(str(local_actor),str(local_actor)))
            sent+=1
        return {'sent':sent,'received':_pull_own_presence(store,lock,is_current,command,url,actor)}
    except (sqlite3.Error,ValueError,KeyError) as exc:raise LocalStoreError('Präsenzabgleich fehlgeschlagen: '+str(exc)) from exc

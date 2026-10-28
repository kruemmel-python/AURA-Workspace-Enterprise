#!/usr/bin/python3
import importlib.util
from pathlib import Path
import tempfile
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('local_store',root/'clients/linux/local_store.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
with tempfile.TemporaryDirectory(prefix='aura-roles-') as tmp:
    path=Path(tmp)/'workspace.db';admin=mod.LocalStore(path);admin.create('Admin','admin','Admin Passwort 2026');admin.open();admin.login('admin','Admin Passwort 2026')
    def reject(action):
        try:action()
        except mod.LocalStoreError:return
        raise AssertionError('unexpected success')
    protected=next(r for r in admin.roles() if r['code']=='admin')
    reject(lambda:admin.save_role(protected['id'],'admin','Andere','', 'rooms.manage'))
    reject(lambda:admin.archive_role(protected['id']))
    reject(lambda:admin.save_role(0,'invalid','Leer','',''))
    admin.save_role(0,'room_operator','Raumteam','Beschreibung','rooms.manage')
    role=next(r for r in admin.roles() if r['code']=='room_operator');assert role['permissions']==['rooms.manage']
    admin.save_employee(0,'Robin','Technik','IT');employee=next(e for e in admin.employees() if e['name']=='Robin')
    admin.assign_role(employee['id'],'room_operator');admin.create_account(employee['id'],'robin','Temporär Passwort 2026')
    user=mod.LocalStore(path);user.open();assert user.login('robin','Temporär Passwort 2026');user.change_password('Temporär Passwort 2026','Eigenes Passwort 2026')
    user.save_room(0,'Test','Berlin',3,'');reject(lambda:user.save_task(0,'Nein','IT','Mittel',employee['id']))
    admin.save_role(role['id'],'room_operator','Raumteam 2','Neu','tasks.manage')
    reject(lambda:user.save_room(0,'Kein Zugriff','Berlin',3,''));user.save_task(0,'Erlaubt','IT','Mittel',employee['id'])
    admin.save_project(0,'Auswählbar','IT','','planned',admin.session_employee())
    project=next(p['id'] for p in admin.projects() if p['name']=='Auswählbar')
    assert any(p['id']==project and p['active'] for p in user.projects_for_assignment())
    user.create_task_in_project('Mit Projekt','IT','Mittel',employee['id'],0,0,project)
    assert next(t for t in user.tasks() if t['title']=='Mit Projekt')['projectIds']==[project]
    admin.archive_role(role['id']);reject(lambda:user.save_task(0,'Nicht mehr','IT','Mittel',employee['id']))
    admin.save_project(0,'Projekt','IT','','planned',admin.session_employee())
    reject(lambda:user.set_project_edge(admin.tasks()[0]['id'],admin.projects()[0]['id'],False,True))
    assert next(e for e in admin.employees() if e['id']==employee['id'])['systemRole']==''
    assert any('role.archived' in entry for entry in admin.audit())
    reject(lambda:user.roles());reject(lambda:user.audit());reject(lambda:user.room_resolution_history());reject(lambda:user.save_role(0,'admin2','X','','*'))
    user.close();admin.close();admin.open();admin.login('admin','Admin Passwort 2026')
    assert not any(r['code']=='room_operator' for r in admin.roles()) and any('role.archived' in entry for entry in admin.audit());admin.close()
print('Local roles and audit: live grants/revocation, protected admin, permissions and persistence: OK')

#!/usr/bin/python3
"""Exercise concurrent calls against one native session and its result buffer."""
import sys,tempfile,threading
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'clients/linux'))
from local_store import LocalStore
with tempfile.TemporaryDirectory(prefix='aura-concurrent-session-') as tmp:
    store=LocalStore(Path(tmp)/'workspace.db');store.create('Admin','admin','Admin Passwort 2026');store.open();store.login('admin','Admin Passwort 2026')
    for i in range(30):store.save_task(0,'Aufgabe '+str(i),'IT','Mittel',1)
    barrier=threading.Barrier(4);errors=[]
    def worker(call,check):
        try:
            barrier.wait(timeout=10)
            for _ in range(1000):assert check(call())
        except BaseException as exc:errors.append(exc)
    cases=((store.tasks,lambda value:len(value)==30),(store.identity,lambda value:value['employeeId']==1),(store.employees,lambda value:len(value)==1),(store.projects,lambda value:isinstance(value,list)))
    threads=[threading.Thread(target=worker,args=case) for case in cases]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads),'concurrent calls deadlocked'
    assert not errors,errors
    store.close()
print('Concurrent local session calls retain independent valid native responses: OK')

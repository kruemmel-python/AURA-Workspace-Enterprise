#!/usr/bin/python3
"""Exercise the desktop Go ABI against an isolated real Linux server."""
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
import urllib.error

root = Path(__file__).resolve().parents[1]
go = os.environ.get('AURA_GO', 'go')
with tempfile.TemporaryDirectory(prefix='aura-desktop-snapshot-') as tmp:
    tmp = Path(tmp)
    library = tmp / 'aura_services.so'
    env = os.environ.copy()
    env['GOTOOLCHAIN'] = 'local'
    subprocess.run([go, 'build', '-buildmode=c-shared', '-o', str(library), '.'], cwd=root/'services', env=env, check=True)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
    url = f'http://127.0.0.1:{port}'
    env.update({'AURA_SYNC_LISTEN':f'127.0.0.1:{port}', 'AURA_SYNC_URL':url,
                'AURA_SYNC_JOURNAL':str(tmp/'server.ndjson'), 'AURA_CENTRAL_DB':str(tmp/'central.sqlite'),
                'AURA_SYNC_TOKEN':'test-token', 'AURA_SYNC_CLIENT_ID':'desktop-a'})
    for key in ('AURA_SYNC_IDENTITIES','AURA_SYNC_REQUIRE_IDENTITIES','AURA_SYNC_REQUIRE_TLS','AURA_SYNC_TLS_CERT','AURA_SYNC_TLS_KEY','AURA_SYNC_CA_CERT'):
        env.pop(key, None)
    process = subprocess.Popen([str(root/'KR_BUILD/linux-x64/aura-sync-server'), '--server'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None: raise RuntimeError(process.stderr.read().decode())
            try:
                urllib.request.urlopen(url+'/health', timeout=1).close(); break
            except OSError: time.sleep(.05)
        else: raise RuntimeError('Server nicht gestartet')
        def request(path, data=None):
            headers={'Authorization':'Bearer test-token'}
            body=None if data is None else json.dumps(data).encode()
            return urllib.request.urlopen(urllib.request.Request(url+path,data=body,headers=headers),timeout=5)
        def event(topic, payload):
            with request('/events', {'topic':topic, 'payload':json.dumps(payload,ensure_ascii=False)}) as response: assert response.status==202
        event('task.created',{'id':'desktop-a:task','title':'Test','department':'IT','priority':'Mittel','status':'open','assigneeId':1,'createdBy':1,'active':True})
        attachment={'id':'desktop-a:attachment','subjectType':'task','subjectId':'desktop-a:task',
                    'originalName':'Nachweis ä.txt','sizeBytes':5,'sha256':'a'*64,'uploadedBy':'desktop-a:employee','active':True}
        event('attachment.created',attachment)
        archived=dict(attachment,active=False)
        event('attachment.archived',archived)
        client_script='''import ctypes,json,sys
dll=ctypes.CDLL(sys.argv[1]);snapshot=dll.aura_desktop_attachment_snapshot
snapshot.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_longlong)];snapshot.restype=ctypes.c_int
cursor=ctypes.c_longlong(999);tiny=ctypes.create_string_buffer(4)
code=snapshot(tiny,len(tiny),ctypes.byref(cursor));assert code==10 and cursor.value==999,(code,cursor.value)
buffer=ctypes.create_string_buffer(1024*1024)
assert snapshot(buffer,len(buffer),ctypes.byref(cursor))==0
events=json.loads(buffer.value);previous=cursor.value
assert snapshot(buffer,len(buffer),ctypes.byref(cursor))==0 and cursor.value==previous
assert json.loads(buffer.value)==events
print(json.dumps({'events':events,'cursor':cursor.value}))
'''
        result=json.loads(subprocess.run(['/usr/bin/python3','-c',client_script,str(library)],env=env,capture_output=True,text=True,check=True).stdout)
        events=result['events']; assert len(events)==1 and events[0]['topic']=='attachment.archived'
        payload=json.loads(events[0]['payload']);assert payload==archived
        with request('/api/v1/sync/changes?cursor=0') as response: feed=json.load(response)
        assert result['cursor']==feed['nextCursor']
        event('task.created',{'id':'desktop-a:next','title':'Folge','department':'IT','priority':'Mittel','status':'open','assigneeId':1,'createdBy':1,'active':True})
        recurrence={'clientId':'desktop-a','operationId':'recurrence-1','sourceId':'desktop-a:task','successorId':'desktop-a:next'}
        link_script='''import ctypes,sys
dll=ctypes.CDLL(sys.argv[1]);fn=dll.aura_sync_desktop_task_recurrence;fn.argtypes=[ctypes.c_char_p];fn.restype=ctypes.c_int
assert fn(sys.argv[2].encode())==0;assert fn(sys.argv[2].encode())==0
'''
        subprocess.run(['/usr/bin/python3','-c',link_script,str(library),json.dumps(recurrence)],env=env,check=True)
        with request('/api/v1/task-recurrence-link') as response:assert {'sourceId':'desktop-a:task','successorId':'desktop-a:next'} in json.load(response)
        recurrence_snapshot='''import ctypes,json,sys
dll=ctypes.CDLL(sys.argv[1]);fn=dll.aura_desktop_task_recurrence_snapshot
fn.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_longlong)];fn.restype=ctypes.c_int
cursor=ctypes.c_longlong(99);small=ctypes.create_string_buffer(4);assert fn(small,len(small),ctypes.byref(cursor))==10 and cursor.value==99
buf=ctypes.create_string_buffer(1024*1024);assert fn(buf,len(buf),ctypes.byref(cursor))==0
print(json.dumps({'links':json.loads(buf.value),'cursor':cursor.value}))
'''
        snapshot=json.loads(subprocess.run(['/usr/bin/python3','-c',recurrence_snapshot,str(library)],env=env,capture_output=True,text=True,check=True).stdout)
        assert {'sourceId':'desktop-a:task','successorId':'desktop-a:next'} in snapshot['links']
        with request('/api/v1/sync/changes?cursor=0') as response:assert snapshot['cursor']==json.load(response)['nextCursor']
        # A restarted client must recover the outbox and send bytes before metadata.
        content=b'Offline-Nachweis'; source=tmp/'offline.txt';source.write_bytes(content)
        digest=hashlib.sha256(content).hexdigest()
        pending={'id':'desktop-a:offline','subjectType':'task','subjectId':'desktop-a:task',
                 'originalName':'offline.txt','sizeBytes':len(content),'sha256':digest,
                 'uploadedBy':'desktop-a:employee','active':True}
        client_env=env.copy();client_env['AURA_SYNC_JOURNAL']=str(tmp/'client-outbox.ndjson')
        unused=socket.socket();unused.bind(('127.0.0.1',0));unused_port=unused.getsockname()[1];unused.close()
        offline_env=client_env.copy();offline_env['AURA_SYNC_URL']=f'http://127.0.0.1:{unused_port}'
        upload_script='''import ctypes,json,sys
dll=ctypes.CDLL(sys.argv[1]);fn=dll.aura_publish_desktop_attachment
fn.argtypes=[ctypes.c_char_p,ctypes.c_char_p];fn.restype=ctypes.c_int
code=fn(sys.argv[2].encode(),sys.argv[3].encode());assert code!=0,code
'''
        subprocess.run(['/usr/bin/python3','-c',upload_script,str(library),str(source),json.dumps(pending)],env=offline_env,check=True)
        outbox=tmp/'client-outbox.ndjson';assert outbox.stat().st_size>0
        flush_script='''import ctypes,sys
dll=ctypes.CDLL(sys.argv[1]);fn=dll.aura_flush_outbox;fn.restype=ctypes.c_int
code=fn();assert code==0,code
'''
        subprocess.run(['/usr/bin/python3','-c',flush_script,str(library)],env=client_env,check=True)
        assert outbox.read_bytes()==b''
        with request('/api/v1/attachments/'+digest) as response:assert response.read()==content
        with request('/api/v1/attachment-sync?subjectType=task') as response: attachments=json.load(response)
        assert any(item['id']==pending['id'] and item['sha256']==digest for item in attachments)
        # Another client archives and restores the attachment while an older
        # Windows archive remains in the outbox.
        archived_pending=dict(pending,active=False)
        event('attachment.archived',archived_pending)
        query=urllib.parse.urlencode({'subjectType':'task','subjectId':pending['subjectId'],'id':pending['id']})
        with request('/api/v1/subject-attachments?'+query,archived_pending) as response:assert response.status==204
        pending_op={'topic':'attachment.archived','payload':json.dumps(dict(archived_pending,baseCursor=0)),
                    'at':'2026-01-01T00:00:00Z','clientId':'desktop-a','operationId':'conflict-op'}
        later={'topic':'task.updated','payload':'{}','at':'2026-01-01T00:00:01Z'}
        outbox.write_text(json.dumps(pending_op)+'\n'+json.dumps(later)+'\n')
        resolve_script='''import ctypes,json,sys
dll=ctypes.CDLL(sys.argv[1]);inspect=dll.aura_inspect_desktop_attachment_archive
inspect.argtypes=[ctypes.c_void_p,ctypes.c_int];inspect.restype=ctypes.c_int
prepare=dll.aura_prepare_desktop_attachment_archive_abandon;prepare.argtypes=[ctypes.c_char_p];prepare.restype=ctypes.c_int
finish=dll.aura_abandon_desktop_attachment_archive;finish.argtypes=[ctypes.c_char_p];finish.restype=ctypes.c_int
buffer=ctypes.create_string_buffer(1024*1024);assert inspect(buffer,len(buffer))==0
review=json.loads(buffer.value);assert review['expected']['active'] is True
before=open(sys.argv[2],'rb').read();assert prepare(buffer.value)==0;assert open(sys.argv[2],'rb').read()==before
assert finish(buffer.value)==0
remaining=open(sys.argv[2],'rb').read().splitlines();assert len(remaining)==1 and json.loads(remaining[0])['topic']=='task.updated'
'''
        subprocess.run(['/usr/bin/python3','-c',resolve_script,str(library),str(outbox)],env=client_env,check=True)
        try:
            request('/api/v1/desktop-attachment-events',pending_op)
            raise AssertionError('abandoned operation accepted')
        except urllib.error.HTTPError as error:assert error.code==409
    finally:
        process.terminate();process.wait(timeout=5)
print('Desktop attachment C ABI: snapshot, offline upload, restart recovery and conflict fencing: OK')

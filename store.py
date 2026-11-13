"""Content-addressed file snapshots with transactional manifests and verified restore."""
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

CHUNK=16384
MAX_FILE=2*1024*1024
MAX_TOTAL=4*1024*1024
HASH=re.compile(r'[0-9a-f]{64}')
RESERVED={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}


class Conflict(Exception):pass
class Missing(Exception):pass
class Integrity(Exception):pass


def exact(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise ValueError(f'{label} needs exactly: {", ".join(keys)}.')


def digest(data):return hashlib.sha256(data).hexdigest()


def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')


def collection_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}',value):raise ValueError('Collection names use 1–48 lowercase letters, digits, or hyphens, starting with a letter.')
    return value


def portable_path(value):
    if not isinstance(value,str) or len(value)>180:raise ValueError('File paths are limited to 180 characters.')
    parts=value.split('/')
    if not 1<=len(parts)<=4:raise ValueError('Use at most four path components.')
    for part in parts:
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_. -]{0,63}',part) or part.endswith(('.', ' ')) or part.split('.')[0].casefold() in RESERVED:
            raise ValueError('Use portable relative paths: simple names, no traversal, hidden names, trailing dots/spaces, or device names.')
    return value


def check_paths(paths):
    lowered=sorted(portable_path(p).casefold() for p in paths)
    if len(lowered)!=len(set(lowered)):raise ValueError('Paths must be unique, ignoring case.')
    known=set(lowered)
    for path in lowered:
        parts=path.split('/')
        if any('/'.join(parts[:i]) in known for i in range(1,len(parts))):raise ValueError('A file cannot also be a parent directory.')


def validate_upload(value):
    exact(value,['collection','expected','files'],'Snapshot')
    collection_name(value['collection'])
    if type(value['expected']) is not int or value['expected']<0:raise ValueError('Expected version must be a nonnegative integer.')
    if not isinstance(value['files'],list) or not 1<=len(value['files'])<=32:raise ValueError('A snapshot needs 1–32 files.')
    files=[];total=0
    for f in value['files']:
        exact(f,['path','base64'],'File')
        portable_path(f['path'])
        if not isinstance(f['base64'],str) or len(f['base64'])>4*((MAX_FILE+2)//3):raise ValueError('Each file is limited to 2 MiB.')
        try:data=base64.b64decode(f['base64'],validate=True)
        except (ValueError,UnicodeEncodeError) as error:raise ValueError('File content must be valid base64.') from error
        if len(data)>MAX_FILE:raise ValueError('Each file is limited to 2 MiB.')
        total+=len(data)
        if total>MAX_TOTAL:raise ValueError('A snapshot is limited to 4 MiB of file data.')
        files.append((f['path'],data))
    check_paths([p for p,_ in files])
    return value['collection'],value['expected'],sorted(files,key=lambda f:f[0].casefold())


def validate_manifest(m):
    exact(m,['collection','version','files','totalBytes','chunkSize'],'Manifest')
    collection_name(m['collection'])
    if type(m['version']) is not int or m['version']<1 or m['chunkSize']!=CHUNK:raise ValueError('Invalid manifest version or chunk size.')
    if not isinstance(m['files'],list) or not 1<=len(m['files'])<=32:raise ValueError('Invalid manifest file count.')
    total=0
    for f in m['files']:
        exact(f,['path','size','sha256','chunks'],'Manifest file');portable_path(f['path'])
        if type(f['size']) is not int or not 0<=f['size']<=MAX_FILE or not isinstance(f['sha256'],str) or not HASH.fullmatch(f['sha256']):raise ValueError('Invalid file size or digest.')
        if not isinstance(f['chunks'],list) or len(f['chunks'])!=(f['size']+CHUNK-1)//CHUNK:raise ValueError('Invalid chunk count.')
        size=0
        for i,c in enumerate(f['chunks']):
            exact(c,['hash','size'],'Chunk')
            if not isinstance(c['hash'],str) or not HASH.fullmatch(c['hash']) or type(c['size']) is not int or not 1<=c['size']<=CHUNK or (i<len(f['chunks'])-1 and c['size']!=CHUNK):raise ValueError('Invalid chunk metadata.')
            size+=c['size']
        if size!=f['size']:raise ValueError('Chunk sizes do not reconstruct the file size.')
        total+=size
    check_paths([f['path'] for f in m['files']])
    if type(m['totalBytes']) is not int or total!=m['totalBytes'] or total>MAX_TOTAL:raise ValueError('Invalid total size.')
    return m


class Store:
    def __init__(self,root):
        self.root=Path(root);self.blobs=self.root/'blobs';self.blobs.mkdir(parents=True,exist_ok=True);self.path=self.root/'catalog.sqlite3'
        with self.connect() as db:db.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,collection TEXT NOT NULL,version INTEGER NOT NULL,manifest BLOB NOT NULL,digest TEXT NOT NULL,created TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),UNIQUE(collection,version));
            CREATE TABLE IF NOT EXISTS chunks(hash TEXT PRIMARY KEY,size INTEGER NOT NULL);
        ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def blob_path(self,hash_value):
        if not isinstance(hash_value,str) or not HASH.fullmatch(hash_value):raise Integrity('Invalid chunk address.')
        return self.blobs/hash_value[:2]/hash_value

    def read_chunk(self,c):
        try:data=self.blob_path(c['hash']).read_bytes()
        except OSError as error:raise Integrity('A required chunk is missing or unreadable.') from error
        if len(data)!=c['size'] or digest(data)!=c['hash']:raise Integrity('Chunk integrity verification failed.')
        return data

    def put_chunk(self,data):
        h=digest(data);path=self.blob_path(h)
        if path.exists():self.read_chunk({'hash':h,'size':len(data)});return h
        path.parent.mkdir(parents=True,exist_ok=True);temp=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,prefix='pending-',delete=False) as f:
                temp=Path(f.name);f.write(data);f.flush();os.fsync(f.fileno())
            os.replace(temp,path)
        finally:
            if temp is not None and temp.exists():temp.unlink()
        return h

    @staticmethod
    def load(db,sid):
        if type(sid) is not int or sid<1:raise ValueError('Snapshot ID must be positive.')
        row=db.execute('SELECT * FROM snapshots WHERE id=?',(sid,)).fetchone()
        if row is None:raise Missing('Snapshot not found.')
        if not isinstance(row['manifest'],bytes):raise Integrity('Stored manifest has an invalid representation.')
        payload=row['manifest']
        if digest(payload)!=row['digest']:raise Integrity('Manifest digest mismatch.')
        try:
            m=validate_manifest(json.loads(payload))
            if (m['collection'],m['version'])!=(row['collection'],row['version']):raise ValueError('Manifest identity mismatch.')
        except (ValueError,RecursionError) as error:raise Integrity('Stored manifest is invalid.') from error
        return {'id':sid,'collection':row['collection'],'version':row['version'],'digest':row['digest'],'created':row['created'],'manifest':m}

    def get(self,sid):
        with self.connect() as db:return self.load(db,sid)

    def create(self,value):
        collection,expected,files=validate_upload(value)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT coalesce(max(version),0) FROM snapshots WHERE collection=?',(collection,)).fetchone()[0]
            if current!=expected:raise Conflict(f'Collection changed from version {expected} to {current}. Refresh and review before saving.')
            if db.execute('SELECT count(*) FROM snapshots').fetchone()[0]>=200:raise ValueError('This pantry has reached its 200-snapshot limit.')
            m={'collection':collection,'version':current+1,'chunkSize':CHUNK,'totalBytes':sum(len(d) for _,d in files),'files':[]}
            for path,data in files:
                chunks=[]
                for at in range(0,len(data),CHUNK):
                    chunk=data[at:at+CHUNK];h=self.put_chunk(chunk);chunks.append({'hash':h,'size':len(chunk)});db.execute('INSERT OR IGNORE INTO chunks(hash,size) VALUES(?,?)',(h,len(chunk)))
                m['files'].append({'path':path,'size':len(data),'sha256':digest(data),'chunks':chunks})
            payload=canonical(m);sid=db.execute('INSERT INTO snapshots(collection,version,manifest,digest) VALUES(?,?,?,?)',(collection,current+1,payload,digest(payload))).lastrowid
            return self.load(db,sid)

    def state(self):
        with self.connect() as db:
            db.execute('BEGIN')
            snapshots=[self.load(db,row[0]) for row in db.execute('SELECT id FROM snapshots ORDER BY id DESC')]
            catalog={row['hash']:row['size'] for row in db.execute('SELECT hash,size FROM chunks')}
            logical=sum(s['manifest']['totalBytes'] for s in snapshots)
        physical={p.name:p.stat().st_size for p in self.blobs.glob('*/*') if p.is_file() and HASH.fullmatch(p.name)}
        return {'snapshots':[{'id':s['id'],'collection':s['collection'],'version':s['version'],'created':s['created'],'digest':s['digest'],'files':len(s['manifest']['files']),'bytes':s['manifest']['totalBytes']} for s in snapshots], 'storage':{'logicalBytes':logical,'cataloguedChunkBytes':sum(catalog.values()),'physicalChunkBytes':sum(physical.values()),'chunks':len(catalog),'orphanChunks':len(physical.keys()-catalog.keys()),'missingChunks':len(catalog.keys()-physical.keys()),'chunkSize':CHUNK}}

    def files(self,sid):
        snapshot=self.get(sid);result=[]
        for f in snapshot['manifest']['files']:
            data=b''.join(self.read_chunk(c) for c in f['chunks'])
            if len(data)!=f['size'] or digest(data)!=f['sha256']:raise Integrity('Reconstructed file digest mismatch.')
            result.append((f['path'],data))
        return snapshot,result

    def verify(self,sid):
        s,files=self.files(sid)
        return {'id':sid,'digest':s['digest'],'verified':True,'files':len(files),'bytes':sum(len(d) for _,d in files)}

    def archive(self,sid):
        s,files=self.files(sid);output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for path,data in files:
                info=zipfile.ZipInfo(path,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;archive.writestr(info,data)
        return s,output.getvalue()

    def compare(self,before,after):
        a,b=self.get(before),self.get(after);left={f['path']:f for f in a['manifest']['files']};right={f['path']:f for f in b['manifest']['files']};rows=[]
        for path in sorted(left.keys()|right.keys()):
            old,new=left.get(path),right.get(path);kind='added' if old is None else 'removed' if new is None else 'unchanged' if old['sha256']==new['sha256'] else 'modified'
            rows.append({'path':path,'change':kind,'beforeSize':old['size'] if old else None,'afterSize':new['size'] if new else None,'beforeHash':old['sha256'] if old else None,'afterHash':new['sha256'] if new else None})
        return {'before':before,'after':after,'counts':{kind:sum(r['change']==kind for r in rows) for kind in ['added','removed','modified','unchanged']},'rows':rows}

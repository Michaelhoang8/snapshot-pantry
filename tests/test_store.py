import base64
import concurrent.futures
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from store import Store,Conflict,Integrity,Missing,CHUNK,MAX_TOTAL,validate_upload,portable_path,canonical,digest


def upload(files,expected=0,collection='notes'):
    return {'collection':collection,'expected':expected,'files':[{'path':p,'base64':base64.b64encode(d).decode()} for p,d in files]}


class StoreTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root)
    def tearDown(self):self.temp.cleanup()

    def test_binary_empty_and_nested_files_restore_exactly(self):
        files=[('nested/binary.bin',bytes(range(256))*100),('empty.txt',b''),('unicode.txt','hello \u2603\r\n'.encode())]
        s=self.store.create(upload(files));self.assertEqual(s['version'],1);_,restored=self.store.files(s['id']);self.assertEqual(dict(restored),dict(files))
        _,data=self.store.archive(s['id'])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(set(z.namelist()),set(dict(files)));self.assertEqual({p:z.read(p) for p in z.namelist()},dict(files));self.assertIsNone(z.testzip())
        self.assertTrue(self.store.verify(s['id'])['verified'])

    def test_deduplication_across_files_versions_and_collections(self):
        block=b'A'*CHUNK;first=self.store.create(upload([('a.bin',block*2),('copy.bin',block*2)]))
        second=self.store.create(upload([('a.bin',block+b'B'*CHUNK)],1));self.store.create(upload([('other.bin',block)],collection='other'))
        state=self.store.state()['storage'];self.assertEqual(state['chunks'],2);self.assertEqual(state['cataloguedChunkBytes'],2*CHUNK);self.assertEqual(state['logicalBytes'],7*CHUNK)
        self.assertEqual(state['orphanChunks'],0);self.assertEqual(self.store.get(first['id'])['manifest']['files'][0]['size'],2*CHUNK)
        self.assertEqual(self.store.get(second['id'])['version'],2)

    def test_concurrent_same_collection_has_one_winner(self):
        def attempt(i):
            try:return self.store.create(upload([('file.txt',str(i).encode())]))['version']
            except Conflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:result=list(pool.map(attempt,range(8)))
        self.assertEqual(result.count(1),1);self.assertEqual(result.count('conflict'),7);self.assertEqual(len(self.store.state()['snapshots']),1)

    def test_transaction_failure_never_publishes_partial_snapshot(self):
        with self.store.connect() as db:db.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON snapshots BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.store.create(upload([('file.bin',b'A'*CHUNK)]))
        state=self.store.state();self.assertEqual(state['snapshots'],[]);self.assertEqual(state['storage']['chunks'],0);self.assertEqual(state['storage']['orphanChunks'],1)
        with self.store.connect() as db:db.execute('DROP TRIGGER fail_snapshot')
        self.store.create(upload([('file.bin',b'A'*CHUNK)]));self.assertEqual(self.store.state()['storage']['orphanChunks'],0)

    def test_corrupted_and_missing_chunks_fail_closed(self):
        s=self.store.create(upload([('file.bin',b'important')]))
        c=s['manifest']['files'][0]['chunks'][0];path=self.store.blob_path(c['hash']);path.write_bytes(b'corrupted')
        with self.assertRaises(Integrity):self.store.verify(s['id'])
        with self.assertRaises(Integrity):self.store.archive(s['id'])
        with self.assertRaises(Integrity):self.store.create(upload([('file.bin',b'important')],1))
        path.unlink()
        with self.assertRaises(Integrity):self.store.files(s['id'])
        self.assertEqual(self.store.state()['storage']['missingChunks'],1)

    def test_manifest_and_reconstructed_file_hashes_are_verified(self):
        s=self.store.create(upload([('file.txt',b'hello')]))
        with self.store.connect() as db:db.execute('UPDATE snapshots SET manifest=? WHERE id=?',(b'{}',s['id']))
        with self.assertRaises(Integrity):self.store.get(s['id'])
        m=s['manifest'];m['files'][0]['sha256']='0'*64;payload=canonical(m)
        with self.store.connect() as db:db.execute('UPDATE snapshots SET manifest=?,digest=? WHERE id=?',(payload,digest(payload),s['id']))
        with self.assertRaises(Integrity):self.store.files(s['id'])

    def test_comparison_classifies_added_removed_modified_unchanged(self):
        a=self.store.create(upload([('same.txt',b'same'),('change.txt',b'old'),('gone.txt',b'x')]))
        b=self.store.create(upload([('same.txt',b'same'),('change.txt',b'new'),('added.txt',b'y')],1))
        self.assertEqual(self.store.compare(a['id'],b['id'])['counts'],{'added':1,'removed':1,'modified':1,'unchanged':1})
        self.assertEqual(Store(self.root).get(b['id'])['digest'],b['digest'])

    def test_portable_paths_reject_traversal_devices_case_collisions_and_prefixes(self):
        for p in ['../x','/x','x\\y','C:/x','a/../b','.secret','a.','a ','CON','aux.txt','a//b','a/b/c/d/e']:
            with self.assertRaises(ValueError,msg=p):portable_path(p)
        for paths in [['a.txt','A.txt'],['a','a-b','a/child'],['a/b','a']]:
            with self.assertRaises(ValueError):validate_upload(upload([(p,b'') for p in paths]))
        self.assertEqual(portable_path('notes/field report.txt'),'notes/field report.txt')

    def test_validation_is_atomic_and_size_limits_are_enforced(self):
        for value in [{'collection':'notes','expected':True,'files':[]},upload([]),{'collection':'notes','expected':0,'files':[{'path':'x','base64':'!'}]},upload([('large.bin',b'x'*(2*1024*1024+1))]),upload([('a',b'x'*(2*1024*1024)),('b',b'x'*(2*1024*1024)),('c',b'x')])]:
            with self.assertRaises(ValueError):self.store.create(value)
        self.assertEqual(self.store.state()['snapshots'],[]);self.assertEqual(list(self.store.blobs.glob('*/*')),[])
        with self.assertRaises(Missing):self.store.get(99)


if __name__=='__main__':unittest.main()

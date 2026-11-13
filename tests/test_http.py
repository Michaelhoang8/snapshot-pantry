import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from run import make_server
from test_store import upload


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.server=make_server(self.temp.name);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,method,path,value=None,headers=None,raw=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5);data=raw if raw is not None else json.dumps(value).encode() if value is not None else None;c.request(method,path,data,{'Content-Type':'application/json',**(headers or {})});r=c.getresponse();body=r.read();status=r.status;mime=r.getheader('Content-Type','');disposition=r.getheader('Content-Disposition');c.close();return status,json.loads(body) if body and mime.startswith('application/json') else body,disposition

    def test_snapshot_verify_compare_and_zip_roundtrip(self):
        self.assertEqual(self.request('GET','/api/state')[1]['snapshots'],[])
        status,s,_=self.request('POST','/api/snapshots',upload([('notes/file.txt',b'hello\r\n')]))
        self.assertEqual(status,201);self.assertEqual(s['version'],1)
        self.assertEqual(self.request('POST','/api/snapshots',upload([('notes/file.txt',b'new')]))[0],409)
        self.assertTrue(self.request('POST','/api/verify',{'id':s['id']})[1]['verified'])
        status,archive,header=self.request('GET',f"/api/snapshots/{s['id']}/zip");self.assertEqual(status,200);self.assertIn('notes-v1.zip',header)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:self.assertEqual(z.read('notes/file.txt'),b'hello\r\n')
        self.assertEqual(self.request('POST','/api/compare',{'before':s['id'],'after':s['id']})[1]['counts']['unchanged'],1)
        self.assertEqual(self.request('GET','/api/snapshots/999')[0],404)

    def test_integrity_failure_prevents_archive_download(self):
        s=self.server.store.create(upload([('a.txt',b'payload')]))
        c=s['manifest']['files'][0]['chunks'][0];self.server.store.blob_path(c['hash']).write_bytes(b'bad')
        status,body,header=self.request('GET',f"/api/snapshots/{s['id']}/zip")
        self.assertEqual(status,422);self.assertIn('integrity',body['error']);self.assertIsNone(header)

    def test_transport_and_paths_are_bounded(self):
        for path in ['/data/catalog.sqlite3','/.git/config','/../store.py']:
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.invalid'})[0],400)
        self.assertEqual(self.request('POST','/api/verify',{},headers={'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.request('POST','/api/verify',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/verify',raw=b'{"id":1,"id":2}')[0],400)
        self.assertEqual(self.request('POST','/api/verify',raw=b'\xff')[0],400)
        self.assertEqual(self.request('POST','/api/verify',raw=b'{"id":NaN}')[0],400)
        self.assertEqual(self.request('POST','/api/verify',{},headers={'Content-Length':'6000001'})[0],413)
        self.assertEqual(self.request('HEAD','/api/state')[1],b'')
        self.assertEqual(self.request('DELETE','/api/state')[0],405)


if __name__=='__main__':unittest.main()

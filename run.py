"""Serve Snapshot Pantry on loopback with a local content-addressed store."""
import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from store import Store,Conflict,Missing,Integrity,exact

ROOT=Path(__file__).resolve().parent
ASSETS={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}


def pairs(items):
    out={}
    for key,value in items:
        if key in out:raise ValueError(f'Duplicate JSON field: {key}')
        out[key]=value
    return out


def constant(value):raise ValueError('JSON constants must be finite.')


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(15)

    def reply(self,status,value,mime='application/json',filename=None):
        body=value if isinstance(value,bytes) else json.dumps(value,allow_nan=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)

    def handle_request(self):
        try:
            hosts=self.headers.get_all('Host',[])
            if len(hosts)!=1 or not re.fullmatch(r'(localhost|127\.0\.0\.1)(:[0-9]{1,5})?',hosts[0],re.I):return self.reply(400,{'error':'Use a loopback Host header.'})
            path=urlsplit(self.path).path
            if self.command in ('GET','HEAD'):
                if path in ASSETS:
                    name,mime=ASSETS[path];return self.reply(200,(ROOT/name).read_bytes(),mime+'; charset=utf-8')
                if path=='/api/state':return self.reply(200,self.server.store.state())
                match=re.fullmatch(r'/api/snapshots/([1-9][0-9]{0,8})(/zip)?',path)
                if match:
                    sid=int(match[1])
                    if match[2]:
                        snapshot,archive=self.server.store.archive(sid)
                        return self.reply(200,archive,'application/zip',f"{snapshot['collection']}-v{snapshot['version']}.zip")
                    return self.reply(200,self.server.store.get(sid))
                return self.reply(404,{'error':'Not found.'})
            if self.command!='POST':return self.reply(405,{'error':'Method not supported.'})
            origins=self.headers.get_all('Origin',[]);allowed=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
            if len(origins)>1 or (origins and origins[0] not in allowed):return self.reply(403,{'error':'Cross-origin requests are rejected.'})
            lengths=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit() or not 0<int(lengths[0])<=6000000:return self.reply(413,{'error':'Provide a JSON body of at most 6 MB.'})
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':return self.reply(415,{'error':'Use application/json.'})
            raw=self.rfile.read(int(lengths[0]))
            if len(raw)!=int(lengths[0]):raise ValueError('Incomplete request body.')
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
            if path=='/api/snapshots':return self.reply(201,self.server.store.create(value))
            if path=='/api/verify':
                exact(value,['id'],'Verification');return self.reply(200,self.server.store.verify(value['id']))
            if path=='/api/compare':
                exact(value,['before','after'],'Comparison');return self.reply(200,self.server.store.compare(value['before'],value['after']))
            return self.reply(404,{'error':'Not found.'})
        except Conflict as error:self.reply(409,{'error':str(error)})
        except Missing as error:self.reply(404,{'error':str(error)})
        except Integrity as error:self.reply(422,{'error':str(error)})
        except (ValueError,UnicodeDecodeError,RecursionError) as error:self.reply(400,{'error':str(error)})
        except (sqlite3.OperationalError,OSError,TimeoutError):self.reply(503,{'error':'Local storage is temporarily unavailable. Retry after checking the data directory.'})

    do_GET=handle_request
    do_HEAD=handle_request
    do_POST=handle_request
    do_PUT=handle_request
    do_DELETE=handle_request


def make_server(root,port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.store=Store(root);return server


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8000);parser.add_argument('--data',default=str(ROOT/'data'));args=parser.parse_args();server=make_server(args.data,args.port)
    print(f'Snapshot Pantry: http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()

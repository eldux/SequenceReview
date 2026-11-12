# SPDX-License-Identifier: AGPL-3.0-only
"""Sequence Review local HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile, ZIP_DEFLATED

from PIL import Image, ImageOps
from catalog import Catalog, SeparationConflict, natural, read, within, write

APP = Path(__file__).resolve().parent


class Service:
    def __init__(self):
        self.root = Path(os.getenv('SEQUENCE_REVIEW_ROOT','/data')).resolve()
        state_root = Path(os.getenv('SEQUENCE_REVIEW_STATE','/state')).resolve()
        mount_id = os.getenv('SEQUENCE_REVIEW_MOUNT_ID',str(self.root)).replace('\\','/')
        self.state = state_root / hashlib.sha256(mount_id.encode()).hexdigest()[:16]
        self.exports = Path(os.getenv('SEQUENCE_REVIEW_EXPORT_ROOT','/exports')).resolve()
        for folder in (self.state, self.exports):
            folder.mkdir(parents=True,exist_ok=True)
        if not self.root.is_dir():
            raise ValueError('Mount an existing input folder at /data.')
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.edit_lock = threading.Lock()
        self.jobs = {}
        self.current = None
        key = read(self.state/'current.json',{}).get('key')
        if key and (document := read(self.state/(key+'.json'))) is not None:
            self.current = Catalog(self.root,self.state,key,document)
            self.current.import_separations()

    def current_catalog(self, expected=None):
        current = self.current
        if current is None:
            raise ValueError('Open an image folder first.')
        if expected is not None and current.key!=expected:
            raise ValueError('Another folder was opened. Reload this page before editing.')
        return current

    def start_job(self, kind, payload, expected):
        with self.lock:
            if any(item['state']=='running' for item in self.jobs.values()):
                raise ValueError('A job is already running. Wait for it to finish.')
            while len(self.jobs)>=32:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = secrets.token_urlsafe(12)
            self.jobs[job_id] = {'id':job_id,'kind':kind,'state':'running','percent':0,'message':'Queued'}

        def update(**changes):
            with self.lock:
                self.jobs[job_id].update(changes)

        def progress(percent,message):
            update(percent=percent,message=message)

        def worker():
            try:
                if kind=='open':
                    mode = payload.get('mode')
                    folder = payload.get('folder')
                    numeric = payload.get('numbers')
                    if mode not in ('open','sequence') or not isinstance(folder,str):
                        raise ValueError('Choose a folder and a workflow.')
                    if mode=='sequence' and type(numeric) is not bool:
                        raise ValueError('Choose whether filenames should help identify sequences.')
                    if mode=='open':
                        numeric = False
                    catalog = Catalog.open(self.root,self.state,folder,mode,numeric,progress)
                    with self.edit_lock:
                        write(self.state/'current.json',{'key':catalog.key})
                        self.current = catalog
                    target, message = '', 'Folder ready'
                else:
                    with self.edit_lock:
                        catalog = self.current_catalog(expected)
                        if kind=='edit':
                            target = catalog.edit(payload,progress)
                            message = f'Saved to {target}'
                        else:
                            target = catalog.export(self.exports,payload.get('name'),progress)
                            message = f'Exported to {target}'
                update(state='done',percent=100,message=message,destination=target)
            except SeparationConflict as error:
                update(state='conflict',message=str(error),reasons=error.reasons,separation_override=error.token)
            except Exception as error:
                update(state='failed',message=str(error) or 'The job failed.')

        threading.Thread(target=worker,name=f'sequence-review-{kind}',daemon=False).start()
        return {'id':job_id}

    def status(self, job_id=None):
        with self.lock:
            job = self.jobs.get(job_id) if job_id else next(reversed(self.jobs.values()),None)
            return dict(job) if job else None


def main():
    app = Service()

    class Handler(BaseHTTPRequestHandler):
        def send(self, value, status=200, mime='application/json'):
            body = json.dumps(value).encode() if mime=='application/json' else value
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self, method):
            try:
                url = urlsplit(self.path)
                route, query = url.path, parse_qs(url.query)
                def param(name, default=''):
                    return query.get(name,[default])[0]
                host = urlsplit('//'+self.headers.get('Host','')).hostname
                if host not in ('localhost','127.0.0.1','::1'):
                    self.send({'error':'Open Sequence Review using localhost.'},403)
                    return
                if method=='GET' and route=='/health':
                    self.send({'ok':True})
                    return
                if method=='GET' and route in ('/','/review','/table','/singletons'):
                    if route!='/' and app.current is None:
                        self.send_response(303);self.send_header('Location','/');self.send_header('Content-Length','0');self.end_headers()
                        return
                    path = APP/('index.html' if route=='/' else 'review.html')
                    body = path.read_text(encoding='utf-8').replace('__REVIEW_TOKEN__',app.token)
                    self.send(body.encode(),mime='text/html; charset=utf-8')
                    return
                if method=='GET' and route=='/license':
                    self.send((APP/'LICENSE').read_bytes(),mime='text/plain; charset=utf-8')
                    return
                if method=='GET' and route=='/source':
                    archive = BytesIO()
                    files = ('app.py','catalog.py','sequencing.py','index.html','review.html','Dockerfile',
                             'compose.yaml','requirements.txt','README.md','LICENSE','.dockerignore','.gitignore','start.ps1')
                    with ZipFile(archive,'w',ZIP_DEFLATED) as bundle:
                        for file in files:
                            bundle.write(APP/file,file)
                    self.send(archive.getvalue(),mime='application/zip')
                    return
                if not secrets.compare_digest(self.headers.get('X-Review-Token') or param('token'),app.token):
                    self.send({'error':'Refresh the page to reconnect to the local service.'},403)
                    return
                if method=='GET':
                    if route=='/api/folders':
                        folder = within(app.root,param('folder','.'))
                        if not folder.is_dir():
                            raise ValueError('Folder does not exist.')
                        directories = [p.relative_to(app.root).as_posix() for p in folder.iterdir()
                                       if p.is_dir() and not p.is_symlink() and p.resolve().is_relative_to(app.root)]
                        current = app.current
                        self.send({'folder':folder.relative_to(app.root).as_posix(),
                                   'parent':folder.parent.relative_to(app.root).as_posix() if folder!=app.root else None,
                                   'folders':sorted(directories,key=natural),
                                   'active':current.document['folder'] if current else None})
                    elif route=='/api/edit-status':
                        self.send(app.status(param('id') or None))
                    elif route in ('/api/data','/api/flags','/api/revision','/image'):
                        catalog = app.current_catalog(param('workspace') or None)
                        if route=='/api/data':
                            self.send(catalog.data())
                        elif route=='/api/flags':
                            self.send(catalog.document['flags'])
                        elif route=='/api/revision':
                            self.send({'revision':catalog.document['revision'],'workspace':catalog.key})
                        else:
                            frame = catalog.document['frames'].get(param('id'))
                            if frame is None:
                                raise ValueError('Image not found.')
                            path = within(app.root,frame['path'])
                            if param('full')=='1' and path.suffix.lower() not in ('.tif','.tiff'):
                                self.send(path.read_bytes(),mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                            elif param('full')=='1':
                                with Image.open(path) as original:
                                    buffer = BytesIO()
                                    ImageOps.exif_transpose(original).convert('RGB').save(buffer,'PNG')
                                self.send(buffer.getvalue(),mime='image/png')
                            else:
                                size = (300,220) if param('thumb')=='1' else (2048,1536)
                                with Image.open(path) as original:
                                    original.draft('RGB',size)
                                    picture = ImageOps.exif_transpose(original).convert('RGB')
                                    picture.thumbnail(size)
                                    buffer = BytesIO()
                                    picture.save(buffer,'JPEG',quality=85)
                                self.send(buffer.getvalue(),mime='image/jpeg')
                    else:
                        self.send({'error':'Page not found.'},404)
                    return

                size = int(self.headers.get('Content-Length','0'))
                if not 0<size<=1_000_000:
                    raise ValueError('Invalid request size.')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload,dict):
                    raise ValueError('Expected a JSON object.')
                expected = self.headers.get('X-Review-Workspace')
                if route=='/api/flags':
                    with app.edit_lock:
                        self.send(app.current_catalog(expected).flag(payload))
                elif route in ('/api/open','/api/edit','/api/export'):
                    kind = route.rsplit('/',1)[1]
                    if kind!='open':
                        app.current_catalog(expected)
                    self.send(app.start_job(kind,payload,expected),202)
                else:
                    self.send({'error':'Unknown operation.'},404)
            except (BrokenPipeError,ConnectionResetError):
                return
            except (ValueError,OSError,KeyError,TypeError) as error:
                self.send({'error':str(error)},400)

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

    bind = os.getenv('SEQUENCE_REVIEW_BIND','127.0.0.1')
    port = int(os.getenv('SEQUENCE_REVIEW_PORT','8765'))
    with ThreadingHTTPServer((bind,port),Handler) as server:
        def terminate(*_):
            threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,terminate)
        print(f'Sequence Review listening on port {server.server_port}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__=='__main__':
    main()

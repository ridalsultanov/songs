#!/usr/bin/env python3
"""Лад — сервер синхронизации. Хранит данные песенника (звёздочки, аккорды, свои песни) по паролю.
Только стандартная библиотека Python 3. Слушает 127.0.0.1, HTTPS даёт Caddy."""
import hashlib, hmac, json, os, re, secrets, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get('LAD_DATA', '/var/lib/lad')
PORT = int(os.environ.get('LAD_PORT', '8787'))
ORIGINS = set(filter(None, os.environ.get('LAD_ORIGINS', 'https://ridalsultanov.github.io').split(',')))
MAX_BODY = 3 * 1024 * 1024
SPACE_RE = re.compile(r'^[a-z0-9_-]{2,32}$')
lock = threading.Lock()
fails = {}  # ip -> [timestamps]

def path(space, ext): return os.path.join(DATA, space + ext)

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return salt + ':' + h

def check_pw(space, pw):
    try:
        stored = open(path(space, '.auth')).read().strip()
    except FileNotFoundError:
        return False
    salt = stored.split(':', 1)[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)

def limited(ip):
    now = time.time()
    lst = [t for t in fails.get(ip, []) if now - t < 900]
    fails[ip] = lst
    return len(lst) >= 10

class H(BaseHTTPRequestHandler):
    server_version = 'LadSync/1'

    def cors(self):
        o = self.headers.get('Origin', '')
        if o in ORIGINS or '*' in ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o or '*')
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, PUT, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Max-Age', '86400')

    def reply(self, code, obj=None):
        body = json.dumps(obj if obj is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.headers.get('X-Forwarded-For', self.client_address[0]), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def auth(self):
        ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
        m = re.match(r'^/api/([a-z0-9_-]+)$', self.path.split('?')[0])
        if not m or not SPACE_RE.match(m.group(1)):
            self.reply(404, {'error': 'not_found'}); return None
        space = m.group(1)
        if limited(ip):
            self.reply(429, {'error': 'too_many_attempts'}); return None
        a = self.headers.get('Authorization', '')
        pw = a[7:] if a.startswith('Bearer ') else ''
        if not pw or not check_pw(space, pw):
            fails.setdefault(ip, []).append(time.time())
            self.reply(401, {'error': 'bad_password'}); return None
        return space

    def do_GET(self):
        if self.path.split('?')[0] in ('/', '/api', '/api/'):
            self.reply(200, {'ok': True, 'app': 'lad-sync'}); return
        space = self.auth()
        if not space: return
        try:
            with lock:
                doc = json.load(open(path(space, '.json')))
        except FileNotFoundError:
            doc = {'data': None, 'updated': 0}
        self.reply(200, doc)

    def do_PUT(self):
        space = self.auth()
        if not space: return
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BODY:
            self.reply(413, {'error': 'too_large'}); return
        try:
            doc = json.loads(self.rfile.read(n))
            assert isinstance(doc.get('data'), dict) and isinstance(doc.get('updated'), (int, float))
        except Exception:
            self.reply(400, {'error': 'bad_json'}); return
        with lock:
            p = path(space, '.json')
            try:
                cur = json.load(open(p))
            except FileNotFoundError:
                cur = {'updated': 0}
            if doc['updated'] < cur.get('updated', 0) and not doc.get('force'):
                self.reply(409, cur); return
            out = {'data': doc['data'], 'updated': doc['updated'], 'saved': int(time.time() * 1000)}
            if os.path.exists(p):  # предыдущая версия — на всякий случай
                os.replace(p, path(space, '.prev.json'))
            tmp = p + '.tmp'
            json.dump(out, open(tmp, 'w'), ensure_ascii=False)
            os.replace(tmp, p)
        self.reply(200, {'ok': True, 'updated': out['updated'], 'saved': out['saved']})

def add_user(space, pw=None):
    assert SPACE_RE.match(space), 'имя: латиница, цифры, - и _'
    os.makedirs(DATA, exist_ok=True)
    pw = pw or '-'.join(secrets.token_hex(2) for _ in range(3))
    open(path(space, '.auth'), 'w').write(hash_pw(pw))
    return pw

if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'adduser':
        pw = add_user(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print(f'{sys.argv[2]}: {pw}')
        sys.exit(0)
    os.makedirs(DATA, exist_ok=True)
    print(f'lad-sync on 127.0.0.1:{PORT}, data {DATA}, origins {sorted(ORIGINS)}', flush=True)
    ThreadingHTTPServer((os.environ.get('LAD_HOST', '127.0.0.1'), PORT), H).serve_forever()

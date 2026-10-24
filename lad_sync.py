#!/usr/bin/env python3
"""Лад — сервер синхронизации (версия 1.1).
Хранит данные песенника по паролю, передаёт песни между пользователями, подбирает и раздаёт обложки.
Только стандартная библиотека Python 3. Слушает 127.0.0.1, HTTPS даёт Caddy."""
import hashlib, hmac, json, os, re, secrets, sys, threading, time, unicodedata, urllib.parse, urllib.request
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.1'
DATA = os.environ.get('LAD_DATA', '/var/lib/lad')
PORT = int(os.environ.get('LAD_PORT', '8787'))
ORIGINS = set(filter(None, os.environ.get('LAD_ORIGINS', 'https://ridalsultanov.github.io').split(',')))
MAX_BODY = 3 * 1024 * 1024
SPACE_RE = re.compile(r'^[a-z0-9_-]{2,32}$')
MD5_RE = re.compile(r'^[0-9a-f]{32}$')
lock = threading.Lock()
fails = {}

def path(space, ext): return os.path.join(DATA, space + ext)

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()

def check_pw(space, pw):
    try: stored = open(path(space, '.auth')).read().strip()
    except FileNotFoundError: return False
    return hmac.compare_digest(hash_pw(pw, stored.split(':', 1)[0]), stored)

def exists(space): return bool(SPACE_RE.match(space)) and os.path.exists(path(space, '.auth'))

def limited(ip):
    now = time.time(); lst = [t for t in fails.get(ip, []) if now - t < 900]; fails[ip] = lst
    return len(lst) >= 10

def load(p, default):
    try: return json.load(open(p))
    except (FileNotFoundError, ValueError): return default

def dump(p, obj):
    tmp = p + '.tmp'; json.dump(obj, open(tmp, 'w'), ensure_ascii=False); os.replace(tmp, p)

# ---------- обложки ----------
TR = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ', 'a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch _ y _ e yu ya i yi e g'.split()))
def norm(s):
    s = ''.join(TR.get(c, c) for c in (s or '').lower()).replace('_', '')
    s = unicodedata.normalize('NFKD', s)
    s = re.sub(r'\(.*?\)', '', s); s = s.replace('&', 'and')
    return re.sub(r'[^a-z0-9]+', '', s)
def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return 0
    if a == b: return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    if (a in b or b in a) and min(len(a), len(b)) >= 4: r = max(r, .85)
    return r

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 LadSync'})
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()

def find_cover(artist, song):
    best = None
    for q in (f'artist:"{artist}" track:"{song}"', f'{artist} {song}'):
        try: items = json.loads(http_get('https://api.deezer.com/search?' + urllib.parse.urlencode({'q': q})))['data']
        except Exception: items = []
        for x in items:
            a, t = x.get('artist', {}).get('name', ''), x.get('title', '')
            sa, ss = sim(artist, a), max(sim(song, t), sim(song, x.get('title_short', '')))
            md5 = x.get('album', {}).get('md5_image')
            if md5 and sa >= .6 and ss >= .7 and (not best or ss * 2 + sa > best[0]): best = (ss * 2 + sa, md5)
        if best and best[0] >= 2.6: break
    return best[1] if best else None

def cover_file(md5):
    d = os.path.join(DATA, 'covers'); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, md5 + '.jpg')
    if not os.path.exists(p):
        img = http_get(f'https://cdn-images.dzcdn.net/images/cover/{md5}/250x250-000000-80-0-0.jpg')
        if img[:2] != b'\xff\xd8': raise ValueError('not jpeg')
        open(p + '.tmp', 'wb').write(img); os.replace(p + '.tmp', p)
    return p

class H(BaseHTTPRequestHandler):
    server_version = 'LadSync/' + VERSION

    def cors(self):
        o = self.headers.get('Origin', '')
        if o in ORIGINS or '*' in ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o or '*'); self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Max-Age', '86400')

    def reply(self, code, obj=None):
        body = json.dumps(obj if obj is not None else {}, ensure_ascii=False).encode()
        self.send_response(code); self.cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8'); self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.headers.get('X-Forwarded-For', self.client_address[0]), fmt % args))

    def do_OPTIONS(self): self.send_response(204); self.cors(); self.end_headers()

    def route(self):
        u = urllib.parse.urlparse(self.path)
        return [p for p in u.path.split('/') if p], urllib.parse.parse_qs(u.query)

    def auth(self, space):
        ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
        if not SPACE_RE.match(space): self.reply(404, {'error': 'not_found'}); return False
        if limited(ip): self.reply(429, {'error': 'too_many_attempts'}); return False
        a = self.headers.get('Authorization', ''); pw = a[7:] if a.startswith('Bearer ') else ''
        if not pw or not check_pw(space, pw):
            fails.setdefault(ip, []).append(time.time()); self.reply(401, {'error': 'bad_password'}); return False
        return True

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BODY: self.reply(413, {'error': 'too_large'}); return None
        try: return json.loads(self.rfile.read(n))
        except Exception: self.reply(400, {'error': 'bad_json'}); return None

    def do_GET(self):
        parts, q = self.route()
        if parts in ([], ['api']): self.reply(200, {'ok': True, 'app': 'lad-sync', 'version': VERSION}); return
        if len(parts) == 2 and parts[0] == 'cover' and parts[1].endswith('.jpg') and MD5_RE.match(parts[1][:-4]):
            try: data = open(cover_file(parts[1][:-4]), 'rb').read()
            except Exception: self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable'); self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
        if len(parts) < 2 or parts[0] != 'api': self.reply(404, {'error': 'not_found'}); return
        space = parts[1]
        if not self.auth(space): return
        if len(parts) == 2:
            with lock: self.reply(200, load(path(space, '.json'), {'data': None, 'updated': 0}))
        elif parts[2:] == ['inbox']:
            with lock: self.reply(200, {'items': load(path(space, '.inbox.json'), [])})
        elif parts[2:] == ['users']:
            users = sorted(f[:-5] for f in os.listdir(DATA) if f.endswith('.auth'))
            self.reply(200, {'users': [u for u in users if u != space]})
        elif parts[2:] == ['cover']:
            a, s = (q.get('a') or [''])[0], (q.get('s') or [''])[0]
            md5 = find_cover(a, s) if a and s else None
            if md5:
                try: cover_file(md5)
                except Exception: md5 = None
            self.reply(200, {'md5': md5})
        else: self.reply(404, {'error': 'not_found'})

    def do_PUT(self):
        parts, _ = self.route()
        if len(parts) != 2 or parts[0] != 'api': self.reply(404, {'error': 'not_found'}); return
        space = parts[1]
        if not self.auth(space): return
        doc = self.body()
        if doc is None: return
        if not (isinstance(doc.get('data'), dict) and isinstance(doc.get('updated'), (int, float))): self.reply(400, {'error': 'bad_json'}); return
        with lock:
            p = path(space, '.json'); cur = load(p, {'updated': 0})
            if doc['updated'] < cur.get('updated', 0) and not doc.get('force'): self.reply(409, cur); return
            out = {'data': doc['data'], 'updated': doc['updated'], 'saved': int(time.time() * 1000)}
            if os.path.exists(p): os.replace(p, path(space, '.prev.json'))
            dump(p, out)
        self.reply(200, {'ok': True, 'updated': out['updated'], 'saved': out['saved']})

    def do_POST(self):
        parts, _ = self.route()
        if len(parts) != 3 or parts[0] != 'api' or parts[2] != 'send': self.reply(404, {'error': 'not_found'}); return
        space = parts[1]
        if not self.auth(space): return
        doc = self.body()
        if doc is None: return
        to, song = doc.get('to', ''), doc.get('song') or {}
        if not exists(to) or to == space: self.reply(404, {'error': 'no_such_user'}); return
        if not (isinstance(song.get('a'), str) and isinstance(song.get('s'), str) and song['a'].strip() and song['s'].strip()):
            self.reply(400, {'error': 'bad_song'}); return
        item = {'id': secrets.token_hex(6), 'from': space, 't': int(time.time() * 1000),
                'song': {k: song[k] for k in ('a', 's', 'ch', 'c') if isinstance(song.get(k), str)}}
        with lock:
            p = path(to, '.inbox.json'); box = load(p, []); box.append(item); dump(p, box[-200:])
        self.reply(200, {'ok': True})

    def do_DELETE(self):
        parts, q = self.route()
        if len(parts) != 3 or parts[0] != 'api' or parts[2] != 'inbox': self.reply(404, {'error': 'not_found'}); return
        space = parts[1]
        if not self.auth(space): return
        ids = set((q.get('ids') or [''])[0].split(','))
        with lock:
            p = path(space, '.inbox.json'); dump(p, [x for x in load(p, []) if x.get('id') not in ids])
        self.reply(200, {'ok': True})

def add_user(space, pw=None):
    assert SPACE_RE.match(space), 'имя: латиница, цифры, - и _'
    os.makedirs(DATA, exist_ok=True)
    pw = pw or '-'.join(secrets.token_hex(2) for _ in range(3))
    open(path(space, '.auth'), 'w').write(hash_pw(pw))
    return pw

if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'adduser':
        print(f'{sys.argv[2]}: {add_user(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)}'); sys.exit(0)
    os.makedirs(DATA, exist_ok=True)
    print(f'lad-sync {VERSION} on 127.0.0.1:{PORT}, data {DATA}, origins {sorted(ORIGINS)}', flush=True)
    ThreadingHTTPServer((os.environ.get('LAD_HOST', '127.0.0.1'), PORT), H).serve_forever()

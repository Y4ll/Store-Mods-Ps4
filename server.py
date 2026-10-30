#!/usr/bin/env python3
"""Servidor para McStore (app PS4). Biblioteca estandar de Python 3.8+ (Pillow es opcional: miniaturas).

Endpoints:
  GET /browse?q=&page=1&sort=popular&cat=ID  -> {"page","pages","total","items":[...]}  (24 por pagina)
  GET /search?q=texto                        -> [...]  (compatibilidad con la app vieja)
  GET /detail/<id>                           -> {nombre, autor, descripcion completa, descargas, ...}
  GET /thumb/<id>?s=96|128|256                   -> imagen cruda para la PS4: "MCT1"+w+h+pixeles 0x80RRGGBB
  GET /download/<id>                         -> el .mcpack/.mcaddon/.mcworld (los .zip se convierten solos)
  GET /  y  PUT /up?name=x.mcpack            -> pagina para subir packs a files/ desde el navegador (telefono)
  GET /categories                            -> categorias de CurseForge (para filtrar)

Fuentes (combinables): carpeta files/, catalog.json y CurseForge (export CF_API_KEY=clave,
gratis en https://console.curseforge.com). Para las miniaturas:  pip install pillow
Uso:  python3 server.py [puerto]   (por defecto 8080)
"""
import html
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image
except ImportError:  # sin Pillow no hay miniaturas, todo lo demas funciona
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.environ.get("MCSTORE_FILES", os.path.join(HERE, "files"))
CATALOG = os.environ.get("MCSTORE_CATALOG", os.path.join(HERE, "catalog.json"))
CACHE_DIR = os.environ.get("MCSTORE_CACHE", os.path.join(HERE, "cache"))
CF_KEY = os.environ.get("CF_API_KEY", "")
CF_BASE = os.environ.get("CF_API_BASE", "https://api.curseforge.com")
CF_GAME_ID = int(os.environ.get("CF_GAME_ID", "78022"))  # Minecraft Bedrock
PAGE_SIZE = 24
EXTS = (".mcpack", ".mcaddon", ".mcworld", ".mctemplate", ".zip")
PACK_EXTS = (".mcpack", ".mcaddon", ".mcworld", ".mctemplate")
SORTS = {"popular": (2, "desc"), "downloads": (6, "desc"), "updated": (3, "desc"),
         "new": (11, "desc"), "name": (4, "asc")}
UA = {"User-Agent": "McStore/1.1"}

POOL = ThreadPoolExecutor(max_workers=8)
_lock = threading.Lock()
MODS = {}       # id -> datos del mod (de CurseForge)
LOCALS = {}     # id -> ("file", ruta) | ("url", enlace)
_cache = {}     # clave -> (hora, valor)
_inflight = {}  # clave -> Event (evita trabajos repetidos)


def fold(s):
    """Texto a ASCII (la fuente de la app solo dibuja 32..126)."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if 32 <= ord(c) < 127 or c in "\n")


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def once(key, fn):
    """Si varios hilos piden lo mismo a la vez, solo uno trabaja; los demas esperan."""
    with _lock:
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:
        ev.wait(60)
        return None
    try:
        return fn()
    finally:
        with _lock:
            _inflight.pop(key, None)
        ev.set()


# ---------------------------------------------------------------- CurseForge

def cf_json(path, params=None):
    url = CF_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=dict(UA, **{"x-api-key": CF_KEY, "Accept": "application/json"}))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def best_file(m):
    """Archivo mas nuevo que se pueda bajar fuera de CurseForge (si el autor lo bloquea, no hay)."""
    files = [f for f in (m.get("latestFiles") or []) if f.get("downloadUrl")]
    if not files:
        return None
    ok = [f for f in files if f.get("fileName", "").lower().endswith(EXTS)] or files
    return max(ok, key=lambda f: f.get("fileDate", ""))


def cf_item(m):
    f = best_file(m)
    if not f:
        return None
    MODS[m["id"]] = m
    name = f["fileName"]
    if name.lower().endswith(".zip"):
        name = name[:-4] + ".mcpack"
    authors = m.get("authors") or [{}]
    return {"id": "C%d" % m["id"], "name": fold(m["name"]), "summary": fold(m.get("summary", ""))[:190],
            "file": fold(name), "size": f.get("fileLength", 0), "author": fold(authors[0].get("name", "")),
            "dl": int(m.get("downloadCount", 0)), "upd": (m.get("dateModified") or "")[:10],
            "th": 1 if (m.get("logo") or {}).get("thumbnailUrl") else 0}


def cf_categories():
    def go():
        try:
            d = cf_json("/v1/categories", {"gameId": CF_GAME_ID, "classesOnly": "true"})["data"]
            return [{"id": str(c["id"]), "name": fold(c["name"])} for c in d]
        except Exception as e:
            print("CurseForge categorias:", e)
            return []
    return cached("cats", 86400, go) if CF_KEY else []


def cf_browse(q, page, sort, cat):
    if not CF_KEY:
        return [], 0
    field, order = SORTS.get(sort, SORTS["popular"])
    p = {"gameId": CF_GAME_ID, "pageSize": PAGE_SIZE, "index": (page - 1) * PAGE_SIZE,
         "sortField": field, "sortOrder": order}
    if q:
        p["searchFilter"] = q
    if cat:
        p["classId"] = cat

    def go():
        try:
            r = cf_json("/v1/mods/search", p)
        except Exception as e:
            print("CurseForge error:", e)
            return None
        return r

    r = cached("s:" + json.dumps(p, sort_keys=True), 600, go)
    if not r:
        return [], 0
    items = [i for i in (cf_item(m) for m in r["data"]) if i]
    for i in items:  # las miniaturas se preparan mientras la app dibuja la lista
        if i["th"]:
            POOL.submit(get_thumb, i["id"], 128)
    return items, int(r.get("pagination", {}).get("totalCount", len(items)))


# ---------------------------------------------------------------- fuentes locales

def local_items(q):
    out = []
    if os.path.isdir(FILES_DIR):
        for i, fname in enumerate(sorted(os.listdir(FILES_DIR))):
            path = os.path.join(FILES_DIR, fname)
            name = os.path.splitext(fname)[0]
            if not fname.lower().endswith(EXTS) or not os.path.isfile(path) or (q and q not in name.lower()):
                continue
            mid = "F%d" % i
            LOCALS[mid] = ("file", path)
            out.append({"id": mid, "name": fold(name), "summary": "Archivo local de la carpeta files/",
                        "file": fold(fname), "size": os.path.getsize(path), "author": "", "dl": 0, "upd": "",
                        "th": 1 if Image else 0})
    try:
        with open(CATALOG, encoding="utf-8") as f:
            cat = json.load(f)
    except (OSError, ValueError):
        cat = []
    for i, m in enumerate(cat):
        if not isinstance(m, dict) or not m.get("url"):
            continue
        if q and q not in (m.get("name", "") + " " + m.get("summary", "")).lower():
            continue
        mid = "L%d" % i
        LOCALS[mid] = ("url", m["url"])
        fname = m.get("file") or os.path.basename(urllib.parse.urlparse(m["url"]).path) or "descarga.mcpack"
        out.append({"id": mid, "name": fold(m.get("name", fname)), "summary": fold(m.get("summary", "")),
                    "file": fold(fname), "size": m.get("size", 0), "author": "", "dl": 0, "upd": "", "th": 0})
    return out


def browse(q, page, sort, cat):
    q = q.strip().lower()
    items, total = cf_browse(q, page, sort, cat)
    if page == 1 and not cat:
        items = local_items(q) + items
    pages = max(1, min(math.ceil(total / PAGE_SIZE), 400)) if total else 1
    return {"page": page, "pages": pages, "total": max(total, len(items)), "items": items}


# ---------------------------------------------------------------- miniaturas

def _pack(img, s):
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (30, 38, 34, 255))
    bg.alpha_composite(img)
    img = bg.convert("RGB")
    w, h = img.size
    side = min(w, h)  # recorte cuadrado centrado
    img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if side != s:
        img = img.resize((s, s), Image.LANCZOS)
    r, g, b = img.split()
    px = Image.merge("RGBA", (b, g, r, Image.new("L", (s, s), 0x80))).tobytes()  # 0x80RRGGBB little endian
    return b"MCT1" + struct.pack("<HH", s, s) + px


def _thumb_source(mid):
    """Bytes de la imagen original, o None."""
    if mid.startswith("C"):
        m = MODS.get(int(mid[1:]))
        if m is None:
            m = load_mod(int(mid[1:]))
        url = ((m or {}).get("logo") or {}).get("thumbnailUrl")
        if not url:
            return None
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            return r.read(4_000_000)
    kv = LOCALS.get(mid)
    if kv and kv[0] == "file":  # el icono sale de dentro del propio .mcpack
        try:
            with zipfile.ZipFile(kv[1]) as z:
                for n in z.namelist():
                    if n.lower().endswith("pack_icon.png"):
                        return z.read(n)
        except Exception:
            pass
    return None


def get_thumb(mid, s):
    if Image is None or s not in (96, 128, 256):
        return None
    path = os.path.join(CACHE_DIR, "thumb", "%s_%d.bin" % (mid, s))
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    def work():
        try:
            src = _thumb_source(mid)
            if not src:
                return
            data = _pack(Image.open(io.BytesIO(src)), s)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        except Exception as e:
            print("miniatura", mid, "fallo:", e)

    once("t:%s:%d" % (mid, s), work)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


# ---------------------------------------------------------------- detalle

def load_mod(num):
    try:
        m = cf_json("/v1/mods/%d" % num)["data"]
        MODS[num] = m
        return m
    except Exception as e:
        print("mod", num, "fallo:", e)
        return None


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</tr>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "\n- ", h)
    h = re.sub(r"(?s)<[^>]+>", "", h)
    t = fold(html.unescape(h)).replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def detail(mid):
    if mid.startswith("C"):
        num = int(mid[1:])
        m = MODS.get(num) or load_mod(num)
        if not m:
            return None
        it = cf_item(m) or {"id": mid, "name": fold(m["name"]), "file": "", "size": 0, "author": "", "dl": 0, "upd": "", "th": 0, "summary": ""}

        def go():
            try:
                return html_to_text(cf_json("/v1/mods/%d/description" % num)["data"])
            except Exception as e:
                print("descripcion:", e)
                return ""
        desc = cached("d:%d" % num, 3600, go) or it["summary"]
        f = best_file(m) or {}
        it = dict(it, desc=desc[:1800], ver=fold(", ".join((f.get("gameVersions") or [])[:3])))
        POOL.submit(prepare_download, mid)  # el archivo se baja mientras lees la descripcion
        POOL.submit(get_thumb, mid, 256)
        return it
    for it in local_items(""):
        if it["id"] == mid:
            POOL.submit(get_thumb, mid, 256)
            return dict(it, desc=it["summary"], ver="")
    return None


# ---------------------------------------------------------------- descarga automatica

def normalize(path, fname):
    """Devuelve (ruta, nombre) listos para Minecraft. Los .zip se convierten a .mcpack/.mcaddon."""
    stem, ext = os.path.splitext(fname)
    if ext.lower() != ".zip":
        return path, fname
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            inner = [n for n in names if n.lower().endswith(PACK_EXTS)]
            if len(inner) == 1:  # un pack dentro del zip: se saca tal cual
                out = path + ".inner"
                with z.open(inner[0]) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                return out, os.path.basename(inner[0])
            mans = [n for n in names if n.lower().endswith("manifest.json")]
            if len(mans) >= 2:
                return path, stem + ".mcaddon"
            if len(mans) == 1 and "/" in mans[0]:  # manifest dentro de una carpeta: se re-empaqueta sin ella
                pre = mans[0][:-len("manifest.json")]
                out = path + ".repack"
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
                    for n in names:
                        if n.startswith(pre):
                            zo.writestr(n[len(pre):], z.read(n))
                return out, stem + ".mcpack"
    except zipfile.BadZipFile:
        pass
    return path, stem + ".mcpack"


def prepare_download(mid):
    """Baja el archivo a la cache (una sola vez). Devuelve (ruta, nombre) o None."""
    kv = None
    if mid.startswith("C"):
        num = int(mid[1:])
        m = MODS.get(num) or load_mod(num)
        f = best_file(m) if m else None
        if f:
            kv = ("url", f["downloadUrl"], f["fileName"])
    else:
        v = LOCALS.get(mid)
        if not v:
            local_items("")
            v = LOCALS.get(mid)
        if v:
            kv = (v[0], v[1], os.path.basename(urllib.parse.urlparse(v[1]).path) if v[0] == "url" else os.path.basename(v[1]))
    if not kv:
        return None
    kind, src, fname = kv
    if kind == "file":
        return normalize(src, fname) if fname.lower().endswith(".zip") else (src, fname)
    raw = os.path.join(CACHE_DIR, "dl", mid + ".bin")
    meta = raw + ".json"
    if not (os.path.exists(raw) and os.path.exists(meta)):
        def work():
            os.makedirs(os.path.dirname(raw), exist_ok=True)
            tmp = raw + ".tmp"
            with urllib.request.urlopen(urllib.request.Request(src, headers=UA), timeout=30) as r, open(tmp, "wb") as o:
                shutil.copyfileobj(r, o, 262144)
            path, name = normalize(tmp, fname)
            os.replace(path, raw)
            if path != tmp and os.path.exists(tmp):
                os.remove(tmp)
            with open(meta, "w") as f:
                json.dump({"name": name}, f)
        try:
            once("dl:" + mid, work)
        except Exception as e:
            print("descarga fallida:", e)
            return None
        if not (os.path.exists(raw) and os.path.exists(meta)):
            return None
    with open(meta) as f:
        return raw, json.load(f)["name"]


# ---------------------------------------------------------------- HTTP

GH_REPO = os.environ.get("MCSTORE_REPO", "Y4ll/Store-Mods-Ps4")  # repo donde se publica (modo creador)
GH_API = os.environ.get("GH_API_BASE", "https://api.github.com")
TOKEN_FILE = os.path.join(HERE, "github_token.txt")


def gh_token():
    """Token de GitHub del dueno (variable GITHUB_TOKEN o archivo github_token.txt junto a server.py)."""
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if not t and os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
    return t


def gh_publish(path, name):
    """Sube el archivo a addons/ del repo de GitHub: 'Build catalog' lo agrega solo al catalogo. Devuelve (ok, texto)."""
    tok = gh_token()
    if not tok:
        return False, "falta el token de GitHub (Modo creador)"
    url = "%s/repos/%s/contents/addons/%s" % (GH_API, GH_REPO, urllib.parse.quote(name))
    hdr = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "User-Agent": "mcstore-server"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            sha = json.loads(r.read().decode()).get("sha")
    except Exception:
        pass
    import base64
    with open(path, "rb") as f:
        body = {"message": "Pack propio: " + name, "branch": "main", "content": base64.b64encode(f.read()).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(hdr, **{"Content-Type": "application/json"}), method="PUT")
    try:
        urllib.request.urlopen(req, timeout=300).read()
        return True, "publicado"
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except Exception:
            msg = ""
        return False, "GitHub respondio %d: %s" % (e.code, msg)
    except Exception as e:
        return False, "sin conexion con GitHub: %s" % e


UPLOAD_PAGE = "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>McStore</title><style>\nbody{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#16181d;color:#eee;margin:0;padding:22px;max-width:640px;margin:auto}\nh1{color:#6fd45a;margin:.2em 0}p{color:#aab}\nlabel.b,button{display:block;width:100%;box-sizing:border-box;padding:16px;margin:12px 0;border:0;border-radius:12px;font-size:18px;text-align:center;background:#6fd45a;color:#10200a;font-weight:700}\nbutton[disabled]{opacity:.4}input[type=file]{display:none}\ninput[type=password]{width:100%;box-sizing:border-box;padding:14px;margin:6px 0;border-radius:10px;border:1px solid #3a3f4b;background:#20232b;color:#eee;font-size:16px}\n.f{background:#262a33;border-radius:10px;padding:10px 12px;margin:8px 0;word-break:break-all}\n.bar{height:8px;background:#3a3f4b;border-radius:4px;margin-top:6px}.bar i{display:block;height:8px;background:#6fd45a;border-radius:4px;width:0}\n.ok{color:#6fd45a}.er{color:#ff7a6b}.pub{background:#262a33;border-radius:10px;padding:12px;margin:10px 0}summary{color:#6fd45a;font-weight:700;margin:14px 0}\n</style></head><body>\n<h1>McStore</h1><p>Sube tus .mcpack o .mcaddon a la PS4. / Upload your .mcpack or .mcaddon to the PS4.</p>\n<label class=b for=f>1. Elegir archivos / Choose files</label><input id=f type=file multiple>\n<div id=l></div>\n<div class=pub id=own style='display:none'><label><input type=checkbox id=pub checked style='display:inline;width:auto'> Publicar tambien para TODOS (catalogo de McStore)</label></div>\n<button id=go disabled>2. Subir / Upload</button><p id=t></p>\n<details id=cr><summary>Modo creador (solo para el dueno)</summary>\n<p>Pega tu token de GitHub (Contents: Read and write) para poder publicar para todos. Se guarda solo en la PS4.</p>\n<input id=tk type=password placeholder='github_pat_...' autocapitalize=off autocorrect=off><button id=stk type=button>Guardar token</button></details>\n<script>\nvar f=document.getElementById('f'),l=document.getElementById('l'),go=document.getElementById('go'),t=document.getElementById('t'),own=document.getElementById('own'),pub=document.getElementById('pub');\nfetch('/info').then(function(r){return r.text()}).then(function(x){if(x=='1')own.style.display='block'}).catch(function(){});\ndocument.getElementById('stk').onclick=function(){var v=document.getElementById('tk').value.trim();var x=new XMLHttpRequest();x.open('PUT','/token');\nx.onload=function(){if(x.status==200){own.style.display='block';t.innerHTML='<b class=ok>Token guardado.</b>';document.getElementById('tk').value=''}else t.innerHTML='<span class=er>Token no valido</span>'};x.send(v)};\nf.onchange=function(){l.innerHTML='';for(var i=0;i<f.files.length;i++){var d=document.createElement('div');d.className='f';\nd.innerHTML=f.files[i].name+' ('+Math.round(f.files[i].size/1024)+' KB)<div class=bar><i></i></div>';l.appendChild(d)}go.disabled=!f.files.length};\nfunction up(i){if(i>=f.files.length){t.innerHTML='<b class=ok>Listo. En la PS4 pulsa R2 para instalar. / Done: press R2 on the PS4.</b>';go.disabled=false;return}\nvar d=l.children[i],bar=d.querySelector('i'),x=new XMLHttpRequest();\nx.open('PUT','/up?name='+encodeURIComponent(f.files[i].name)+(own.style.display=='block'&&pub.checked?'&pub=1':''));\nx.upload.onprogress=function(e){if(e.lengthComputable)bar.style.width=(100*e.loaded/e.total)+'%'};\nx.onload=function(){if(x.status==200){bar.style.width='100%';d.className='f ok';if(x.responseText.indexOf('publicado')>0)d.innerHTML+='<br>Publicado para todos'}else{d.className='f er';d.innerHTML+='<br>'+x.responseText}up(i+1)};\nx.onerror=function(){d.className='f er';d.innerHTML+='<br>Error de red / network error';up(i+1)};x.send(f.files[i])}\ngo.onclick=function(){go.disabled=true;t.textContent='';up(0)};\n</script></body></html>\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # sin chunked: mas facil para la PS4

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode(), "application/json")

    def do_PUT(self):
        """Subir un pack desde el navegador del telefono: PUT /up?name=archivo.mcpack -> files/."""
        u = urllib.parse.urlparse(self.path)
        if u.path == "/token":
            tk = re.sub(r"[^A-Za-z0-9_-]", "", self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 300)).decode("latin1"))
            ok = len(tk) >= 20
            if ok:
                with open(TOKEN_FILE, "w") as f:
                    f.write(tk + "\n")
            return self._send(b"ok" if ok else b"token no valido", "text/plain", 200 if ok else 400)
        qs0 = urllib.parse.parse_qs(u.query)
        name = os.path.basename(qs0.get("name", [""])[0])
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
        size = int(self.headers.get("Content-Length") or 0)
        if u.path != "/up" or not name.lower().endswith((".mcpack", ".mcaddon", ".mcworld")) or size <= 0:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        os.makedirs(FILES_DIR, exist_ok=True)
        part = os.path.join(FILES_DIR, name + ".part")
        left = size
        with open(part, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(262144, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(part)
            return
        os.replace(part, os.path.join(FILES_DIR, name))
        print("Recibido:", name)
        if qs0.get("pub", [""])[0] == "1":
            ok, msg = gh_publish(os.path.join(FILES_DIR, name), name)
            print("Publicar", name, "->", msg)
            if not ok:
                return self._send(("Guardado en el PC pero NO publicado: " + msg).encode(), "text/plain", 502)
            return self._send(b"ok publicado", "text/plain")
        self._send(b"ok", "text/plain")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/info":
                return self._send(b"1" if gh_token() else b"0", "text/plain")
            if u.path in ("/", "/index.html"):
                return self._send(UPLOAD_PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/browse":
                page = max(1, int(qs.get("page", "1") or 1))
                return self._json(browse(qs.get("q", ""), page, qs.get("sort", "popular"), qs.get("cat", "")))
            if u.path == "/search":
                return self._json(browse(qs.get("q", ""), 1, "popular", "")["items"])
            if u.path == "/categories":
                return self._json(cf_categories())
            if u.path.startswith("/detail/"):
                d = detail(u.path.rsplit("/", 1)[1])
                return self._json(d) if d else self.send_error(404, "id desconocido")
            if u.path.startswith("/thumb/"):
                data = get_thumb(u.path.rsplit("/", 1)[1], int(qs.get("s", "128")))
                return self._send(data, "application/octet-stream") if data else self.send_error(404, "sin imagen")
            if u.path.startswith("/download/"):
                res = prepare_download(u.path.rsplit("/", 1)[1])
                if not res:
                    return self.send_error(502, "no se pudo obtener el archivo")
                path, name = res
                size = os.path.getsize(path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % fold(name))
                self.end_headers()
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 262144)
                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            print("error en", self.path, ":", e)
            return self.send_error(500, "error interno")
        self.send_error(404)

    def log_message(self, fmt, *args):
        print("%s %s" % (self.client_address[0], fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    os.makedirs(FILES_DIR, exist_ok=True)
    print("McStore server en el puerto %d | files/: %s | CurseForge: %s | miniaturas: %s"
          % (port, FILES_DIR, "ON" if CF_KEY else "OFF (falta CF_API_KEY)",
             "ON" if Image else "OFF (pip install pillow)"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

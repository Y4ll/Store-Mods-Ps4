#!/usr/bin/env python3
"""Genera catalog.json (solo .mcpack de Minecraft Bedrock en CurseForge)
con el mismo formato que usa la app McStore:

[ {"name": ..., "summary": ..., "file": ..., "url": ..., "size": ...,
   "ver": "1.21.50", "vers": "1.21.50,1.21.40", "cls": "tex", "img": "cf123456",
   "author": "...", "dl": 12345, "upd": "2026-10-01", "tags": "shaders,ui"}, ... ]
  author/dl/upd = autor, descargas totales y fecha del archivo (para ordenar en la app).
  tags = categorias de CurseForge (slug, separadas por coma) para el filtro de categoria.
  ver/vers = versiones de Minecraft del archivo (la mas nueva / todas); cls = tipo (tex, add, map, skin...).
  img = nombre de la miniatura: thumbs/<img>.t96 (96x96 pixeles RGB565 sin cabecera = 18432 bytes), sacada del logo
  del mod en CurseForge. Necesita Pillow (pip install pillow); si no esta, el catalogo sale sin imagenes.

Uso:
    export CF_API_KEY='tu_clave'
    python3 build_catalog.py          # revisa hasta 1000 addons
    python3 build_catalog.py 5000     # o el maximo que quieras (tope 10000)
"""
import datetime
import hashlib
import io
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import html as htmllib
import unicodedata
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("CF_API_BASE", "https://api.curseforge.com") + "/v1"
GAME_ID = 78022  # Minecraft Bedrock
KEY = os.environ.get("CF_API_KEY", "")
FETCH_SIZE = 50
OUT = "catalog.json"
GOOD_EXT = (".mcpack", ".mcaddon")  # la app instala los dos (el .mcaddon trae varios packs)
THUMBS = "thumbs"
DESCS = "desc"
ADDONS = "addons"  # tus propios .mcpack / .mcaddon: sube los archivos aqui y se agregan solos al catalogo
SHOTS = "shots"
SHOT_W, SHOT_H = 160, 90
SHOT_TOP = int(os.environ.get("CF_SHOTS", "300"))  # solo los N mas descargados llevan capturas (pesan 28 KB cada una)
THUMB = 96
BG = (24, 24, 28)


def to_t96(data):
    """Imagen (png/jpg/webp...) -> 96x96 RGB565 little-endian, sin cabecera."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    back = Image.new("RGBA", im.size, BG + (255,))
    back.alpha_composite(im)
    im = back.convert("RGB")
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((THUMB, THUMB), Image.LANCZOS)
    raw = im.tobytes()
    out = bytearray()
    for i in range(0, len(raw), 3):
        out += struct.pack("<H", ((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3))
    return bytes(out)


def to_t160(data):
    """Captura (png/jpg/webp...) -> 160x90 RGB565 little-endian, sin cabecera (recorte 16:9 centrado)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    back = Image.new("RGBA", im.size, BG + (255,))
    back.alpha_composite(im)
    im = back.convert("RGB")
    w, h = im.size
    cw, ch = (w, w * SHOT_H // SHOT_W) if w * SHOT_H <= h * SHOT_W else (h * SHOT_W // SHOT_H, h)
    im = im.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch)).resize((SHOT_W, SHOT_H), Image.LANCZOS)
    raw = im.tobytes()
    out = bytearray()
    for i in range(0, len(raw), 3):
        out += struct.pack("<H", ((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3))
    return bytes(out)


def make_shot(key, url):
    """Baja una captura y guarda shots/<key>.t96. Devuelve True si el archivo existe al final."""
    path = os.path.join(SHOTS, key + ".t96")
    if os.path.exists(path) and os.path.getsize(path) == SHOT_W * SHOT_H * 2:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mcstore-catalog"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(to_t160(data))
        return True
    except Exception as e:
        print("  sin captura %s: %s" % (key, e))
        return False


def make_thumb(name, url):
    """Baja el logo y guarda thumbs/<name>.t96. Devuelve True si el archivo existe al final."""
    path = os.path.join(THUMBS, name + ".t96")
    if os.path.exists(path) and os.path.getsize(path) == THUMB * THUMB * 2:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mcstore-catalog"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(to_t96(data))
        return True
    except Exception as e:
        print("  sin miniatura para %s: %s" % (name, e))
        return False


class _Text(HTMLParser):
    BLOCK = ("p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(h, limit=4000):
    """HTML de la descripcion de CurseForge -> texto plano ASCII (la fuente de la app no tiene acentos)."""
    p = _Text()
    p.feed(h or "")
    t = unicodedata.normalize("NFKD", "".join(p.out)).encode("ascii", "ignore").decode()
    out, blank = [], False
    for line in t.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()[:limit]


def cf_soft(path):
    """Como cf() pero sin cortar todo el programa si falla (para llamadas en hilos)."""
    url = API + path
    req = urllib.request.Request(url, headers={"x-api-key": KEY, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1 + attempt)
    return {}


def _manifest(z):
    """Manifest mas cercano a la raiz del zip (dict) o {}."""
    names = sorted([n for n in z.namelist() if n.lower().endswith("manifest.json")], key=lambda n: n.count("/"))
    for n in names:
        raw = z.read(n).decode("utf-8-sig", "ignore")
        for txt in (raw, re.sub(r"//[^\n]*|/\*.*?\*/", "", raw, flags=re.S), re.sub(r",\s*([}\]])", r"\1", re.sub(r"//[^\n]*|/\*.*?\*/", "", raw, flags=re.S))):
            try:
                return json.loads(txt)
            except Exception:
                pass
    return {}


def _icon(z):
    names = z.namelist()
    for n in sorted(names, key=lambda n: n.count("/")):
        if os.path.basename(n).lower() in ("pack_icon.png", "pack_icon.jpg", "pack_icon.jpeg"):
            return z.read(n)
    return None


def local_entries(old):
    """Entradas del catalogo para los packs propios de addons/ (nombre, descripcion e icono salen del manifest)."""
    out = []
    if not os.path.isdir(ADDONS):
        return out
    today = datetime.date.today().isoformat()
    for fn in sorted(os.listdir(ADDONS), key=str.lower):
        if not fn.lower().endswith(GOOD_EXT):
            continue
        path = os.path.join(ADDONS, fn)
        try:
            z = zipfile.ZipFile(path)
            man = _manifest(z)
            icon = _icon(z)
        except Exception as e:
            print("  %s: no se pudo leer (%s)" % (fn, e))
            continue
        hdr = man.get("header", {}) if isinstance(man, dict) else {}
        name = str(hdr.get("name") or "")
        if not name or name.startswith(("pack.", "§")) or "%" in name:
            name = os.path.splitext(fn)[0].replace("_", " ")
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().strip() or fn
        desc = unicodedata.normalize("NFKD", str(hdr.get("description") or "")).encode("ascii", "ignore").decode().strip()
        mods = [str(m.get("type", "")).lower() for m in man.get("modules", []) if isinstance(m, dict)] if isinstance(man, dict) else []
        cls = "tex" if "resources" in mods else ("add" if mods else class_code(""))
        ver = ""
        mev = hdr.get("min_engine_version")
        if isinstance(mev, list) and len(mev) >= 2:
            ver = ".".join(str(x) for x in mev[:3])
        authors = (man.get("metadata", {}) or {}).get("authors") if isinstance(man, dict) else None
        key = "loc_" + hashlib.md5(fn.encode("utf-8")).hexdigest()[:12]
        e = {
            "name": name[:90],
            "summary": (desc or "Pack propio de McStore")[:200],
            "file": fn[:90],
            "url": "%s/%s" % (ADDONS, urllib.parse.quote(fn)),
            "size": os.path.getsize(path),
            "ver": ver, "vers": ver, "cls": cls,
            "author": (authors[0] if isinstance(authors, list) and authors else "McStore")[:39],
            "dl": 0,
            "upd": (old.get(fn) or {}).get("upd") or today,
            "tags": "",
            "img": "",
        }
        if icon:
            try:
                os.makedirs(THUMBS, exist_ok=True)
                with open(os.path.join(THUMBS, key + ".t96"), "wb") as f:
                    f.write(to_t96(icon))
                e["img"] = key
            except Exception as ex:
                print("  sin icono para %s: %s" % (fn, ex))
        if desc and e["img"]:
            os.makedirs(DESCS, exist_ok=True)
            with open(os.path.join(DESCS, key + ".txt"), "w", encoding="utf-8") as f:
                f.write(desc[:4000])
        out.append(e)
    print("Packs propios en %s/: %d" % (ADDONS, len(out)))
    return out


def make_desc(name):
    """Guarda desc/<name>.txt con la descripcion completa. True si el archivo existe al final."""
    path = os.path.join(DESCS, name + ".txt")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    if not name.startswith("cf"):  # packs propios: la descripcion ya se guardo (o no tienen)
        return False
    text = html_to_text((cf_soft("/mods/%s/description" % name[2:]).get("data") or ""))
    if not text:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def cf(path, params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": KEY, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("  reintento (%s)" % e)
            time.sleep(2 * (attempt + 1))
    raise SystemExit("No se pudo conectar con CurseForge.")


def forge_url(f):
    if f.get("downloadUrl"):
        return f["downloadUrl"]
    fid = f["id"]
    return "https://edge.forgecdn.net/files/%d/%d/%s" % (
        fid // 1000, fid % 1000, urllib.parse.quote(f["fileName"]))


def unique_name(mod_id, file_name):
    """Nombre de archivo unico (id del mod + nombre) para que dos addons distintos
    con el mismo nombre de archivo no se pisen al descargar ni se confundan al comparar."""
    base, ext = os.path.splitext(file_name)
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    name = "%d_%s" % (mod_id, base)
    return name[:88 - len(ext)] + ext


def versions_of(f):
    """Versiones de Minecraft (x.y o x.y.z) del archivo, de la mas nueva a la mas vieja."""
    found = set()
    names = list(f.get("gameVersions") or [])
    for sv in f.get("sortableGameVersions") or []:
        names.append(sv.get("gameVersionName") or "")
    for n in names:
        if re.fullmatch(r"\d+\.\d+(\.\d+)*", str(n).strip()):
            found.add(str(n).strip())
    return sorted(found, key=lambda v: [int(x) for x in v.split(".")], reverse=True)


def class_code(name):
    """Nombre de la clase de CurseForge -> codigo corto que entiende la app."""
    n = (name or "").lower()
    if "textur" in n or "resource" in n:
        return "tex"
    if "addon" in n or "add-on" in n or "behavior" in n:
        return "add"
    if "map" in n or "world" in n:
        return "map"
    if "skin" in n:
        return "skin"
    return re.sub(r"[^a-z]", "", n)[:7]


def pick_file(mod):
    if mod.get("allowModDistribution") is False:
        return None
    files = [f for f in (mod.get("latestFiles") or []) if f.get("fileName")]
    good = [f for f in files if f["fileName"].lower().endswith(GOOD_EXT)]
    if not good:
        return None
    # el mas reciente (por fecha); si CurseForge no la trae, el ultimo de la lista
    return max(enumerate(good), key=lambda t: ((t[1].get("fileDate") or ""), t[0]))[1]


def tags_of(mod, limit=62):
    """Categorias del mod (slug), la principal primero, sin pasar de `limit` caracteres."""
    cats = [c for c in (mod.get("categories") or []) if not c.get("isClass") and c.get("slug")]
    prim = mod.get("primaryCategoryId")
    cats.sort(key=lambda c: 0 if c.get("id") == prim else 1)
    out = []
    for c in cats:
        slug = re.sub(r"[^a-z0-9-]", "", str(c["slug"]).lower())[:30]
        if slug and slug not in out and len(",".join(out + [slug])) <= limit:
            out.append(slug)
    return ",".join(out)


def main():
    limit = min(int(sys.argv[1]) if len(sys.argv) > 1 else 1000, 10000)
    if not KEY:
        print("Sin CF_API_KEY: solo se usan tus packs de %s/." % ADDONS)
        limit = 0
    old = {}
    if os.path.exists(OUT):
        try:
            for e in json.load(open(OUT, encoding="utf-8")):
                old[e.get("file", "")] = e
        except Exception:
            pass
    classes = {}
    try:
        if not KEY:
            raise SystemExit

        for c in cf("/categories", {"gameId": GAME_ID, "classesOnly": "true"}).get("data", []):
            classes[c["id"]] = c.get("name", "")
        print("Clases:", ", ".join("%s=%s" % (k, v) for k, v in classes.items()))
    except SystemExit:
        print("No se pudieron leer las clases: el filtro de tipo usara el nombre del pack.")
    items = local_entries(old)
    local_imgs = {e["img"] for e in items if e["img"]}
    logos = {}  # nombre de miniatura -> url del logo
    shot_urls = {}  # nombre -> hasta 3 urls de capturas
    index = 0
    while index < limit:
        size = min(FETCH_SIZE, limit - index)
        data = cf("/mods/search", {
            "gameId": GAME_ID, "pageSize": size, "index": index,
            "sortField": 2, "sortOrder": "desc",
        })
        mods = data.get("data", [])
        if not mods:
            break
        for m in mods:
            f = pick_file(m)
            if not f:
                continue
            summary = (m.get("summary") or "").replace("\n", " ").strip()
            vs = versions_of(f)
            logo = (m.get("logo") or {}).get("thumbnailUrl") or (m.get("logo") or {}).get("url") or ""
            img = ("cf%d" % m["id"]) if logo else ""
            if img:
                logos[img] = logo
                su = [(x.get("thumbnailUrl") or x.get("url") or "") for x in (m.get("screenshots") or [])]
                shot_urls[img] = [u for u in su if u][:3]
            items.append({
                "name": m["name"],
                "summary": summary[:200],
                "file": unique_name(m["id"], f["fileName"]),
                "url": forge_url(f),
                "size": f.get("fileLength", 0),
                "ver": vs[0] if vs else "",
                "vers": ",".join(vs)[:46],
                "cls": class_code(classes.get(m.get("classId"), "")),
                "img": img,
                "author": ((m.get("authors") or [{}])[0].get("name") or "")[:39],
                "dl": int(m.get("downloadCount") or 0),
                "upd": (f.get("fileDate") or m.get("dateModified") or "")[:10],
                "tags": tags_of(m),
            })
        index += len(mods)
        total = data.get("pagination", {}).get("totalCount", 0)
        print("Revisados %d | mcpack guardados %d | total en CF %d" % (index, len(items), total))
        if total and index >= total:
            break
        time.sleep(0.5)

    if not items:
        raise SystemExit("No se encontro ningun .mcpack; no se modifica %s" % OUT)
    try:
        import PIL  # noqa: F401
        os.makedirs(THUMBS, exist_ok=True)
        todo = [n for n in logos if not os.path.exists(os.path.join(THUMBS, n + ".t96"))]
        print("Miniaturas: %d nuevas por bajar (de %d)" % (len(todo), len(logos)))
        with ThreadPoolExecutor(8) as ex:
            ok = dict(zip(logos, ex.map(lambda n: make_thumb(n, logos[n]), list(logos))))
        for it in items:
            if it["img"] and it["img"] not in local_imgs and not ok.get(it["img"]):
                it["img"] = ""
        used = {it["img"] + ".t96" for it in items if it["img"]}
        for fn in os.listdir(THUMBS):  # quita las que ya no se usan
            if fn.endswith(".t96") and fn not in used:
                os.remove(os.path.join(THUMBS, fn))
    except ImportError:
        print("Pillow no esta instalado: el catalogo sale sin miniaturas (pip install pillow).")
        for it in items:
            it["img"] = ""
    # descripcion completa (texto) de cada addon con id de CurseForge: desc/<img>.txt
    os.makedirs(DESCS, exist_ok=True)
    names = [it["img"] for it in items if it["img"]]
    names = [n for n in names if n not in local_imgs] + [n for n in names if n in local_imgs]
    todo = [n for n in names if not os.path.exists(os.path.join(DESCS, n + ".txt"))]
    print("Descripciones: %d nuevas por bajar (de %d)" % (len(todo), len(names)))
    with ThreadPoolExecutor(6) as ex:
        okd = dict(zip(names, ex.map(make_desc, names)))
    for it in items:
        if it["img"] and (okd.get(it["img"]) or (it["img"] in local_imgs and os.path.exists(os.path.join(DESCS, it["img"] + ".txt")))):
            it["desc"] = 1
    usedd = {n + ".txt" for n in names}
    for fn in os.listdir(DESCS):
        if fn.endswith(".txt") and fn not in usedd:
            os.remove(os.path.join(DESCS, fn))
    # capturas de pantalla (160x90) solo para los SHOT_TOP addons mas descargados
    try:
        import PIL  # noqa: F401
        os.makedirs(SHOTS, exist_ok=True)
        top = sorted([it for it in items if it["img"] and shot_urls.get(it["img"])], key=lambda it: -it["dl"])[:SHOT_TOP]
        jobs = [("%s_%d" % (it["img"], k + 1), u) for it in top for k, u in enumerate(shot_urls[it["img"]])]
        print("Capturas: %d (de %d addons)" % (len(jobs), len(top)))
        with ThreadPoolExecutor(8) as ex:
            oks = dict(zip([j[0] for j in jobs], ex.map(lambda j: make_shot(*j), jobs)))
        usedk = set()
        for it in top:
            n = 0
            for k in range(len(shot_urls[it["img"]])):
                key = "%s_%d" % (it["img"], k + 1)
                if oks.get(key) and n == k:  # solo capturas seguidas desde la 1
                    usedk.add(key + ".t96")
                    n += 1
            if n:
                it["shots"] = n
        for fn in os.listdir(SHOTS):
            if fn.endswith(".t96") and fn not in usedk:
                os.remove(os.path.join(SHOTS, fn))
    except ImportError:
        pass
    for it in items:
        if not it["img"]:
            del it["img"]
    # un addon por linea y sin espacios de sobra: el catalogo pesa menos y se lee mas rapido en la PS4
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("[\n" + ",\n".join(json.dumps(it, ensure_ascii=False, separators=(",", ":")) for it in items) + "\n]\n")
    exts = {}
    tagc = {}
    for it in items:
        e = os.path.splitext(it["file"])[1].lower()
        exts[e] = exts.get(e, 0) + 1
        for t in it["tags"].split(","):
            if t:
                tagc[t] = tagc.get(t, 0) + 1
    print("Tipos de archivo:", exts)
    print("Categorias:", sorted(tagc.items(), key=lambda kv: -kv[1])[:30])
    print("Tamano de %s: %d KB" % (OUT, os.path.getsize(OUT) // 1024))
    print("Listo: %d addons en %s" % (len(items), OUT))


if __name__ == "__main__":
    main()

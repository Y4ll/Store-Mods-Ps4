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
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("CF_API_BASE", "https://api.curseforge.com") + "/v1"
GAME_ID = 78022  # Minecraft Bedrock
KEY = os.environ.get("CF_API_KEY", "")
FETCH_SIZE = 50
OUT = "catalog.json"
GOOD_EXT = (".mcpack", ".mcaddon")  # la app instala los dos (el .mcaddon trae varios packs)
THUMBS = "thumbs"
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
    if not KEY:
        raise SystemExit("Falta CF_API_KEY.")
    limit = min(int(sys.argv[1]) if len(sys.argv) > 1 else 1000, 10000)
    classes = {}
    try:
        for c in cf("/categories", {"gameId": GAME_ID, "classesOnly": "true"}).get("data", []):
            classes[c["id"]] = c.get("name", "")
        print("Clases:", ", ".join("%s=%s" % (k, v) for k, v in classes.items()))
    except SystemExit:
        print("No se pudieron leer las clases: el filtro de tipo usara el nombre del pack.")
    items = []
    logos = {}  # nombre de miniatura -> url del logo
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
            if it["img"] and not ok.get(it["img"]):
                it["img"] = ""
        used = {it["img"] + ".t96" for it in items if it["img"]}
        for fn in os.listdir(THUMBS):  # quita las que ya no se usan
            if fn.endswith(".t96") and fn not in used:
                os.remove(os.path.join(THUMBS, fn))
    except ImportError:
        print("Pillow no esta instalado: el catalogo sale sin miniaturas (pip install pillow).")
        for it in items:
            it["img"] = ""
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

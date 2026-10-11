#!/usr/bin/env python3
"""Genera el catalogo de addons Bedrock de CurseForge para subir a GitHub.

Uso:
    export CF_API_KEY='tu_clave'
    python3 build_catalog.py            # hasta 2000 addons
    python3 build_catalog.py 5000       # o el maximo que quieras (tope 10000)

Crea la carpeta 'catalog/' con:
    index.json        -> total y lista de paginas
    page_0.json ...   -> 100 addons por pagina
Sube esa carpeta a GitHub y publicala con GitHub Pages.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.curseforge.com/v1"
GAME_ID = 78022  # Minecraft Bedrock
KEY = os.environ.get("CF_API_KEY", "")
FETCH_SIZE = 50
PAGE_SIZE = 100
OUT = "catalog"
GOOD_EXT = (".mcpack",)  # solo mcpack por ahora


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


def pick_file(mod):
    files = mod.get("latestFiles") or []
    # solo archivos cuyo autor permite distribucion de terceros
    files = [f for f in files if f.get("fileName")]
    if mod.get("allowModDistribution") is False:
        return None
    good = [f for f in files if f["fileName"].lower().endswith(GOOD_EXT)]
    return good[-1] if good else None


def main():
    if not KEY:
        raise SystemExit("Falta CF_API_KEY. Usa: export CF_API_KEY='tu_clave'")
    limit = min(int(sys.argv[1]) if len(sys.argv) > 1 else 2000, 10000)
    items = []
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
            items.append({
                "id": m["id"],
                "name": m["name"],
                "summary": m.get("summary", ""),
                "author": (m.get("authors") or [{}])[0].get("name", ""),
                "downloads": m.get("downloadCount", 0),
                "thumb": (m.get("logo") or {}).get("thumbnailUrl", ""),
                "fileName": f["fileName"],
                "size": f.get("fileLength", 0),
                "url": forge_url(f),
            })
        index += len(mods)
        total = data.get("pagination", {}).get("totalCount", 0)
        print("Revisados %d  |  guardados %d  |  total en CF %d" % (index, len(items), total))
        if total and index >= total:
            break
        time.sleep(0.5)

    os.makedirs(OUT, exist_ok=True)
    pages = 0
    for i in range(0, len(items), PAGE_SIZE):
        with open(os.path.join(OUT, "page_%d.json" % pages), "w") as fh:
            json.dump(items[i:i + PAGE_SIZE], fh, ensure_ascii=False)
        pages += 1
    with open(os.path.join(OUT, "index.json"), "w") as fh:
        json.dump({"total": len(items), "pageSize": PAGE_SIZE, "pages": pages,
                   "updated": time.strftime("%Y-%m-%d")}, fh)
    print("Listo: %d addons en %d paginas dentro de '%s/'" % (len(items), pages, OUT))


if __name__ == "__main__":
    main()

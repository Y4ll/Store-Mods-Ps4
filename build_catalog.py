#!/usr/bin/env python3
"""Genera catalog.json (solo .mcpack de Minecraft Bedrock en CurseForge)
con el mismo formato que usa la app McStore:

[ {"name": ..., "summary": ..., "file": ..., "url": ..., "size": ...}, ... ]

Uso:
    export CF_API_KEY='tu_clave'
    python3 build_catalog.py          # revisa hasta 1000 addons
    python3 build_catalog.py 5000     # o el maximo que quieras (tope 10000)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.curseforge.com/v1"
GAME_ID = 78022  # Minecraft Bedrock
KEY = os.environ.get("CF_API_KEY", "")
FETCH_SIZE = 50
OUT = "catalog.json"
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


def unique_name(mod_id, file_name):
    """Nombre de archivo unico (id del mod + nombre) para que dos addons distintos
    con el mismo nombre de archivo no se pisen al descargar ni se confundan al comparar."""
    base, ext = os.path.splitext(file_name)
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    name = "%d_%s" % (mod_id, base)
    return name[:88 - len(ext)] + ext


def pick_file(mod):
    if mod.get("allowModDistribution") is False:
        return None
    files = [f for f in (mod.get("latestFiles") or []) if f.get("fileName")]
    good = [f for f in files if f["fileName"].lower().endswith(GOOD_EXT)]
    return good[-1] if good else None


def main():
    if not KEY:
        raise SystemExit("Falta CF_API_KEY.")
    limit = min(int(sys.argv[1]) if len(sys.argv) > 1 else 1000, 10000)
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
            summary = (m.get("summary") or "").replace("\n", " ").strip()
            items.append({
                "name": m["name"],
                "summary": summary[:200],
                "file": unique_name(m["id"], f["fileName"]),
                "url": forge_url(f),
                "size": f.get("fileLength", 0),
            })
        index += len(mods)
        total = data.get("pagination", {}).get("totalCount", 0)
        print("Revisados %d | mcpack guardados %d | total en CF %d" % (index, len(items), total))
        if total and index >= total:
            break
        time.sleep(0.5)

    if not items:
        raise SystemExit("No se encontro ningun .mcpack; no se modifica %s" % OUT)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    print("Listo: %d addons en %s" % (len(items), OUT))


if __name__ == "__main__":
    main()

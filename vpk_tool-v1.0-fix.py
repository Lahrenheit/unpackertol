#!/usr/bin/env python3
"""
vpk_tool.py - Desempaqueta / empaqueta el demo.vpk ("vtPack") de Torrente Online.

Uso (sin argumentos se abre una ventana con menu; "menu" = menu en consola):
    python vpk_tool.py
    python vpk_tool.py list    demo.vpk
    python vpk_tool.py unpack  demo.vpk  carpeta_salida
    python vpk_tool.py pack    carpeta_origen  demo_nuevo.vpk
    python vpk_tool.py extract demo.vpk  ruta\\interna  carpeta_salida   (un archivo o carpeta)
    python vpk_tool.py texdds  a.tex b.dds carpeta ... [--salida carpeta_destino]

Formato del archivo (little endian):
    0x00  6 bytes   "vtPack"
    0x06  16 bytes  01 00 00 00 + 12 ceros (se conservan tal cual)
    0x16  u32       tamano de la cabecera = offset del primer dato (0x22)
    0x1A  u32       cantidad de registros de la tabla (carpetas + archivos)
    0x1E  u32       offset de la tabla (TOC)
    0x22  datos de los archivos, uno detras de otro, sin comprimir
    TOC   registros consecutivos hasta el final del archivo:
          u32 len_nombre, nombre + \\0,
          u32 len_dir,    dir    + \\0   (ruta de la carpeta con "\\" final; vacio = raiz)
          i32 flag (-1 carpeta de primer nivel, 0 el resto)
          u32 tamano, u32 tamano (repetido), u32 offset (0 en carpetas)
          u32 marca (AAAAAAAA carpeta raiz, BBBBBBBB subcarpeta, CCCCCCCC archivo)
          u32 tipo  (1 carpeta, 2 archivo)

Notas:
 * Los nombres se guardan como bytes. Algunos nombres de tu demo.vpk contienen
   bytes raros (basura); al extraer se escriben como %XX en el disco y al
   empaquetar se vuelven a convertir, asi se conservan exactos.
 * unpack crea ademas "_vpk_manifest.json" con el orden original y la cabecera;
   pack lo usa para dejar el archivo igual que el original (archivos nuevos van
   al final de su carpeta).
"""
import os
import sys
import json
import struct
import time
import zlib
import base64
import threading
import webbrowser   # import explicito: PyInstaller/auto-py-to-exe lo incluye en el .exe

MAGIC = b"vtPack"
DEFAULT_HDR_EXTRA = bytes([1, 0, 0, 0]) + bytes(12)
HEADER_SIZE = 0x22
TAG_ROOT, TAG_DIR, TAG_FILE = 0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC
MANIFEST = "_vpk_manifest.json"
REPO_URL = "https://github.com/Lahrenheit/unpackertol"
VERSION_TXT = "" + REPO_URL
COPY_BUF = 4 * 1024 * 1024
_BAD = set('<>:"/\\|?*%')


# ---------------------------------------------------------------- nombres
def raw_to_fs(raw: bytes) -> str:
    """bytes del VPK -> nombre seguro para Windows/Linux (reversible)."""
    out = []
    for i, b in enumerate(raw):
        ch = None
        if b >= 0x20 and b != 0x7F:
            try:
                ch = bytes([b]).decode("cp1252")
            except UnicodeDecodeError:
                ch = None
        if ch is None or ch in _BAD:
            out.append("%%%02X" % b)
        else:
            out.append(ch)
    s = "".join(out)
    # Windows no admite punto o espacio al final
    if s and s[-1] in ". ":
        s = s[:-1] + "%%%02X" % ord(s[-1])
    if s in (".", ".."):
        s = "".join("%%%02X" % ord(c) for c in s)
    return s


def fs_to_raw(name: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(name):
        c = name[i]
        if c == "%" and i + 2 < len(name) + 0 and _is_hex(name[i + 1:i + 3]):
            out.append(int(name[i + 1:i + 3], 16))
            i += 3
        else:
            try:
                out += c.encode("cp1252")
            except UnicodeEncodeError:
                raise SystemExit(
                    "Nombre no representable en el VPK (caracter fuera de cp1252): %r" % name)
            i += 1
    return bytes(out)


def _is_hex(s):
    return len(s) == 2 and all(c in "0123456789abcdefABCDEF" for c in s)


# ---------------------------------------------------------------- progreso
def console_progress(done, total, fdone, ftotal, name, final=False):
    pct = 100.0 if total == 0 else done * 100.0 / total
    if sys.stdout.isatty():
        w = 30
        k = int(w * pct / 100)
        line = "\r[%s%s] %5.1f%%  %d/%d archivos  %s" % (
            "#" * k, "-" * (w - k), pct, fdone, ftotal, name[-35:].ljust(35))
        sys.stdout.write(line + ("\n" if final else ""))
        sys.stdout.flush()
    else:
        step = int(pct // 10)
        if final or step > console_progress.last:
            console_progress.last = step
            print("%3d%%  %d/%d archivos" % (int(pct), fdone, ftotal))
console_progress.last = -1

progress_cb = console_progress   # la ventana lo reemplaza


class Progress:
    def __init__(self, total_bytes, total_files):
        self.total, self.ftotal = total_bytes, total_files
        self.done = self.fdone = 0
        self.name = ""
        self.last = 0.0
        console_progress.last = -1
        self._emit(False)

    def _emit(self, final):
        progress_cb(self.done, self.total, self.fdone, self.ftotal, self.name, final) \
            if progress_cb is console_progress else \
            progress_cb(self.done, self.total, self.fdone, self.ftotal, self.name)

    def add(self, nbytes, name=None):
        self.done += nbytes
        if name is not None:
            self.name = name
        now = time.time()
        if now - self.last >= 0.05:
            self.last = now
            self._emit(False)

    def file_done(self):
        self.fdone += 1

    def finish(self):
        self.done, self.fdone = self.total, self.ftotal
        self._emit(True)


# ---------------------------------------------------------------- lectura
def read_toc(f):
    f.seek(0, 2)
    total = f.tell()
    f.seek(0)
    head = f.read(HEADER_SIZE)
    if len(head) < HEADER_SIZE or head[:6] != MAGIC:
        raise SystemExit("No es un archivo vtPack (falta la firma 'vtPack').")
    hdr_extra = head[6:0x16]
    data_start, count, toc_off = struct.unpack_from("<III", head, 0x16)
    if not (HEADER_SIZE <= toc_off <= total):
        raise SystemExit("Offset de tabla invalido: %d" % toc_off)
    f.seek(toc_off)
    toc = f.read(total - toc_off)
    recs, p = [], 0
    try:
        while p < len(toc):
            nl, = struct.unpack_from("<I", toc, p); p += 4
            name = toc[p:p + nl]; p += nl
            dl, = struct.unpack_from("<I", toc, p); p += 4
            dr = toc[p:p + dl]; p += dl
            flag, s1, s2, off, tag, typ = struct.unpack_from("<iIIIII", toc, p); p += 24
            if name.endswith(b"\0"):
                name = name[:-1]
            if dr.endswith(b"\0"):
                dr = dr[:-1]
            recs.append(dict(name=name, dir=dr, flag=flag, size=s1, size2=s2,
                             off=off, tag=tag, type=typ))
    except struct.error:
        raise SystemExit("La tabla esta truncada o corrupta.")
    if len(recs) != count:
        print("Aviso: la cabecera dice %d registros y la tabla tiene %d." % (count, len(recs)),
              file=sys.stderr)
    return dict(hdr_extra=hdr_extra, data_start=data_start, toc_off=toc_off,
                total=total, recs=recs)


def rec_fs_path(r):
    """Ruta relativa en disco (con '/') de un registro."""
    parts = [raw_to_fs(x) for x in r["dir"].split(b"\\") if x]
    parts.append(raw_to_fs(r["name"]))
    return "/".join(parts)


# ---------------------------------------------------------------- comandos
def cmd_list(vpk):
    with open(vpk, "rb") as f:
        t = read_toc(f)
    nf = nd = 0
    for r in t["recs"]:
        if r["type"] == 2:
            nf += 1
            print("%10d  %10d  %s" % (r["off"], r["size"], rec_fs_path(r)))
        else:
            nd += 1
    print("\n%d archivos, %d carpetas, tabla en %d, tamano total %d" %
          (nf, nd, t["toc_off"], t["total"]))


def cmd_unpack(vpk, outdir):
    with open(vpk, "rb") as f:
        t = read_toc(f)
        os.makedirs(outdir, exist_ok=True)
        order = []
        n = 0
        fl = [r for r in t["recs"] if r["type"] == 2]
        pr = Progress(sum(r["size"] for r in fl), len(fl))
        for r in t["recs"]:
            rel = rec_fs_path(r)
            dest = os.path.join(outdir, *rel.split("/"))
            if r["type"] == 1:
                os.makedirs(dest, exist_ok=True)
                order.append(["d", rel, r["flag"]])
                continue
            if r["off"] + r["size"] > t["toc_off"]:
                raise SystemExit("Entrada fuera de rango: %s" % rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f.seek(r["off"])
            left = r["size"]
            with open(dest, "wb") as o:
                while left:
                    chunk = f.read(min(COPY_BUF, left))
                    if not chunk:
                        raise SystemExit("Archivo truncado leyendo %s" % rel)
                    o.write(chunk)
                    left -= len(chunk)
                    pr.add(len(chunk), rel)
            order.append(["f", rel, r["flag"]])
            n += 1
            pr.file_done()
        pr.finish()
    with open(os.path.join(outdir, MANIFEST), "w", encoding="utf-8") as m:
        json.dump(dict(hdr_extra=t["hdr_extra"].hex(), order=order), m,
                  ensure_ascii=False, indent=0)
    print("Desempaquetados %d archivos en %s" % (n, outdir))


def cmd_extract_items(vpk, items, outdir):
    """Extrae solo algunos registros. items = lista de (tipo, registro, ruta_relativa_en_disco)."""
    with open(vpk, "rb") as f:
        total = os.path.getsize(vpk)
        files = [it for it in items if it[0] == "f"]
        pr = Progress(sum(r["size"] for _, r, _ in files), len(files))
        os.makedirs(outdir, exist_ok=True)
        n = 0
        for kind, r, rel in items:
            dest = os.path.join(outdir, *rel.split("/"))
            if kind == "d":
                os.makedirs(dest, exist_ok=True)
                continue
            if r["off"] + r["size"] > total:
                raise SystemExit("Entrada fuera de rango: %s" % rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f.seek(r["off"])
            left = r["size"]
            with open(dest, "wb") as o:
                while left:
                    chunk = f.read(min(COPY_BUF, left))
                    if not chunk:
                        raise SystemExit("Archivo truncado leyendo %s" % rel)
                    o.write(chunk)
                    left -= len(chunk)
                    pr.add(len(chunk), rel)
            n += 1
            pr.file_done()
        pr.finish()
    print("Extraidos %d archivos en %s" % (n, outdir))


def select_items(recs, r):
    """Registros a extraer al elegir r (archivo o carpeta con todo su contenido)."""
    def parts(raw):
        return [raw_to_fs(x) for x in raw.split(b"\\") if x]
    if r["type"] == 2:
        return [("f", r, raw_to_fs(r["name"]))]
    prefix = r["dir"] + r["name"] + b"\\"
    top = raw_to_fs(r["name"])
    items = [("d", r, top)]
    for x in recs:
        if x is r:
            continue
        if x["type"] == 2 and x["dir"].startswith(prefix):
            rel = "/".join([top] + parts(x["dir"][len(prefix):]) + [raw_to_fs(x["name"])])
            items.append(("f", x, rel))
        elif x["type"] == 1 and (x["dir"] + x["name"] + b"\\").startswith(prefix):
            rel = "/".join([top] + parts(x["dir"][len(prefix):]) + [raw_to_fs(x["name"])])
            items.append(("d", x, rel))
    return items


def select_many(recs, chosen):
    """Une varios elementos elegidos (archivos y/o carpetas) sin repetir registros
    ni pisar nombres iguales."""
    items, seen, rels = [], set(), set()
    for r in chosen:
        for kind, x, rel in select_items(recs, r):
            if id(x) in seen:
                continue
            seen.add(id(x))
            if kind == "f" and rel.lower() in rels:
                base, ext = os.path.splitext(rel)
                k = 2
                while ("%s (%d)%s" % (base, k, ext)).lower() in rels:
                    k += 1
                rel = "%s (%d)%s" % (base, k, ext)
            rels.add(rel.lower())
            items.append((kind, x, rel))
    return items


# ---------------------------------------------------------------- visor de texturas .tex
def _rgb565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def decode_dxt(w, h, fmt, data, step=1):
    """Decodifica DXT1 (fmt 0) o DXT5 (fmt 1) tomando 1 de cada 'step' pixeles.
    Devuelve (ancho, alto, bytearray RGBA)."""
    bw, bh = (w + 3) // 4, (h + 3) // 4
    bpb = 8 if fmt == 0 else 16
    ow, oh = (w + step - 1) // step, (h + step - 1) // step
    out = bytearray(ow * oh * 4)
    for by in range(bh):
        ys = [j for j in range(4) if (by * 4 + j) % step == 0 and by * 4 + j < h]
        if not ys:
            continue
        for bx in range(bw):
            xs = [i for i in range(4) if (bx * 4 + i) % step == 0 and bx * 4 + i < w]
            if not xs:
                continue
            o = (by * bw + bx) * bpb
            blk = data[o:o + bpb]
            if len(blk) < bpb:
                continue
            if fmt == 1:
                a0, a1 = blk[0], blk[1]
                abits = int.from_bytes(blk[2:8], "little")
                if a0 > a1:
                    al = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
                else:
                    al = [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255]
                cb = blk[8:16]
            else:
                cb = blk
            c0, c1, bits = struct.unpack("<HHI", cb)
            r0, g0, b0 = _rgb565(c0)
            r1, g1, b1 = _rgb565(c1)
            if c0 > c1 or fmt == 1:
                pal = [(r0, g0, b0, 255), (r1, g1, b1, 255),
                       ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
                       ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255)]
            else:
                pal = [(r0, g0, b0, 255), (r1, g1, b1, 255),
                       ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255), (0, 0, 0, 0)]
            for j in ys:
                for i in xs:
                    k = j * 4 + i
                    px = pal[(bits >> (2 * k)) & 3]
                    a = px[3] if fmt == 0 else al[(abits >> (3 * k)) & 7]
                    q = (((by * 4 + j) // step) * ow + (bx * 4 + i) // step) * 4
                    out[q], out[q + 1], out[q + 2], out[q + 3] = px[0], px[1], px[2], a
    return ow, oh, out


def _png_rgb(w, h, raw):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def tex_preview(data, box=440, ignore_alpha=False):
    """Convierte un .tex (TeX2, DXT1/DXT5) en un PNG que cabe en box x box.
    Devuelve (bytes_png, texto_info). La transparencia se muestra sobre un tablero gris."""
    if data[:4] != b"TeX2":
        raise ValueError("no es un TeX2")
    fmt = data[4]
    if fmt not in (0, 1):
        raise ValueError("formato %d no soportado" % fmt)
    w, h, sz = struct.unpack("<III", data[9:21])
    if w == 0 or h == 0 or w > 8192 or h > 8192:
        raise ValueError("tamano invalido %dx%d" % (w, h))
    step = 1
    while max(w, h) // step > 1024:
        step *= 2
    ow, oh, rgba = decode_dxt(w, h, fmt, data[21:21 + sz], step)
    sc = min(box / ow, box / oh, 4.0)
    nw, nh = max(1, int(ow * sc)), max(1, int(oh * sc))
    xi = [min(ow - 1, int(x / sc)) for x in range(nw)]
    raw = bytearray()
    for y in range(nh):
        base = min(oh - 1, int(y / sc)) * ow * 4
        row = bytearray(nw * 3)
        for x in range(nw):
            q = base + xi[x] * 4
            r, g, b, a = rgba[q], rgba[q + 1], rgba[q + 2], rgba[q + 3]
            if a != 255 and not ignore_alpha:
                bg = 205 if ((x >> 3) + (y >> 3)) & 1 else 140
                r, g, b = (r * a + bg * (255 - a)) // 255, (g * a + bg * (255 - a)) // 255, \
                          (b * a + bg * (255 - a)) // 255
            row[x * 3:x * 3 + 3] = bytes((r, g, b))
        raw += b"\0" + row
    return _png_rgb(nw, nh, bytes(raw)), "%dx%d  %s" % (w, h, "DXT1" if fmt == 0 else "DXT5")


def read_tex_bytes(vpk, rec):
    """Lee de un VPK solo lo necesario para mostrar una textura (cabecera + nivel principal)."""
    with open(vpk, "rb") as f:
        f.seek(rec["off"])
        head = f.read(21)
        if len(head) < 21 or head[:4] != b"TeX2":
            raise ValueError("no es un TeX2")
        w, h = struct.unpack("<II", head[9:17])
        need = ((w + 3) // 4) * ((h + 3) // 4) * (8 if head[4] == 0 else 16)
        need = min(need, max(0, rec["size"] - 21))
        return head + f.read(need)


# ---------------------------------------------------------------- copia de seguridad / edicion directa
_BACKED = set()   # VPK que ya tienen copia .bak en esta sesion


def _bak_name(vpk):
    n = vpk + ".bak"
    k = 1
    while os.path.exists(n):
        n = "%s.%d.bak" % (vpk, k)
        k += 1
    return n


def backup_copy(vpk):
    """Hace una copia .bak del VPK antes del primer cambio directo de la sesion."""
    key = os.path.realpath(vpk)
    if key in _BACKED:
        return None
    bak = _bak_name(vpk)
    pr = Progress(os.path.getsize(vpk), 1)
    label = "Copia de seguridad: " + os.path.basename(bak)
    with open(vpk, "rb") as fi, open(bak, "wb") as fo:
        while True:
            c = fi.read(COPY_BUF)
            if not c:
                break
            fo.write(c)
            pr.add(len(c), label)
    pr.file_done()
    pr.finish()
    _BACKED.add(key)
    print("Copia de seguridad: %s" % bak)
    return bak


def cmd_replace(vpk, rec, srcfile):
    """Reemplazo en el sitio (ver replace_in_vpk) con copia .bak automatica."""
    backup_copy(vpk)
    w, orig, new = replace_in_vpk(vpk, rec, srcfile)
    if new == orig:
        caso = "igual tamano"
    elif new > orig:
        caso = "MAYOR: se escribio solo hasta el tamano original (%d de %d bytes)" % (w, new)
    else:
        caso = "menor: rellenado con ceros (%d bytes)" % (orig - new)
    print("Reemplazado: %s\nPor: %s\nTamano %s" % (rec_display(rec), srcfile, caso))


def _toc_bytes(r):
    nm, dr = r["name"], r["dir"]
    b = struct.pack("<I", len(nm) + 1) + nm + b"\0"
    b += struct.pack("<I", len(dr) + 1 if dr else 0) + (dr + b"\0" if dr else b"")
    return b + struct.pack("<iIIIII", r["flag"], r["size"], r["size"], r["off"], r["tag"], r["type"])


def plan_add(recs, target, files=(), folder=None):
    """Prepara lo que se va a anadir dentro de la carpeta interna 'target' (bytes con \\ final).
    Devuelve (adds, conflictos): conflictos = archivos que ya existen (mismo nombre)."""
    adds = []
    existing = {(r["dir"].upper(), r["name"].upper()) for r in recs if r["type"] == 2}
    for p in files:
        adds.append(dict(kind="f", dir=target, name=fs_to_raw(os.path.basename(p)), src=p))

    def add_dir(path, parent):
        nm = fs_to_raw(os.path.basename(os.path.normpath(path)))
        adds.append(dict(kind="d", dir=parent, name=nm, src=None))
        child = parent + nm + b"\\"
        for e in sorted(os.scandir(path), key=lambda e: e.name.upper()):
            if e.is_dir():
                add_dir(e.path, child)
            elif e.name != MANIFEST:
                adds.append(dict(kind="f", dir=child, name=fs_to_raw(e.name), src=e.path))
    if folder:
        add_dir(folder, target)
    conflicts = [a for a in adds if a["kind"] == "f" and (a["dir"].upper(), a["name"].upper()) in existing]
    return adds, conflicts


def modify_vpk(vpk, remove_keys=(), adds=()):
    """Edita el VPK: elimina registros (clave = (dir, nombre, tipo)) y anade o reemplaza archivos
    y carpetas. Reescribe el VPK completo en un archivo temporal (datos contiguos, sin restos),
    deja el original como copia .bak (una vez por sesion) y pone el nuevo en su lugar."""
    with open(vpk, "rb") as f0:
        t = read_toc(f0)
    old = t["recs"]
    remove_keys = set(remove_keys)
    keep = [r for r in old if (r["dir"], r["name"], r["type"]) not in remove_keys]
    removed = len(old) - len(keep)
    index = {(r["dir"].upper(), r["name"].upper(), r["type"]): r for r in keep}
    new_recs, n_new, n_rep = [], 0, 0
    for a in adds:
        typ = 1 if a["kind"] == "d" else 2
        k = (a["dir"].upper(), a["name"].upper(), typ)
        if k in index:
            if typ == 2:
                index[k]["src"] = a["src"]
                n_rep += 1
            continue
        top = (a["dir"] == b"")
        r = dict(name=a["name"], dir=a["dir"], flag=-1 if (top and typ == 1) else 0, size=0,
                 size2=0, off=0, type=typ, src=a["src"],
                 tag=(TAG_ROOT if top else TAG_DIR) if typ == 1 else TAG_FILE)
        new_recs.append(r)
        index[k] = r
        if typ == 2:
            n_new += 1
    allr = keep + new_recs
    # orden de la tabla: por carpeta, sus entradas y luego cada subcarpeta (como el original)
    kids = {}
    for r in allr:
        kids.setdefault(r["dir"], []).append(r)
    out, seen_dirs = [], set()

    def emit(d):
        if d in seen_dirs:
            return
        seen_dirs.add(d)
        items = kids.get(d, [])
        out.extend(items)
        for r in items:
            if r["type"] == 1:
                emit(r["dir"] + r["name"] + b"\\")
    emit(b"")
    ids = {id(r) for r in out}
    out.extend(r for r in allr if id(r) not in ids)   # huerfanos (por si acaso)

    files = [r for r in out if r["type"] == 2]
    sizes = [os.path.getsize(r["src"]) if r.get("src") else r["size"] for r in files]
    pr = Progress(sum(sizes), len(files))
    tmp = vpk + ".tmp"
    bak = None
    try:
        with open(vpk, "rb") as fi, open(tmp, "wb") as o:
            o.write(b"\0" * HEADER_SIZE)
            toc = bytearray()
            for r in out:
                if r["type"] == 2:
                    off = o.tell()
                    label = rec_display(r)
                    if r.get("src"):
                        with open(r["src"], "rb") as fs_:
                            while True:
                                c = fs_.read(COPY_BUF)
                                if not c:
                                    break
                                o.write(c)
                                pr.add(len(c), label)
                    else:
                        fi.seek(r["off"])
                        left = r["size"]
                        while left:
                            c = fi.read(min(COPY_BUF, left))
                            if not c:
                                raise SystemExit("VPK truncado leyendo %s" % label)
                            o.write(c)
                            left -= len(c)
                            pr.add(len(c), label)
                    if o.tell() >= 0xFFFFFFFF:
                        raise SystemExit("El VPK superaria 4 GB; el formato usa offsets de 32 bits.")
                    r["off"], r["size"] = off, o.tell() - off
                    pr.file_done()
                toc += _toc_bytes(r)
            toc_off = o.tell()
            o.write(toc)
            o.seek(0)
            o.write(MAGIC + t["hdr_extra"] + struct.pack("<III", HEADER_SIZE, len(out), toc_off))
        pr.finish()
        key = os.path.realpath(vpk)
        if key not in _BACKED:
            bak = _bak_name(vpk)
            os.replace(vpk, bak)
            _BACKED.add(key)
        try:
            os.replace(tmp, vpk)
        except Exception:
            if bak:
                os.replace(bak, vpk)
                _BACKED.discard(key)
                bak = None
            raise
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    if bak:
        print("Copia de seguridad: %s" % bak)
    print("Anadidos: %d archivos nuevos, reemplazados: %d, eliminados: %d registros." % (
        n_new, n_rep, removed))


def cmd_pack(src, out):
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        raise SystemExit("No existe la carpeta: %s" % src)
    rank, hdr_extra, flags = {}, DEFAULT_HDR_EXTRA, {}
    mpath = os.path.join(src, MANIFEST)
    if os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as m:
            man = json.load(m)
        hdr_extra = bytes.fromhex(man["hdr_extra"])
        for i, (k, rel, fl) in enumerate(man["order"]):
            rank[(k, rel.lower())] = i
            flags[rel.lower()] = fl

    def sort_key(kind, rel, name):
        return (rank.get((kind, rel.lower()), 1 << 30), fs_to_raw(name).upper())

    # Orden de la tabla (igual al original): por cada carpeta se escriben sus
    # entradas (subcarpetas y archivos mezclados, orden alfabetico sin distinguir
    # mayusculas) y luego se entra en cada subcarpeta.
    entries = []   # (tipo, ruta relativa)

    def emit(rel):
        base = os.path.join(src, *rel.split("/")) if rel else src
        items = []
        for e in os.scandir(base):
            if rel == "" and e.name == MANIFEST:
                continue
            r2 = (rel + "/" + e.name) if rel else e.name
            kind = "d" if e.is_dir() else "f"
            items.append((sort_key(kind, r2, e.name), kind, r2))
        items.sort(key=lambda x: x[0])
        for _, kind, r2 in items:
            entries.append((kind, r2))
        for _, kind, r2 in items:
            if kind == "d":
                emit(r2)
    emit("")
    n_dirs = sum(1 for k, _ in entries if k == "d")
    n_files = len(entries) - n_dirs

    def raw_dir(rel):
        parts = rel.split("/")[:-1]
        return b"".join(fs_to_raw(p) + b"\\" for p in parts)

    toc = bytearray()

    def add(name, dr, flag, size, off, tag, typ):
        toc.extend(struct.pack("<I", len(name) + 1)); toc.extend(name + b"\0")
        toc.extend(struct.pack("<I", len(dr) + 1 if dr else 0))
        if dr:
            toc.extend(dr + b"\0")
        toc.extend(struct.pack("<iIIIII", flag, size, size, off, tag, typ))

    tmp = out + ".tmp"
    fsizes = [os.path.getsize(os.path.join(src, *rel.split("/")))
              for k, rel in entries if k == "f"]
    pr = Progress(sum(fsizes), len(fsizes))
    with open(tmp, "wb") as o:
        o.write(b"\0" * HEADER_SIZE)
        for kind, rel in entries:
            nm = fs_to_raw(rel.split("/")[-1])
            if kind == "d":
                top = "/" not in rel
                add(nm, raw_dir(rel), -1 if top else 0, 0, 0,
                    TAG_ROOT if top else TAG_DIR, 1)
                continue
            p = os.path.join(src, *rel.split("/"))
            off = o.tell()
            with open(p, "rb") as fin:
                while True:
                    chunk = fin.read(COPY_BUF)
                    if not chunk:
                        break
                    o.write(chunk)
                    pr.add(len(chunk), rel)
            pr.file_done()
            size = o.tell() - off
            if o.tell() >= 0xFFFFFFFF:
                raise SystemExit("El archivo supera 4 GB; el formato usa offsets de 32 bits.")
            add(nm, raw_dir(rel), 0, size, off, TAG_FILE, 2)
        toc_off = o.tell()
        o.write(toc)
        o.seek(0)
        o.write(MAGIC + hdr_extra + struct.pack("<III", HEADER_SIZE,
                n_dirs + n_files, toc_off))
    os.replace(tmp, out)
    pr.finish()
    print("Creado %s: %d archivos, %d carpetas, %d bytes" %
          (out, n_files, n_dirs, os.path.getsize(out)))


# ---------------------------------------------------------------- TEX <-> DDS
# Conversion de texturas .tex de Torrente Online (formato 'TeX2') <-> .dds (DXT1/DXT5).
def mips(w, h, bpb, size):
    n, tot = 0, 0
    while tot < size:
        tot += max(w // 4, 1) * max(h // 4, 1) * bpb
        w, h, n = max(w // 2, 1), max(h // 2, 1), n + 1
    return n


def tex2dds(b):
    if b[:4] != b'TeX2':
        raise ValueError('no es un TeX2')
    fmt = b[4]
    w, h, sz = struct.unpack('<III', b[9:21])
    fourcc = {0: b'DXT1', 1: b'DXT5'}[fmt]
    bpb = 8 if fmt == 0 else 16
    n = mips(w, h, bpb, sz)
    flags = 0x81007 | (0x20000 if n > 1 else 0)
    caps = 0x1000 | (0x400008 if n > 1 else 0)
    hdr = (b'DDS ' + struct.pack('<7I', 124, flags, h, w, sz, 0, n) + b'\0' * 44 +
           struct.pack('<II4s5I', 32, 4, fourcc, 0, 0, 0, 0, 0) +
           struct.pack('<5I', caps, 0, 0, 0, 0))
    return hdr + b[21:21 + sz]


def dds2tex(b):
    """Acepta DXT1/DXT5 (tambien BC1/BC3 con cabecera DX10). Se queda SOLO con el nivel
    principal: los .tex originales casi nunca llevan mipmaps y el juego se cuelga si
    el tamano de datos no coincide con lo que espera."""
    if b[:4] != b'DDS ':
        raise ValueError('no es un DDS')
    h, w = struct.unpack('<II', b[12:20])
    fc = b[84:88]
    off = 128
    if fc == b'DX10':
        dxgi = struct.unpack('<I', b[128:132])[0]
        off = 148
        fc = {70: b'DXT1', 71: b'DXT1', 72: b'DXT1', 76: b'DXT5', 77: b'DXT5',
              78: b'DXT5'}.get(dxgi, b'DX10/%d' % dxgi)
    fmt = {b'DXT1': 0, b'DXT5': 1}.get(fc)
    if fmt is None:
        raise ValueError('formato %r no soportado: guardalo como DXT1 (sin alfa) o DXT5 (con alfa)' % fc)
    bpb = 8 if fmt == 0 else 16
    sz = max(w // 4, 1) * max(h // 4, 1) * bpb
    data = b[off:off + sz]
    if len(data) != sz:
        raise ValueError('el DDS esta incompleto')
    return b'TeX2' + bytes([fmt]) + struct.pack('<I', 1) + struct.pack('<III', w, h, sz) + data


def convertir_textura(p, destino=None):
    ext = os.path.splitext(p)[1].lower()
    if ext not in ('.tex', '.dds'):
        return None
    src_dir = os.path.dirname(os.path.abspath(p))
    if destino:
        out_dir = destino
    else:
        out_dir = src_dir if os.path.basename(src_dir) == 'convertidos' else os.path.join(src_dir, 'convertidos')
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(p))[0]
    with open(p, 'rb') as fh:
        data = fh.read()
    if ext == '.tex':
        out = os.path.join(out_dir, base + '.dds')
        res = tex2dds(data)
    else:
        out = os.path.join(out_dir, base + '.tex')
        res = dds2tex(data)
    with open(out, 'wb') as fh:
        fh.write(res)
    return out


def _expandir(rutas):
    for p in rutas:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                yield os.path.join(p, f)
        else:
            yield p


def cmd_texdds(rutas, destino=None):
    """Convierte .tex -> .dds y .dds -> .tex (segun extension). Acepta archivos y carpetas."""
    lista = [p for p in _expandir(rutas)
             if os.path.splitext(p)[1].lower() in ('.tex', '.dds') and os.path.isfile(p)]
    if not lista:
        print("No hay archivos .tex ni .dds para convertir.")
        return
    pr = Progress(sum(os.path.getsize(p) for p in lista), len(lista))
    ok, errores = [], []
    for p in lista:
        try:
            o = convertir_textura(p, destino)
            if o:
                ok.append(o)
        except Exception as e:
            errores.append('%s: %s' % (os.path.basename(p), e))
        pr.add(os.path.getsize(p), os.path.basename(p))
        pr.file_done()
    pr.finish()
    print("Convertidos: %d" % len(ok))
    if ok:
        print("Carpeta: %s" % os.path.dirname(ok[0]))
    if errores:
        print("\nErrores:\n" + "\n".join(errores))


# ---------------------------------------------------------------- menu
def _run_safe(fn, *args):
    try:
        fn(*args)
    except SystemExit as e:
        if e.code not in (None, 0):
            print("ERROR:", e.code)
    except Exception as e:  # noqa
        print("ERROR: %s: %s" % (type(e).__name__, e))


def console_menu():
    while True:
        print("\n=== VPK Tool - Torrente Online ===")
        print("1) Listar contenido de un VPK")
        print("2) Desempaquetar VPK a una carpeta")
        print("3) Empaquetar una carpeta en un VPK")
        print("4) TEX/DDS: convertir texturas .tex <-> .dds")
        print("0) Salir")
        op = input("Opcion: ").strip()
        if op == "1":
            _run_safe(cmd_list, input("Ruta del VPK: ").strip().strip('"'))
        elif op == "2":
            v = input("Ruta del VPK: ").strip().strip('"')
            d = input("Carpeta de salida: ").strip().strip('"')
            _run_safe(cmd_unpack, v, d)
        elif op == "3":
            d = input("Carpeta origen: ").strip().strip('"')
            v = input("VPK a crear (ej. demo.vpk): ").strip().strip('"')
            if os.path.exists(v) and input("Ya existe, sobrescribir? (s/n): ").lower() != "s":
                continue
            _run_safe(cmd_pack, d, v)
        elif op == "4":
            r = input("Archivos o carpeta (separa varios con ;): ").strip()
            rutas = [x.strip().strip('"') for x in r.split(";") if x.strip()]
            d = input("Carpeta de destino (vacio = subcarpeta 'convertidos'): ").strip().strip('"')
            _run_safe(cmd_texdds, rutas, d or None)
        elif op == "0":
            return


def replace_in_vpk(vpk, rec, srcfile):
    """Reemplaza el contenido de un archivo DENTRO del VPK sin mover nada mas
    (como el 'Reemplazar' del UnpaKer original):
      igual tamano -> se reemplaza
      mayor        -> se escribe solo hasta el tamano original
      menor        -> se escribe y el resto se rellena con ceros (00)
    Devuelve (bytes_escritos, tamano_original, tamano_nuevo)."""
    with open(srcfile, "rb") as fh:
        data = fh.read()
    size = rec["size"]
    chunk = data[:size] + b"\0" * max(0, size - len(data))
    with open(vpk, "r+b") as f:
        f.seek(rec["off"])
        f.write(chunk)
    return min(len(data), size), size, len(data)


def open_url(url):
    """Abre una direccion en el navegador (funciona tambien dentro del .exe)."""
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa
        pass
    try:                       # respaldo en Windows
        os.startfile(url)      # noqa
        return True
    except Exception:  # noqa
        return False


def _find_ico():
    """Busca un .ico junto al programa (o dentro del .exe si se agrego como archivo adicional)."""
    bases = [getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(sys.argv[0] or ".")),
             os.path.dirname(os.path.abspath(__file__))]
    for b in bases:
        if b and os.path.isdir(b):
            for f in sorted(os.listdir(b)):
                if f.lower().endswith(".ico"):
                    return os.path.join(b, f)
    return None


def _icon_from_exe(root):
    """Windows: toma el icono incrustado en el propio .exe (el que se eligio en auto-py-to-exe)
    y se lo pone a la ventana y a la barra de tareas."""
    import ctypes
    from ctypes import wintypes
    root.update_idletasks()
    large, small = wintypes.HICON(), wintypes.HICON()
    shell32, user32 = ctypes.windll.shell32, ctypes.windll.user32
    shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                       ctypes.POINTER(wintypes.HICON),
                                       ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    if shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1) < 1:
        return False
    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    if large.value:
        user32.SendMessageW(hwnd, 0x80, 1, large.value)   # WM_SETICON, ICON_BIG
    if small.value:
        user32.SendMessageW(hwnd, 0x80, 0, small.value)   # WM_SETICON, ICON_SMALL
    return bool(large.value or small.value)


def set_app_icon(root):
    """Pone el icono de la aplicacion en lugar de la pluma de Tk (sin fallar si no se puede)."""
    ico = _find_ico()
    if ico:
        try:
            root.iconbitmap(default=ico)
            return True
        except Exception:  # noqa
            pass
    if os.name == "nt" and getattr(sys, "frozen", False):
        try:
            return _icon_from_exe(root)
        except Exception:  # noqa
            pass
    return False


def rec_display(r):
    """Ruta tal como la muestra el UnpaKer (dir + nombre, con \\)."""
    return (r["dir"] + r["name"]).decode("cp1252", errors="replace")


def gui():
    import threading
    import contextlib
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("VPK Unpacker Tool v1.0")
    root.geometry("1280x700")
    root.minsize(1060, 560)
    set_app_icon(root)

    # panel del visor (derecha) y panel principal (izquierda)
    right = tk.Frame(root, width=470)
    right.pack(side="right", fill="y", padx=(0, 8), pady=8)
    right.pack_propagate(False)
    left = tk.Frame(root)
    left.pack(side="left", fill="both", expand=True)

    FONT = ("Courier New", 10, "bold")
    state = {"vpk": None, "recs": [], "rows": {}, "busy": False}
    buttons = []

    def box(parent, text, width, lw=0):
        """Etiqueta con borde + cuadro de valor, como los contadores del UnpaKer."""
        tk.Label(parent, text=text, font=("Courier New", 11, "bold"), relief="raised",
                 bd=2, bg="#f0d9b5" if text in ("Extraidos",) else "#f0f0f0",
                 padx=10, width=lw).pack(side="left", padx=(6, 2))
        v = tk.Label(parent, text="", font=FONT, relief="sunken", bd=2, bg="white",
                     width=width, anchor="w")
        v.pack(side="left", padx=(0, 6))
        return v

    def mkbtn(parent, text, cmd, w=9):
        b = tk.Button(parent, text=text, width=w, command=cmd, font=("MS Sans Serif", 9, "bold"))
        buttons.append(b)
        return b

    # --- fila 1: Leer + contadores
    row1 = tk.Frame(left)
    row1.pack(fill="x", padx=6, pady=(8, 2))
    btn_leer = mkbtn(row1, "Leer", lambda: do_leer(), 14)
    btn_leer.pack(side="left")
    v_archivos = box(row1, "Archivos", 6, 10)
    v_dirs = box(row1, "Dirs", 5)
    v_total = box(row1, "Total", 6)
    v_extr = box(row1, "Extraidos", 6, 10)
    v_extr.config(text="0")

    # --- fila 2: Desempaquetar, A\u00f1adir, Reemplazar (+ Empaquetar y TEX/DDS a la derecha)
    row2 = tk.Frame(left)
    row2.pack(fill="x", padx=6, pady=2)
    btn_extraer = mkbtn(row2, "Desempaquetar", lambda: do_extraer(), 14)
    btn_extraer.pack(side="left")
    mkbtn(row2, "A\u00f1adir", lambda: do_anadir(), 9).pack(side="left", padx=4)
    btn_rep = mkbtn(row2, "Reemplazar", lambda: do_reemplazar(), 12)
    btn_rep.pack(side="left")
    mkbtn(row2, "TEX/DDS", lambda: do_texdds(), 9).pack(side="right", padx=2)
    mkbtn(row2, "Empaquetar", lambda: do_pack(), 11).pack(side="right", padx=2)
    # version + enlace: en esta misma fila, entre Reemplazar y Empaquetar
    verbox = tk.Frame(row2)
    verbox.pack(side="left", expand=True, padx=8)
    tk.Label(verbox, text="", font=("MS Sans Serif", 8)).pack(side="left")   # sin enlace
    link = tk.Label(verbox, text=REPO_URL, fg="#0645ad", cursor="hand2",             # solo la URL es enlace
                    font=("MS Sans Serif", 8, "underline"))
    link.pack(side="left")
    link.bind("<Button-1>", lambda e: open_url(REPO_URL) or messagebox.showinfo(
        "Enlace", "No se pudo abrir el navegador.\n\n" + REPO_URL))

    # --- barra de progreso
    bar_frame = tk.Frame(left)
    bar_frame.pack(fill="x", padx=8, pady=(4, 2))
    bar = ttk.Progressbar(bar_frame, maximum=100, mode="determinate")
    bar.pack(fill="x")
    status = tk.Label(bar_frame, text="Pulsa 'Leer' para abrir un VPK.", anchor="w")
    status.pack(fill="x")

    # --- lista
    mid = tk.Frame(left)   # se empaqueta al final: la lista se queda con el espacio sobrante
    cols = ("archivo", "sizeb", "size", "offset")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended", height=6)
    for c, t, w, anc in (("archivo", "Archivo", 330, "w"), ("sizeb", "Size b.", 80, "w"),
                         ("size", "Size", 80, "w"), ("offset", "Offset", 80, "w")):
        tree.heading(c, text=t, anchor="w")
        tree.column(c, width=w, anchor=anc, stretch=(c == "archivo"))
    ysb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ysb.set)
    ysb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    # --- Extraer / Eliminar y buscador
    rowx = tk.Frame(left)
    mkbtn(rowx, "Extraer Archivo(s)/Carpeta(s)", lambda: do_extraer_sel(), 29).pack(side="left")
    mkbtn(rowx, "Eliminar Archivo(s)/Carpeta(s)", lambda: do_eliminar(), 29).pack(side="left", padx=4)
    rowb = tk.Frame(left)
    tk.Label(rowb, text="Buscar:", font=("MS Sans Serif", 9, "bold")).pack(side="left")
    search_var = tk.StringVar()
    tk.Entry(rowb, textvariable=search_var, font=("Courier New", 10)).pack(
        side="left", fill="x", expand=True, padx=(6, 0))

    # --- leyenda amarilla
    leg = tk.Frame(left, bg="#ffffc0", bd=2, relief="groove")
    tk.Label(leg, bg="#ffffc0", justify="left", anchor="w", font=("Courier New", 9),
             text=(" Si es Igual tama\u00f1o --> Se reemplaza\n"
                   " Si es Mayor tama\u00f1o --> Se reemplaza solo hasta el tama\u00f1o original\n"
                   " Si es Menor tama\u00f1o --> Se reemplaza y el resto se llena con ceros (00)")
             ).pack(fill="x", padx=4, pady=4)
    # Las filas de abajo se empaquetan primero (de abajo hacia arriba) y la lista al final:
    # con cualquier tamano de ventana el orden de los botones es siempre el mismo.
    leg.pack(side="bottom", fill="x", padx=6, pady=(4, 2))
    rowb.pack(side="bottom", fill="x", padx=6, pady=(6, 0))
    rowx.pack(side="bottom", fill="x", padx=6, pady=(4, 0))
    mid.pack(side="top", fill="both", expand=True, padx=6, pady=2)
    try:   # que la fila de contadores nunca quede cortada
        root.update_idletasks()
        root.minsize(max(1060, left.winfo_reqwidth() + 470 + 24), 560)
    except Exception:  # noqa
        pass

    # ------------------------------------------------------------ logica
    def set_buttons(st):
        for b in buttons:
            b.config(state=st)

    def say(txt):
        status.config(text=txt)

    def gui_progress(done, total, fdone, ftotal, name):
        pct = 100.0 if total == 0 else done * 100.0 / total

        def _u():
            bar["value"] = pct
            status.config(text="%.1f%%  -  %d/%d archivos  -  %s" % (pct, fdone, ftotal, name))
            if state.get("count_extr"):
                v_extr.config(text=str(fdone))
        root.after(0, _u)

    globals()["progress_cb"] = gui_progress

    def run(fn, *args, count_extr=False, on_done=None):
        if state["busy"]:
            return
        state["busy"] = True
        state["count_extr"] = count_extr
        bar["value"] = 0
        set_buttons("disabled")
        buf = []

        class W:
            def write(self, t):
                buf.append(t)
                return len(t)

            def flush(self):
                pass

        def work():
            w = W()
            with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                _run_safe(fn, *args)
            text = "".join(buf).strip()

            def fin():
                state["busy"] = False
                set_buttons("normal")
                if on_done:
                    on_done(text)
            root.after(0, fin)
        threading.Thread(target=work, daemon=True).start()

    def populate():
        """Llena la lista aplicando el filtro de 'Buscar:' (sin distinguir mayusculas)."""
        q = search_var.get().strip().lower()
        tree.delete(*tree.get_children())
        n = 0
        for iid, vals in state.get("all", []):
            if q and q not in vals[0].lower():
                continue
            tree.insert("", "end", iid=iid, values=vals)
            n += 1
        return n

    def on_search(*_):
        if not state.get("all"):
            return
        n = populate()
        q = search_var.get().strip()
        say("%d coincidencias de %d" % (n, len(state["all"])) if q else
            "Mostrando los %d registros" % len(state["all"]))
    search_var.trace_add("write", on_search)

    def cargar(path):
        try:
            with open(path, "rb") as f:
                t = read_toc(f)
        except SystemExit as e:
            messagebox.showerror("Leer", str(e))
            return
        except Exception as e:  # noqa
            messagebox.showerror("Leer", "%s: %s" % (type(e).__name__, e))
            return
        state["vpk"], state["recs"], state["rows"], state["all"] = path, t["recs"], {}, []
        nf = nd = 0
        for i, r in enumerate(t["recs"]):
            if r["type"] == 2:
                nf += 1
            else:
                nd += 1
            iid = str(i)
            state["all"].append((iid, (rec_display(r), r["size"], "%08X" % r["size"],
                                       "%08X" % r["off"])))
            state["rows"][iid] = r
        populate()
        v_archivos.config(text=str(nf))
        v_dirs.config(text=str(nd))
        v_total.config(text=str(len(t["recs"])))
        v_extr.config(text="0")
        bar["value"] = 0
        root.title("VPK Unpacker Tool v1.0 - %s" % os.path.basename(path))
        say("Leido: %s" % path)

    def do_leer():
        v = filedialog.askopenfilename(title="Elegir VPK",
                                       filetypes=[("VPK", "*.vpk"), ("Todos", "*.*")])
        if v:
            cargar(v)

    def need_vpk():
        if not state["vpk"]:
            do_leer()
        return state["vpk"]

    def do_extraer():
        v = need_vpk()
        if not v:
            return
        d = filedialog.askdirectory(title="Carpeta donde desempaquetar", mustexist=False,
                                    initialdir=os.path.dirname(os.path.abspath(v)))
        if not d:
            return
        v_extr.config(text="0")

        def done(text):
            say(text.splitlines()[-1] if text else "")
            if "ERROR" in text:
                messagebox.showwarning("Desempaquetar", text)
            else:
                messagebox.showinfo("Desempaquetar", "Desempaquetado Finalizado !!!\n\n" + text)
        run(cmd_unpack, v, d, count_extr=True, on_done=done)

    def do_extraer_sel():
        if not state["vpk"]:
            messagebox.showinfo("Extraer Archivo(s)/Carpeta(s)", "Primero pulsa 'Leer' y elige un VPK.")
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Extraer Archivo(s)/Carpeta(s)",
                                "Selecciona un archivo o una carpeta de la lista.")
            return
        chosen = [state["rows"][i] for i in sel]
        r = chosen[0]
        d = filedialog.askdirectory(title="Carpeta donde extraer: " + (
            rec_display(r) if len(chosen) == 1 else "%d elementos" % len(chosen)),
                                    mustexist=False,
                                    initialdir=os.path.dirname(os.path.abspath(state["vpk"])))
        if not d:
            return
        items = select_many(state["recs"], chosen)
        v_extr.config(text="0")

        def done(text):
            say(text.splitlines()[-1] if text else "")
            if "ERROR" in text:
                messagebox.showwarning("Extraer Archivo(s)/Carpeta(s)", text)
            else:
                messagebox.showinfo("Extraer Archivo(s)/Carpeta(s)", "Extraccion Finalizada !!!\n\n" + text)
        run(cmd_extract_items, state["vpk"], items, d, count_extr=True, on_done=done)

    def do_pack():
        d = filedialog.askdirectory(title="Carpeta a empaquetar", mustexist=True)
        if not d:
            return
        o = filedialog.asksaveasfilename(title="Guardar VPK como", defaultextension=".vpk",
                                         initialfile="demo.vpk",
                                         filetypes=[("VPK", "*.vpk"), ("Todos", "*.*")])
        if not o:
            return

        def done(text):
            say(text.splitlines()[-1] if text else "")
            if "ERROR" in text:
                messagebox.showwarning("Empaquetar", text)
            else:
                messagebox.showinfo("Empaquetar", text)
                cargar(o)
        run(cmd_pack, d, o, on_done=done)

    def do_texdds():
        rutas = filedialog.askopenfilenames(
            title="Elegir archivos .tex y/o .dds",
            filetypes=[("Texturas", "*.tex *.dds"), ("TeX2", "*.tex"), ("DDS", "*.dds"),
                       ("Todos", "*.*")])
        if not rutas:
            return
        primera = os.path.dirname(os.path.abspath(rutas[0]))
        d = filedialog.askdirectory(
            title='Carpeta de destino (Cancelar = subcarpeta "convertidos" junto al original)',
            initialdir=primera, mustexist=False) or None

        def done(text):
            say(text.splitlines()[0] if text else "")
            if "Errores" in text or "ERROR" in text:
                messagebox.showwarning("TEX/DDS", text)
            else:
                messagebox.showinfo("TEX/DDS", text)
        run(cmd_texdds, list(rutas), d, on_done=done)

    def do_reemplazar():
        if not state["vpk"]:
            messagebox.showinfo("Reemplazar", "Primero pulsa 'Leer' y elige un VPK.")
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Reemplazar", "Selecciona un archivo de la lista.")
            return
        if len(sel) != 1:
            messagebox.showinfo("Reemplazar", "Selecciona un solo archivo para reemplazar.")
            return
        r = state["rows"][sel[0]]
        if r["type"] != 2:
            messagebox.showinfo("Reemplazar", "Eso es una carpeta, elige un archivo.")
            return
        f = filedialog.askopenfilename(title="Archivo que reemplazara a: " + rec_display(r))
        if not f:
            return
        if not messagebox.askyesno(
                "Reemplazar",
                "Reemplazar dentro de %s:\n\n  %s\n\npor:\n\n  %s\n\n"
                "Se modifica el VPK directamente (se crea una copia .bak automatica).\n\n"
                "Continuar?" % (
                    os.path.basename(state["vpk"]), rec_display(r), f)):
            return
        def done(text):
            say("Reemplazado: %s" % os.path.basename(rec_display(r)))
            if "ERROR" in text:
                messagebox.showerror("Reemplazar", text)
            else:
                messagebox.showinfo("Reemplazar", text)
        run(cmd_replace, state["vpk"], r, f, on_done=done)

    def target_dir():
        sel = tree.selection()
        if not sel:
            return None
        r = state["rows"][sel[0]]
        return (r["dir"] + r["name"] + b"\\") if r["type"] == 1 else r["dir"]

    def after_edit(titulo):
        def done(text):
            say(text.splitlines()[-1] if text else "")
            if "ERROR" in text:
                messagebox.showwarning(titulo, text)
            else:
                messagebox.showinfo(titulo, text)
            cargar(state["vpk"])
        return done

    def do_anadir():
        if not state["vpk"]:
            messagebox.showinfo("A\u00f1adir", "Primero pulsa 'Leer' y elige un VPK.")
            return
        target = target_dir()
        if target is None:
            messagebox.showinfo("A\u00f1adir", "Selecciona en la lista la carpeta de destino "
                                "(o un archivo de esa carpeta) y vuelve a pulsar A\u00f1adir.")
            return
        dest = target.decode("cp1252", errors="replace") or "(raiz)"
        q = messagebox.askyesnocancel(
            "A\u00f1adir", "Destino dentro del VPK:\n  %s\n\nSi = elegir archivos\n"
            "No = elegir una carpeta\nCancelar = salir" % dest)
        if q is None:
            return
        try:
            if q:
                files = filedialog.askopenfilenames(title="Archivos a a\u00f1adir en " + dest)
                if not files:
                    return
                adds, conf = plan_add(state["recs"], target, files=list(files))
            else:
                folder = filedialog.askdirectory(title="Carpeta a a\u00f1adir en " + dest, mustexist=True)
                if not folder:
                    return
                adds, conf = plan_add(state["recs"], target, folder=folder)
        except SystemExit as e:
            messagebox.showerror("A\u00f1adir", str(e))
            return
        if conf:
            lista = "\n".join("  " + (c["dir"] + c["name"]).decode("cp1252", errors="replace")
                              for c in conf[:10]) + ("\n  ..." if len(conf) > 10 else "")
            a = messagebox.askyesnocancel(
                "Reemplazar", "Ya existen %d archivo(s) con el mismo nombre en el VPK:\n\n%s\n\n"
                "Si = reemplazarlos\nNo = omitirlos (no a\u00f1adir esos)\nCancelar = cancelar todo"
                % (len(conf), lista))
            if a is None:
                return
            if not a:
                skip = {id(c) for c in conf}
                adds = [x for x in adds if id(x) not in skip]
        if not any(x["kind"] == "f" for x in adds):
            messagebox.showinfo("A\u00f1adir", "No hay archivos para a\u00f1adir.")
            return
        run(modify_vpk, state["vpk"], [], adds, on_done=after_edit("A\u00f1adir"))

    def do_eliminar():
        if not state["vpk"]:
            messagebox.showinfo("Eliminar", "Primero pulsa 'Leer' y elige un VPK.")
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Eliminar", "Selecciona los archivos o carpetas a eliminar.")
            return
        chosen = [state["rows"][i] for i in sel]
        items = select_many(state["recs"], chosen)
        nf = sum(1 for k, _, _ in items if k == "f")
        nd = sum(1 for k, _, _ in items if k == "d")
        lista = "\n".join("  " + rec_display(r) for r in chosen[:8]) + (
            "\n  ..." if len(chosen) > 8 else "")
        if not messagebox.askyesno(
                "Eliminar",
                "Vas a ELIMINAR del VPK:\n\n%s\n\n(%d archivo(s) y %d carpeta(s) en total, "
                "incluido el contenido de las carpetas).\n\nSe crea una copia de seguridad .bak "
                "automaticamente.\n\nContinuar?" % (lista, nf, nd)):
            return
        keys = {(x["dir"], x["name"], x["type"]) for _, x, _ in items}
        run(modify_vpk, state["vpk"], keys, [], on_done=after_edit("Eliminar"))

    # ------------------------------------------------------------ visor de texturas
    tk.Label(right, text="Visor de textura", font=("MS Sans Serif", 10, "bold"),
             anchor="w").pack(fill="x")
    viewer = tk.Frame(right, width=450, height=450, bg="#2b2b2b")
    viewer.pack(pady=(4, 4))
    viewer.pack_propagate(False)
    img_lbl = tk.Label(viewer, bg="#2b2b2b", fg="white", wraplength=420,
                       text="Selecciona un archivo .tex de la lista")
    img_lbl.pack(expand=True)
    info_lbl = tk.Label(right, text="", anchor="w", justify="left", wraplength=450)
    info_lbl.pack(fill="x")
    ign_var = tk.BooleanVar(value=False)
    vs = {"token": 0, "img": None, "rec": None}

    def show_tex(r):
        vs["token"] += 1
        tok = vs["token"]
        vs["rec"] = r
        vpk, ign = state["vpk"], ign_var.get()
        img_lbl.config(image="", text="Cargando...")

        def work():
            try:
                png, info = tex_preview(read_tex_bytes(vpk, r), 440, ign)
                res = (png, info, None)
            except Exception as e:  # noqa
                res = (None, None, str(e) or type(e).__name__)

            def apply():
                if tok != vs["token"]:
                    return
                name = os.path.basename(rec_display(r).replace("\\", "/"))
                if res[2]:
                    vs["img"] = None
                    img_lbl.config(image="", text="No se puede mostrar:\n" + res[2])
                    info_lbl.config(text=name)
                else:
                    im = tk.PhotoImage(data=base64.b64encode(res[0]).decode("ascii"))
                    vs["img"] = im
                    img_lbl.config(image=im, text="")
                    info_lbl.config(text="%s    %s" % (name, res[1]))
            root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def on_select(*_):
        sel = tree.selection()
        if not sel:
            return
        r = state["rows"].get(sel[0])
        if r is not None and r["type"] == 2 and r["name"].lower().endswith(b".tex"):
            show_tex(r)
        else:
            vs["token"] += 1
            vs["img"] = None
            img_lbl.config(image="", text="Selecciona un archivo .tex de la lista")
            info_lbl.config(text="")
    tree.bind("<<TreeviewSelect>>", on_select)
    tk.Checkbutton(right, text="Ignorar transparencia (alfa)", variable=ign_var, anchor="w",
                   command=lambda: vs["rec"] is not None and on_select()).pack(fill="x")

    say("Pulsa 'Leer' para abrir un VPK.")
    root.mainloop()


def main(argv):
    if len(argv) == 1:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            console_menu()
        else:
            gui()
        return 0
    if len(argv) >= 3 and argv[1] == "list":
        cmd_list(argv[2])
    elif len(argv) == 4 and argv[1] == "unpack":
        cmd_unpack(argv[2], argv[3])
    elif len(argv) == 4 and argv[1] == "pack":
        cmd_pack(argv[2], argv[3])
    elif argv[1] == "extract" and len(argv) == 5:
        with open(argv[2], "rb") as fh:
            recs = read_toc(fh)["recs"]
        want = argv[3].replace("/", "\\").strip("\\").lower()
        hit = [r for r in recs if rec_display(r).lower().strip("\\") == want
               or (r["type"] == 1 and rec_display(r).lower() == want)]
        if not hit:
            raise SystemExit("No se encontro en el VPK: %s" % argv[3])
        cmd_extract_items(argv[2], select_items(recs, hit[0]), argv[4])
    elif argv[1] == "texdds" and len(argv) >= 3:
        args, destino = argv[2:], None
        if "--salida" in args:
            i = args.index("--salida")
            destino = args[i + 1]
            del args[i:i + 2]
        cmd_texdds(args, destino)
    elif argv[1] == "menu":
        console_menu()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

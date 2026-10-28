#!/usr/bin/env python3
"""
vpk_tool.py - Desempaqueta / empaqueta el demo.vpk ("vtPack") de Torrente Online.

Uso (sin argumentos se abre una ventana con menu; "menu" = menu en consola):
    python vpk_tool.py
    python vpk_tool.py list    demo.vpk
    python vpk_tool.py unpack  demo.vpk  carpeta_salida
    python vpk_tool.py pack    carpeta_origen  demo_nuevo.vpk
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

MAGIC = b"vtPack"
DEFAULT_HDR_EXTRA = bytes([1, 0, 0, 0]) + bytes(12)
HEADER_SIZE = 0x22
TAG_ROOT, TAG_DIR, TAG_FILE = 0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC
MANIFEST = "_vpk_manifest.json"
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
    print("Extraidos %d archivos en %s" % (n, outdir))


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


def rec_display(r):
    """Ruta tal como la muestra el UnpaKer (dir + nombre, con \\)."""
    return (r["dir"] + r["name"]).decode("cp1252", errors="replace")


def gui():
    import threading
    import contextlib
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Unpacker Demo.vpk - Torrente On-Line")
    root.geometry("640x600")
    root.minsize(560, 480)

    FONT = ("Courier New", 10, "bold")
    state = {"vpk": None, "recs": [], "rows": {}, "busy": False}
    buttons = []

    def box(parent, text, width):
        """Etiqueta con borde + cuadro de valor, como los contadores del UnpaKer."""
        tk.Label(parent, text=text, font=("Courier New", 11, "bold"), relief="raised",
                 bd=2, bg="#f0d9b5" if text in ("Extraidos",) else "#f0f0f0",
                 padx=10).pack(side="left", padx=(6, 2))
        v = tk.Label(parent, text="", font=FONT, relief="sunken", bd=2, bg="white",
                     width=width, anchor="w")
        v.pack(side="left", padx=(0, 6))
        return v

    def mkbtn(parent, text, cmd, w=9):
        b = tk.Button(parent, text=text, width=w, command=cmd, font=("MS Sans Serif", 9, "bold"))
        buttons.append(b)
        return b

    # --- fila 1: Leer + contadores
    row1 = tk.Frame(root)
    row1.pack(fill="x", padx=6, pady=(8, 2))
    btn_leer = mkbtn(row1, "Leer", lambda: do_leer())
    btn_leer.pack(side="left")
    v_archivos = box(row1, "Archivos", 6)
    v_dirs = box(row1, "Dirs", 5)
    v_total = box(row1, "Total", 6)

    # --- fila 2: Extraer + Extraidos (+ botones extra)
    row2 = tk.Frame(root)
    row2.pack(fill="x", padx=6, pady=2)
    btn_extraer = mkbtn(row2, "Extraer", lambda: do_extraer())
    btn_extraer.pack(side="left")
    v_extr = box(row2, "Extraidos", 6)
    v_extr.config(text="0")
    mkbtn(row2, "TXD/DDS", lambda: do_texdds(), 9).pack(side="right", padx=2)
    mkbtn(row2, "Empaquetar", lambda: do_pack(), 11).pack(side="right", padx=2)

    # --- barra de progreso
    bar_frame = tk.Frame(root)
    bar_frame.pack(fill="x", padx=8, pady=(4, 2))
    bar = ttk.Progressbar(bar_frame, maximum=100, mode="determinate")
    bar.pack(fill="x")
    status = tk.Label(bar_frame, text="Pulsa 'Leer' para abrir un VPK.", anchor="w")
    status.pack(fill="x")

    # --- lista
    mid = tk.Frame(root)
    mid.pack(fill="both", expand=True, padx=6, pady=2)
    cols = ("archivo", "sizeb", "size", "offset")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
    for c, t, w, anc in (("archivo", "Archivo", 330, "w"), ("sizeb", "Size b.", 80, "w"),
                         ("size", "Size", 80, "w"), ("offset", "Offset", 80, "w")):
        tree.heading(c, text=t, anchor="w")
        tree.column(c, width=w, anchor=anc, stretch=(c == "archivo"))
    ysb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ysb.set)
    ysb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    # --- Reemplazar
    row3 = tk.Frame(root)
    row3.pack(fill="x", padx=6, pady=(4, 2))
    btn_rep = mkbtn(row3, "Reemplazar :", lambda: do_reemplazar(), 12)
    btn_rep.pack(side="left")
    ent = tk.Entry(row3, font=("Courier New", 10))
    ent.pack(side="left", fill="x", expand=True, padx=(6, 0))

    # --- leyenda amarilla
    leg = tk.Frame(root, bg="#ffffc0", bd=2, relief="groove")
    leg.pack(fill="x", padx=6, pady=(4, 8))
    tk.Label(leg, bg="#ffffc0", justify="left", anchor="w", font=("Courier New", 9),
             text=(" Si es Igual tama\u00f1o --> Se reemplaza\n"
                   " Si es Mayor tama\u00f1o --> Se reemplaza solo hasta el tama\u00f1o original\n"
                   " Si es Menor tama\u00f1o --> Se reemplaza y el resto se llena con ceros (00)")
             ).pack(fill="x", padx=4, pady=4)

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
        state["vpk"], state["recs"], state["rows"] = path, t["recs"], {}
        tree.delete(*tree.get_children())
        nf = nd = 0
        for i, r in enumerate(t["recs"]):
            if r["type"] == 2:
                nf += 1
            else:
                nd += 1
            iid = tree.insert("", "end", iid=str(i), values=(
                rec_display(r), r["size"], "%08X" % r["size"], "%08X" % r["off"]))
            state["rows"][iid] = r
        v_archivos.config(text=str(nf))
        v_dirs.config(text=str(nd))
        v_total.config(text=str(len(t["recs"])))
        v_extr.config(text="0")
        bar["value"] = 0
        root.title("UNPACKER  %s" % os.path.basename(path))
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
        d = filedialog.askdirectory(title="Carpeta donde extraer", mustexist=False,
                                    initialdir=os.path.dirname(os.path.abspath(v)))
        if not d:
            return
        v_extr.config(text="0")

        def done(text):
            say(text.splitlines()[-1] if text else "")
            if "ERROR" in text:
                messagebox.showwarning("Extraer", text)
            else:
                messagebox.showinfo("Extraer", "Extraccion Finalizada !!!\n\n" + text)
        run(cmd_unpack, v, d, count_extr=True, on_done=done)

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
                messagebox.showwarning("TXD/DDS", text)
            else:
                messagebox.showinfo("TXD/DDS", text)
        run(cmd_texdds, list(rutas), d, on_done=done)

    def do_reemplazar():
        if not state["vpk"]:
            messagebox.showinfo("Reemplazar", "Primero pulsa 'Leer' y elige un VPK.")
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Reemplazar", "Selecciona un archivo de la lista.")
            return
        r = state["rows"][sel[0]]
        if r["type"] != 2:
            messagebox.showinfo("Reemplazar", "Eso es una carpeta, elige un archivo.")
            return
        f = filedialog.askopenfilename(title="Archivo que reemplazara a: " + rec_display(r))
        if not f:
            return
        ent.delete(0, "end")
        ent.insert(0, f)
        if not messagebox.askyesno(
                "Reemplazar",
                "Reemplazar dentro de %s:\n\n  %s\n\npor:\n\n  %s\n\n"
                "Se modifica el VPK directamente. Hiciste copia de seguridad?" % (
                    os.path.basename(state["vpk"]), rec_display(r), f)):
            return
        try:
            w, orig, new = replace_in_vpk(state["vpk"], r, f)
        except Exception as e:  # noqa
            messagebox.showerror("Reemplazar", "%s: %s" % (type(e).__name__, e))
            return
        if new == orig:
            caso = "igual tamano"
        elif new > orig:
            caso = "MAYOR: se escribio solo hasta el tamano original (%d de %d bytes)" % (w, new)
        else:
            caso = "menor: rellenado con ceros (%d bytes)" % (orig - new)
        say("Reemplazado: %s  (%s)" % (os.path.basename(rec_display(r)), caso))
        messagebox.showinfo("Reemplazar", "Reemplazado: %s\nPor: %s\n\nTamano %s" % (
            rec_display(r), f, caso))

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

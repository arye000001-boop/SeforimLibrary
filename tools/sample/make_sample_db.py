#!/usr/bin/env python3
"""
Build a small, self-consistent sample of the Seforim (Otzaria) SQLite library.

* Same schema as the full DB (taken verbatim from its sqlite_master), IDs unchanged.
* Whole books only. A link is kept only if BOTH of its books are kept, so every
  foreign key stays valid and commentary/targum/reference links really work.
* Books are chosen greedily to cover as many "kinds" as possible (categories,
  dependence types, connection types, sources, teamim/nekudot, alt-TOC,
  versions, notes ...) and then to pull in the commentaries of what was chosen,
  all under a size budget (calibrated by actually building and measuring).

usage: make_sample_db.py --src seforim.db --out sample.db --target-mb 50
"""
import argparse, collections, json, math, os, sqlite3, sys, time

T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:7.1f}s]", *a, flush=True)


# ───────────────────────────── statistics ─────────────────────────────
def gather(src):
    log("reading categories / books")
    cat_parent, cat_title = {}, {}
    for i, p, t in src.execute("SELECT id,parentId,title FROM category"):
        cat_parent[i], cat_title[i] = p, t

    def chain(cid):
        out = []
        while cid is not None and cid in cat_parent and len(out) < 50:
            out.append(cid)
            cid = cat_parent[cid]
        return out[::-1]  # root .. leaf

    books = {}
    for (i, cid, sid, title, tl, dep, tea, nek, alt, base) in src.execute(
        "SELECT id,categoryId,sourceId,title,totalLines,dependenceType,hasTeamim,"
        "hasNekudot,hasAltStructures,isBaseBook FROM book"
    ):
        ch = chain(cid)
        books[i] = dict(id=i, cat=cid, src=sid, title=title, lines=tl, dep=dep,
                        teamim=tea, nekudot=nek, alt=alt, base=base,
                        top=ch[0] if ch else None,
                        sub=ch[1] if len(ch) > 1 else None,
                        bytes=0, links=0, feats=set())
    log(f"  {len(books)} books, {len(cat_parent)} categories")

    log("measuring stored bytes per book")
    line_cols = {r[1] for r in src.execute("PRAGMA table_info(line)")}
    has_split = bool(src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_content'").fetchone())
    if "content" in line_cols:
        q = "SELECT bookId,COUNT(*),COALESCE(SUM(LENGTH(content)),0) FROM line GROUP BY bookId"
    elif has_split:
        # real schema-7 layout: line_content(id, content) holds the (zstd-compressed) text
        q = ("SELECT l.bookId,COUNT(*),COALESCE(SUM(LENGTH(c.content)),0) "
             "FROM line l LEFT JOIN line_content c ON c.id=l.id GROUP BY l.bookId")
    else:
        sys.exit("cannot find where line text is stored")
    log("  layout:", "line.content" if "content" in line_cols else "line + line_content")
    for b, n, s in src.execute(q):
        if b in books:
            # + per-line row/index overhead (line, line_ref, line_toc, indexes)
            books[b]["bytes"] = int(s) + 90 * n
            books[b]["nlines"] = n

    log("reading connection types and link pairs (full scan of link table)")
    ctype = dict(src.execute("SELECT id,name FROM connection_type"))
    nbr = collections.defaultdict(lambda: collections.defaultdict(int))
    btypes = collections.defaultdict(set)
    for a, b, t, n in src.execute(
        "SELECT sourceBookId,targetBookId,connectionTypeId,COUNT(*) FROM link GROUP BY 1,2,3"
    ):
        if a not in books or b not in books or a == b:
            if a in books:
                books[a]["links"] += n
            continue
        nbr[a][b] += n
        nbr[b][a] += n
        books[a]["links"] += n
        books[b]["links"] += n
        btypes[a].add(t)
        btypes[b].add(t)

    log("reading versions / notes")
    with_versions = {r[0] for r in src.execute("SELECT DISTINCT bookId FROM book_version")}
    with_notes = {r[0] for r in src.execute(
        "SELECT DISTINCT l.bookId FROM line_note n JOIN line l ON l.id=n.lineId")}

    # feature sets: what a book "adds" to the sample's variety
    W = {}
    for b in books.values():
        f = set()
        if b["top"] is not None: f.add(("cat1", b["top"]))
        if b["sub"] is not None: f.add(("cat2", b["sub"]))
        if b["dep"]: f.add(("dep", str(b["dep"]).lower()))
        f.add(("src", b["src"]))
        if b["teamim"]: f.add(("teamim",))
        if b["nekudot"]: f.add(("nekudot",))
        if b["alt"]: f.add(("alt",))
        if b["id"] in with_versions: f.add(("versions",))
        if b["id"] in with_notes: f.add(("notes",))
        if b["base"]: f.add(("base",))
        for t in btypes[b["id"]]:
            f.add(("conn", t))
        b["feats"] = f
    for f in ("cat1", "cat2", "dep", "src", "conn"):
        pass
    weights = {"cat1": 8, "cat2": 5, "dep": 6, "src": 6, "teamim": 5, "nekudot": 5,
               "alt": 4, "versions": 4, "notes": 5, "base": 1, "conn": 4}
    return books, nbr, ctype, weights, chain, cat_parent


# ───────────────────────────── selection ─────────────────────────────
LINK_BYTES = 120      # rough on-disk cost of a link incl. coverage/indexes
LINE_FACTOR = 1.5     # lines + toc + indexes overhead


def select(books, nbr, weights, budget, max_book_bytes):
    cand = [b for b in books.values()
            if b.get("nlines", 0) > 0 and b["bytes"] <= max_book_bytes]
    covered = set()
    selected = []
    aff = collections.defaultdict(int)   # links between candidate and selected set
    spent = 0.0
    alive = {b["id"]: b for b in cand}
    while alive:
        best, best_score = None, -1.0
        for b in alive.values():
            cost = max(b["bytes"], 3000) * LINE_FACTOR + aff[b["id"]] * LINK_BYTES
            if spent + cost > budget:
                continue
            cov = sum(weights[f[0]] for f in b["feats"] if f not in covered)
            hub = 0.6 * math.log1p(b["links"])
            link_gain = 3.0 * math.log1p(aff[b["id"]])
            score = (cov + hub + link_gain) / cost
            if score > best_score:
                best, best_score = b, score
        if best is None:
            break
        cost = max(best["bytes"], 3000) * LINE_FACTOR + aff[best["id"]] * LINK_BYTES
        spent += cost
        selected.append(best["id"])
        covered |= best["feats"]
        del alive[best["id"]]
        for n, c in nbr[best["id"]].items():
            aff[n] += c
    return selected, spent


# ───────────────────────────── build ─────────────────────────────
HANDLED = {
    "schema_meta", "category", "category_closure", "author", "topic", "pub_place",
    "pub_date", "source", "book", "book_pub_place", "book_pub_date", "book_topic",
    "book_author", "book_base_text", "generation", "book_generation", "line",
    "line_ref", "line_dh", "tocText", "tocEntry", "connection_type", "link",
    "link_anchor", "link_range", "link_coverage", "link_suppressed_side",
    "book_has_links", "book_version", "version_line", "line_toc",
    "alt_toc_structure", "alt_toc_entry", "line_alt_toc", "book_acronym",
    "default_commentator", "default_targum", "banner", "book_banner",
    "book_protection", "line_divine_name", "line_note", "line_note_divine_name",
    "line_content", "zstd_dict",
}
FLAG_TYPES = {"hasTargumConnection": "TARGUM", "hasReferenceConnection": "REFERENCE",
              "hasSourceConnection": "SOURCE", "hasCommentaryConnection": "COMMENTARY",
              "hasOtherConnection": "OTHER"}


def build(src_path, out_path, sel, chain, cat_parent, label):
    if os.path.exists(out_path):
        os.remove(out_path)
    d = sqlite3.connect("file:" + out_path + "?mode=rwc", uri=True)
    d.execute("PRAGMA journal_mode=OFF")
    d.execute("PRAGMA synchronous=OFF")
    d.execute("PRAGMA foreign_keys=OFF")
    d.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro&immutable=1",))

    objs = d.execute("SELECT type,name,sql FROM src.sqlite_master "
                     "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
    tables = [o for o in objs if o[0] == "table"]
    later = [o for o in objs if o[0] in ("index", "view", "trigger")]
    for _, name, sql in tables:
        d.execute(sql)
    unhandled = [n for _, n, _ in tables if n not in HANDLED]
    if unhandled:
        log("note: tables unknown to the sampler:", unhandled)

    d.execute("CREATE TEMP TABLE sel_book(id INTEGER PRIMARY KEY)")
    d.executemany("INSERT INTO sel_book VALUES(?)", [(i,) for i in sel])
    cats = set()
    for (cid,) in d.execute("SELECT DISTINCT categoryId FROM src.book WHERE id IN (SELECT id FROM sel_book)"):
        cats.update(chain(cid))
    d.execute("CREATE TEMP TABLE sel_cat(id INTEGER PRIMARY KEY)")
    d.executemany("INSERT INTO sel_cat VALUES(?)", [(i,) for i in cats])

    SB = "(SELECT id FROM sel_book)"
    def cp(table, where=""):
        t = time.time()
        d.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table} {('WHERE ' + where) if where else ''}")
        n = d.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        log(f"  {table:24s} {n:>9d} rows  ({time.time() - t:.1f}s)")

    log(f"[{label}] copying {len(sel)} books")
    cp("category", "id IN (SELECT id FROM sel_cat)")
    cp("category_closure", "ancestorId IN (SELECT id FROM sel_cat) AND descendantId IN (SELECT id FROM sel_cat)")
    cp("book", f"id IN {SB}")
    cp("source", "id IN (SELECT sourceId FROM main.book)")
    for j, ent, col in (("book_author", "author", "authorId"), ("book_topic", "topic", "topicId"),
                        ("book_pub_place", "pub_place", "pubPlaceId"), ("book_pub_date", "pub_date", "pubDateId")):
        cp(j, f"bookId IN {SB}")
        cp(ent, f"id IN (SELECT {col} FROM main.{j})")
    cp("book_base_text", f"bookId IN {SB} AND baseBookId IN {SB}")
    cp("generation")
    cp("book_generation", f"bookId IN {SB}")
    cp("line", f"bookId IN {SB}")
    if any(n == "line_content" for _, n, _ in tables):
        cp("line_content", "id IN (SELECT id FROM main.line)")
    if any(n == "zstd_dict" for _, n, _ in tables):
        cp("zstd_dict")
    cp("line_ref", f"bookId IN {SB}")
    cp("line_dh", f"bookId IN {SB}")
    cp("tocEntry", f"bookId IN {SB}")
    cp("alt_toc_structure", f"bookId IN {SB}")
    cp("alt_toc_entry", "structureId IN (SELECT id FROM main.alt_toc_structure)")
    cp("tocText", "id IN (SELECT textId FROM main.tocEntry UNION SELECT textId FROM main.alt_toc_entry)")
    cp("line_toc", "lineId IN (SELECT id FROM main.line)")
    cp("line_alt_toc", "lineId IN (SELECT id FROM main.line)")
    cp("connection_type")
    cp("link", f"sourceBookId IN {SB} AND targetBookId IN {SB}")
    for t in ("link_anchor", "link_range", "link_coverage", "link_suppressed_side"):
        cp(t, "linkId IN (SELECT id FROM main.link)")
    cp("book_has_links", f"bookId IN {SB}")
    cp("book_version", f"bookId IN {SB}")
    cp("version_line", "versionId IN (SELECT id FROM main.book_version)")
    cp("book_acronym", f"bookId IN {SB}")
    cp("default_commentator", f"bookId IN {SB} AND commentatorBookId IN {SB}")
    cp("default_targum", f"bookId IN {SB} AND targumBookId IN {SB}")
    cp("book_banner", f"bookId IN {SB}")
    cp("banner", "id IN (SELECT bannerId FROM main.book_banner)")
    cp("book_protection", f"bookId IN {SB}")
    cp("line_divine_name", "lineId IN (SELECT id FROM main.line)")
    cp("line_note", "lineId IN (SELECT id FROM main.line)")
    cp("line_note_divine_name", "lineId IN (SELECT id FROM main.line)")
    cp("schema_meta")

    for n in unhandled:  # unknown but small (dictionaries, lookups): copy whole; big ones are skipped loudly
        cnt = d.execute(f"SELECT COUNT(*) FROM src.\"{n}\"").fetchone()[0]
        if cnt <= 20000:
            cp(f'"{n}"')
        else:
            log(f"WARNING: unknown table {n} has {cnt} rows - NOT copied")

    log("creating indexes / views / triggers")
    for _, name, sql in later:
        d.execute(sql)

    log("fixing derived flags so the UI never offers links that are not in the sample")
    d.execute("""UPDATE book_has_links SET
        hasSourceLinks = CASE WHEN hasSourceLinks=1 AND EXISTS(SELECT 1 FROM link WHERE sourceBookId=book_has_links.bookId) THEN 1 ELSE 0 END,
        hasTargetLinks = CASE WHEN hasTargetLinks=1 AND EXISTS(SELECT 1 FROM link WHERE targetBookId=book_has_links.bookId) THEN 1 ELSE 0 END""")
    names = {r[0].upper() for r in d.execute("SELECT name FROM connection_type")}
    for col, tname in FLAG_TYPES.items():
        if tname in names:
            d.execute(f"""UPDATE book SET {col}=0 WHERE {col}=1 AND NOT EXISTS(
                SELECT 1 FROM link l JOIN connection_type c ON c.id=l.connectionTypeId
                WHERE upper(c.name)=? AND (l.sourceBookId=book.id OR l.targetBookId=book.id))""", (tname,))

    uv = d.execute("PRAGMA src.user_version").fetchone()[0]
    d.commit()
    d.execute("DETACH DATABASE src")
    d.execute(f"PRAGMA user_version={uv}")
    d.execute("ANALYZE")
    d.commit()
    d.execute("VACUUM")
    d.close()
    size = os.path.getsize(out_path)
    log(f"[{label}] built {out_path}: {size / 1e6:.1f} MB")
    return size


def verify(out_path):
    d = sqlite3.connect(out_path)
    d.execute("PRAGMA foreign_keys=ON")
    ic = d.execute("PRAGMA integrity_check").fetchone()[0]
    fk = d.execute("PRAGMA foreign_key_check").fetchall()
    log(f"integrity_check={ic}  foreign_key_violations={len(fk)}")
    if fk:
        log("  first violations:", fk[:5])
    if d.execute("SELECT 1 FROM sqlite_master WHERE name='line_content'").fetchone():
        miss = d.execute("SELECT COUNT(*) FROM line l LEFT JOIN line_content c ON c.id=l.id WHERE c.id IS NULL").fetchone()[0]
        orph = d.execute("SELECT COUNT(*) FROM line_content c LEFT JOIN line l ON l.id=c.id WHERE l.id IS NULL").fetchone()[0]
        log(f"lines without content={miss}  orphan content rows={orph}")
        if miss or orph:
            fk = fk + [("line_content", miss, orph)]
    meta = dict(d.execute("SELECT key,value FROM schema_meta"))
    log("schema_meta:", {k: (v if len(v) < 60 else v[:57] + '...') for k, v in meta.items()})
    ok = ic == "ok" and not fk
    return ok, d


def report(d, ctype_by_id, chain_titles, out_json, sel_books):
    r = {}
    r["books"] = d.execute("SELECT COUNT(*) FROM book").fetchone()[0]
    r["lines"] = d.execute("SELECT COUNT(*) FROM line").fetchone()[0]
    r["links"] = d.execute("SELECT COUNT(*) FROM link").fetchone()[0]
    r["links_by_type"] = {n: c for n, c in d.execute(
        "SELECT c.name,COUNT(*) FROM link l JOIN connection_type c ON c.id=l.connectionTypeId GROUP BY 1 ORDER BY 2 DESC")}
    r["books_by_top_category"] = {n: c for n, c in d.execute(
        "SELECT t.title,COUNT(*) FROM book b JOIN category_closure cc ON cc.descendantId=b.categoryId "
        "JOIN category t ON t.id=cc.ancestorId AND t.parentId IS NULL GROUP BY 1 ORDER BY 2 DESC")}
    r["books_by_dependence"] = {str(n): c for n, c in d.execute(
        "SELECT dependenceType,COUNT(*) FROM book GROUP BY 1 ORDER BY 2 DESC")}
    for k, q in (("with_teamim", "hasTeamim=1"), ("with_nekudot", "hasNekudot=1"),
                 ("with_alt_toc", "hasAltStructures=1")):
        r[k] = d.execute(f"SELECT COUNT(*) FROM book WHERE {q}").fetchone()[0]
    r["with_versions"] = d.execute("SELECT COUNT(DISTINCT bookId) FROM book_version").fetchone()[0]
    r["line_notes"] = d.execute("SELECT COUNT(*) FROM line_note").fetchone()[0]
    r["link_anchors"] = d.execute("SELECT COUNT(*) FROM link_anchor").fetchone()[0]
    r["link_ranges"] = d.execute("SELECT COUNT(*) FROM link_range").fetchone()[0]
    r["titles"] = [t for (t,) in d.execute("SELECT title FROM book ORDER BY orderIndex,title")]
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    log("report ->", out_json)
    for k, v in r.items():
        if k != "titles":
            log(f"  {k}: {v}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-mb", type=float, default=50)
    ap.add_argument("--hard-mb", type=float, default=100)
    ap.add_argument("--max-book-frac", type=float, default=0.10,
                    help="skip books whose line bytes exceed this fraction of the target")
    ap.add_argument("--attempts", type=int, default=4)
    a = ap.parse_args()

    src = sqlite3.connect(f"file:{a.src}?mode=ro&immutable=1", uri=True)
    books, nbr, ctype, weights, chain, cat_parent = gather(src)
    src.close()

    target = a.target_mb * 1e6
    max_book = target * a.max_book_frac
    budget = target * 0.6
    best = None  # (size, path)
    tmp = a.out + ".try"
    for attempt in range(1, a.attempts + 1):
        sel, est = select(books, nbr, weights, budget, max_book)
        log(f"attempt {attempt}: budget {budget / 1e6:.1f} MB -> {len(sel)} books (estimate {est / 1e6:.1f} MB)")
        size = build(a.src, tmp, sel, chain, cat_parent, f"try{attempt}")
        ok = size <= target * 1.02
        if ok and (best is None or size > best[0]):
            if best:
                os.remove(best[1])
            keep = f"{a.out}.best{attempt}"
            os.replace(tmp, keep)
            best = (size, keep)
        elif not ok and best is None and attempt == a.attempts:
            keep = f"{a.out}.best{attempt}"
            os.replace(tmp, keep)
            best = (size, keep)
        elif os.path.exists(tmp):
            os.remove(tmp)
        if target * 0.9 <= size <= target * 1.02:
            break
        budget *= max(0.4, min(2.5, (target * 0.97) / size))

    if best is None:
        sys.exit("could not build a sample")
    os.replace(best[1], a.out)
    ok, d = verify(a.out)
    report(d, ctype, None, os.path.splitext(a.out)[0] + "_report.json", None)
    d.close()
    log(f"FINAL {a.out}: {os.path.getsize(a.out) / 1e6:.1f} MB (limit {a.hard_mb} MB)  consistent={ok}")
    if not ok or os.path.getsize(a.out) > a.hard_mb * 1e6:
        sys.exit(2)


if __name__ == "__main__":
    main()

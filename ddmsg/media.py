import hashlib
from pathlib import Path
import uuid

import cache
from .store import now


def file_type(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if header.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if header.startswith(b"PK\x03\x04"):
        return ".zip", "application/zip"
    return ".bin", "application/octet-stream"


def list_media(app, ref=None):
    clause, params = (" WHERE m.message_ref=?", (ref,)) if ref else ("", ())
    rows = app.db.execute("""SELECT m.*,a.path,a.size,a.mime,a.analysis,a.analysis_model FROM media m
      LEFT JOIN assets a ON a.hash=m.asset_hash""" + clause + " ORDER BY m.id LIMIT 100", params).fetchall()
    return {"items": [dict(row) for row in rows]}


def download(app, media_id):
    row = app.db.execute("SELECT m.*,i.cid,i.mid FROM media m JOIN message_index i ON i.id=m.message_ref WHERE m.id=?", (media_id,)).fetchone()
    if not row:
        raise ValueError("Unknown media ID; use media list --message REF")
    with cache.locked(app.directory):
        if row["asset_hash"]:
            asset = app.db.execute("SELECT * FROM assets WHERE hash=?", (row["asset_hash"],)).fetchone()
            if asset and Path(asset["path"]).is_file():
                return {"cached": True, "media_id": media_id, **dict(asset)}
        folder = app.directory / "media"
        folder.mkdir(exist_ok=True, mode=0o700)
        temporary = folder / (uuid.uuid4().hex + ".part")
        try:
            app.client.download(row["cid"], row["mid"], row["resource_id"], temporary)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("DWS reported success but did not write a nonempty file")
            digest = hashlib.sha256()
            with temporary.open("rb") as handle:
                header = handle.read(32)
                digest.update(header)
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            suffix, mime = file_type(header)
            hexdigest = digest.hexdigest()
            target = folder / (hexdigest + suffix)
            if target.exists():
                temporary.unlink()
            else:
                temporary.replace(target)
            with app.db:
                app.db.execute("INSERT OR IGNORE INTO assets(hash,path,size,mime) VALUES (?,?,?,?)", (hexdigest, str(target), target.stat().st_size, mime))
                app.db.execute("UPDATE media SET asset_hash=?,last_error=NULL WHERE id=?", (hexdigest, media_id))
            return {"cached": False, "media_id": media_id, "hash": hexdigest, "path": str(target), "mime": mime, "size": target.stat().st_size}
        except (OSError, RuntimeError) as exc:
            with app.db:
                app.db.execute("UPDATE media SET last_error=? WHERE id=?", (str(exc)[:2000], media_id))
            raise
        finally:
            temporary.unlink(missing_ok=True)


def annotate(app, media_id, text_file, model):
    text = Path(text_file).read_text(encoding="utf-8")
    if len(text) > 64000:
        raise ValueError("Analysis text exceeds 64000 characters")
    row = app.db.execute("SELECT asset_hash FROM media WHERE id=?", (media_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError("Download the media before saving an analysis")
    with cache.locked(app.directory), app.db:
        app.db.execute("UPDATE assets SET analysis=?,analysis_model=?,analysis_at=? WHERE hash=?", (text, model, now(), row[0]))
    return {"media_id": media_id, "analysis_saved": True, "hash": row[0]}

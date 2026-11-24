#!/usr/bin/env python3
"""Local SSD Video Culler — zero-dependency server for rapid preview & 1-click copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure Finder / LaunchServices empty environments still find Homebrew ffmpeg and UTF-8 locales
for _bin_dir in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _bin_dir not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{_bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
os.environ.setdefault("LANG", "en_US.UTF-8")
os.environ.setdefault("LC_ALL", "en_US.UTF-8")

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".webm", ".mpg", ".insv", ".3gp"}
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "node_modules", "__pycache__", "venv", ".venv"}
MIME_OVERRIDES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}

SYSTEM_VOLUMES = {"Macintosh HD", "Macintosh HD - Data", "Recovery", "Preboot", "VM", "Update"}


def detect_default_source() -> str:
    volumes = Path("/Volumes")
    if volumes.is_dir():
        for entry in sorted(volumes.iterdir()):
            if entry.name.startswith(".") or entry.name in SYSTEM_VOLUMES or entry.is_symlink():
                continue
            if entry.is_dir():
                return str(entry)
    return os.path.expanduser("~/Movies")


DEFAULT_SOURCE = detect_default_source()
DEFAULT_DEST = os.path.expanduser("~/Desktop/Selected_Videos")
CACHE_DIR = Path(os.path.expanduser("~/.cache/ssd-video-culler"))
THUMB_DIR = CACHE_DIR / "thumbs"
STATE_FILE = CACHE_DIR / "state.json"
STATIC_INDEX = Path(__file__).with_name("index.html")


def safe_folder_prefix(rel_folder: str) -> str:
    if not rel_folder or rel_folder == ".":
        return "root"
    cleaned = re.sub(r"[:/\\]+", "-", rel_folder).strip(" .-_")
    return cleaned.replace(os.sep, "__") or "folder"


def resolve_dest_path(dest_dir: str, src_path: str, rel_folder: str, copied_map: dict[str, str]) -> str:
    """Pick a collision-free destination path without overwriting unrelated clips."""
    existing = copied_map.get(src_path)
    if existing and os.path.exists(existing) and os.path.dirname(os.path.abspath(existing)) == os.path.abspath(dest_dir):
        return existing

    name = os.path.basename(src_path)
    candidate = os.path.join(dest_dir, name)
    if not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(name)
    prefixed_stem = f"{safe_folder_prefix(rel_folder)}__{stem}"
    candidate = os.path.join(dest_dir, f"{prefixed_stem}{ext}")
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{prefixed_stem}_{counter}{ext}")
        counter += 1
    return candidate


def canonical_video_names(fname: str) -> set[str]:
    """Extract canonical lowercase filenames from exact, Finder (' 2.MOV'), or Culler ('Folder__') names."""
    low = fname.lower()
    stem, ext = os.path.splitext(low)
    names = {low}
    # Finder duplicate suffix: 'DSCF0018 2.MOV', 'clip copy.mov', 'clip copy 2.mov'
    stripped_finder = re.sub(r"(?: copy)?(?: \d+)?$", "", stem)
    if stripped_finder:
        names.add(f"{stripped_finder}{ext}")
    # Culler collision prefix: 'Alishan__DSCF1225.MOV' or 'Alishan__DSCF1225_2.MOV'
    if "__" in stem:
        tail = stem.split("__")[-1]
        if tail:
            names.add(f"{tail}{ext}")
            tail_base = re.sub(r"_\d+$", "", tail)
            if tail_base:
                names.add(f"{tail_base}{ext}")
    return names


def is_real_video_file(fname: str, ext: str, full_path: str) -> bool:
    """Reject TypeScript .mts / .d.mts source files that collide with AVCHD .MTS extension."""
    if ext != ".mts":
        return True
    if fname.lower().endswith(".d.mts"):
        return False
    try:
        with open(full_path, "rb") as fh:
            hdr = fh.read(5)
        # MPEG-TS (188B) has 0x47 at byte 0; BDAV AVCHD .MTS (192B) has 0x47 at byte 4
        return (len(hdr) >= 1 and hdr[0] == 0x47) or (len(hdr) >= 5 and hdr[4] == 0x47)
    except OSError:
        return False


def scan_dest_files(dest_root: str) -> list[dict]:
    """Scan destination folder FIRST so pre-existing files with matching names mark source clips as copied."""
    root = os.path.abspath(dest_root)
    if not os.path.isdir(root):
        return []

    entries: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        depth = 0 if os.path.abspath(dirpath) == root else 1
        for fname in sorted(filenames):
            if fname.startswith(".") or fname.endswith(".culler_part"):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            if st.st_size <= 0 or not is_real_video_file(fname, ext, full_path):
                continue
            entries.append(
                {
                    "path": full_path,
                    "real": os.path.realpath(full_path),
                    "name_lower": fname.lower(),
                    "canon_names": canonical_video_names(fname),
                    "size": st.st_size,
                    "depth": depth,
                }
            )
    entries.sort(key=lambda e: (e["depth"], e["name_lower"]))
    return entries


def reconcile_items_with_dest(
    items: list[dict],
    dest_root: str,
    copied_map: dict[str, str],
    queued_srcs: set[str],
) -> None:
    """Scan destination directory first and mark matching source videos as copied."""
    dest_entries = scan_dest_files(dest_root)
    real_dest_root = os.path.realpath(dest_root) if os.path.isdir(dest_root) else os.path.abspath(dest_root)

    by_canon_and_size: dict[tuple[str, int], list[dict]] = {}
    for entry in dest_entries:
        for cname in entry["canon_names"]:
            by_canon_and_size.setdefault((cname, entry["size"]), []).append(entry)

    claimed_dest_reals: set[str] = set()
    matched_for_src: dict[str, str] = {}

    # Pass 0: Explicit state.json mapping inside the active dest folder
    if os.path.isdir(dest_root):
        for it in items:
            mapped = copied_map.get(it["path"])
            if not mapped or not os.path.isfile(mapped):
                continue
            real_mapped = os.path.realpath(mapped)
            try:
                inside = os.path.commonpath([real_mapped, real_dest_root]) == real_dest_root
            except ValueError:
                inside = False
            if inside:
                matched_for_src[it["path"]] = mapped
                claimed_dest_reals.add(real_mapped)

    # Pass 1: Exact (canonical filename + byte size) match — disambiguates duplicate camera names across folders
    for it in items:
        if it["path"] in matched_for_src:
            continue
        candidates = by_canon_and_size.get((it["name"].lower(), it["size"]))
        if not candidates:
            continue
        chosen = next((c for c in candidates if c["real"] not in claimed_dest_reals), candidates[0])
        matched_for_src[it["path"]] = chosen["path"]
        claimed_dest_reals.add(chosen["real"])

    # Pass 2: Same filename in dest (when size differs, e.g. re-exported/trimmed clip not claimed in Pass 1)
    unclaimed_by_name: dict[str, dict] = {}
    for entry in dest_entries:
        if entry["real"] not in claimed_dest_reals and entry["name_lower"] not in unclaimed_by_name:
            unclaimed_by_name[entry["name_lower"]] = entry

    for it in items:
        if it["path"] in matched_for_src:
            continue
        entry = unclaimed_by_name.get(it["name"].lower())
        if entry:
            matched_for_src[it["path"]] = entry["path"]

    for it in items:
        src_path = it["path"]
        if src_path in queued_srcs:
            it["status"] = "queued"
            it["destPath"] = None
        elif src_path in matched_for_src:
            it["status"] = "copied"
            it["destPath"] = matched_for_src[src_path]
        else:
            it["status"] = "idle"
            it["destPath"] = None


def scan_videos(
    source_root: str,
    dest_root: str,
    copied_map: dict[str, str],
    queued_srcs: set[str],
) -> list[dict]:
    """Scan dest_root first, then scan source_root read-only, ignoring ExFAT AppleDouble (._*) & system dirs."""
    root = os.path.abspath(source_root)
    if not os.path.isdir(root):
        return []

    real_dest = os.path.realpath(dest_root) if dest_root and os.path.isdir(dest_root) else None
    items: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in SKIP_DIRS
            and (not real_dest or os.path.realpath(os.path.join(dirpath, d)) != real_dest)
        )
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            if st.st_size <= 0 or not is_real_video_file(fname, ext, full_path):
                continue
            rel = os.path.relpath(full_path, root)
            rel_folder = os.path.dirname(rel) or "."
            top_folder = rel.split(os.sep)[0] if os.sep in rel else "."
            vid_id = hashlib.sha1(full_path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            items.append(
                {
                    "id": vid_id,
                    "name": fname,
                    "path": full_path,
                    "rel": rel,
                    "folder": rel_folder,
                    "topFolder": top_folder,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "ext": ext,
                    "status": "idle",
                    "destPath": None,
                }
            )
    reconcile_items_with_dest(items, dest_root, copied_map, queued_srcs)
    return items


class CullerApp:
    def __init__(
        self,
        source: str | None = None,
        dest: str | None = None,
        cache_dir: Path = CACHE_DIR,
        lazy_scan: bool = False,
    ):
        self.cache_dir = cache_dir
        self.thumb_dir = cache_dir / "thumbs"
        self.state_file = cache_dir / "state.json"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

        saved = self._load_saved()
        self.source = os.path.abspath(source or saved.get("source") or DEFAULT_SOURCE)
        if not os.path.isdir(self.source) and os.path.isdir(DEFAULT_SOURCE):
            self.source = os.path.abspath(DEFAULT_SOURCE)
        self.dest = os.path.abspath(dest or saved.get("dest") or DEFAULT_DEST)
        # shortcut: state lives in local JSON, upgrade to sqlite if >50k clips
        self.copied_map: dict[str, str] = saved.get("copied", {})

        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.initial_scan_done = False
        self.items_by_id: dict[str, dict] = {}
        self.items_list: list[dict] = []
        self.queued_srcs: set[str] = set()
        self.active_copy: str | None = None
        self.errors: list[str] = []

        self.thumb_sem = threading.BoundedSemaphore(6)
        self.copy_queue: queue.Queue[tuple[str, str, str] | None] = queue.Queue()
        self.worker = threading.Thread(target=self._copy_worker, daemon=True)
        self.worker.start()

        if not lazy_scan:
            self.rescan()

    def _load_saved(self) -> dict:
        try:
            return json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        with self.lock:
            payload = {
                "source": self.source,
                "dest": self.dest,
                "copied": self.copied_map,
            }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.state_file)

    def rescan(self) -> list[dict]:
        with self.scan_lock:
            with self.lock:
                src = self.source
                dst = self.dest
                cmap = dict(self.copied_map)
                qset = set(self.queued_srcs)
            scanned = scan_videos(src, dst, cmap, qset)
            with self.lock:
                self.items_list = scanned
                self.items_by_id = {it["id"]: it for it in scanned}
                self.initial_scan_done = True
            return scanned

    def ensure_scanned_and_reconciled(self) -> None:
        if not self.initial_scan_done:
            self.rescan()
        else:
            self.reconcile_dest()

    def reconcile_dest(self) -> None:
        """Fast (~2ms) destination-first re-check without re-walking the USB SSD."""
        with self.lock:
            reconcile_items_with_dest(self.items_list, self.dest, self.copied_map, self.queued_srcs)

    def set_config(self, source: str | None, dest: str | None) -> None:
        rescan_needed = False
        reconcile_needed = False
        with self.lock:
            if source:
                new_src = os.path.abspath(os.path.expanduser(source.strip()))
                if not os.path.isdir(new_src):
                    raise ValueError(f"Thư mục nguồn không tồn tại: {new_src}")
                if new_src != self.source:
                    self.source = new_src
                    rescan_needed = True
            if dest:
                new_dest = os.path.abspath(os.path.expanduser(dest.strip()))
                if os.path.realpath(new_dest) == os.path.realpath(self.source):
                    raise ValueError("Thư mục đích không được trùng với thư mục nguồn.")
                if new_dest != self.dest:
                    self.dest = new_dest
                reconcile_needed = True
        self._save_state()
        if rescan_needed:
            self.rescan()
        elif reconcile_needed:
            self.reconcile_dest()

    def enqueue_copy(self, vid_id: str) -> dict:
        with self.lock:
            item = self.items_by_id.get(vid_id)
            if not item:
                raise KeyError("Không tìm thấy video.")
            src_path = item["path"]
            if os.path.realpath(self.dest) == os.path.realpath(os.path.dirname(src_path)):
                raise ValueError("Thư mục đích trùng với thư mục chứa video gốc.")
            existing = item.get("destPath") or self.copied_map.get(src_path)
            if (
                existing
                and os.path.isfile(existing)
                and os.path.isdir(self.dest)
                and os.path.commonpath([os.path.realpath(existing), os.path.realpath(self.dest)])
                == os.path.realpath(self.dest)
            ):
                item["status"] = "copied"
                item["destPath"] = existing
                return {"ok": True, "status": "copied", "destPath": existing}
            if src_path in self.queued_srcs:
                return {"ok": True, "status": "queued", "destPath": None}

            os.makedirs(self.dest, exist_ok=True)
            dest_path = resolve_dest_path(self.dest, src_path, item["folder"], self.copied_map)
            self.queued_srcs.add(src_path)
            item["status"] = "queued"
            self.copy_queue.put((vid_id, src_path, dest_path))
            return {"ok": True, "status": "queued", "destPath": dest_path}

    def uncopy(self, vid_id: str) -> dict:
        with self.lock:
            item = self.items_by_id.get(vid_id)
            if not item:
                raise KeyError("Không tìm thấy video.")
            src_path = item["path"]
            dest_path = item.get("destPath") or self.copied_map.get(src_path)
            if not dest_path:
                item["status"] = "idle"
                return {"ok": True, "status": "idle"}

            real_dest_file = os.path.realpath(dest_path)
            real_dest_root = os.path.realpath(self.dest)
            real_src_file = os.path.realpath(src_path)
            # Strict safety guard: never delete source file or anything outside dest folder
            if (
                real_dest_file != real_src_file
                and os.path.isdir(self.dest)
                and os.path.commonpath([real_dest_file, real_dest_root]) == real_dest_root
                and os.path.isfile(real_dest_file)
            ):
                os.remove(real_dest_file)

            self.copied_map.pop(src_path, None)
            item["status"] = "idle"
            item["destPath"] = None
            reconcile_items_with_dest(self.items_list, self.dest, self.copied_map, self.queued_srcs)
        self._save_state()
        return {"ok": True, "status": "idle"}

    def _copy_worker(self) -> None:
        while True:
            job = self.copy_queue.get()
            if job is None:
                self.copy_queue.task_done()
                break
            vid_id, src_path, dest_path = job
            with self.lock:
                self.active_copy = os.path.basename(src_path)
            tmp_path = f"{dest_path}.culler_part"
            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                # Re-check collision right before writing in case two identically named clips queued together
                with self.lock:
                    item = self.items_by_id.get(vid_id)
                    rel_folder = item["folder"] if item else "."
                    if os.path.exists(dest_path):
                        dest_path = resolve_dest_path(self.dest, src_path, rel_folder, self.copied_map)
                        tmp_path = f"{dest_path}.culler_part"
                shutil.copy2(src_path, tmp_path)
                os.replace(tmp_path, dest_path)
                with self.lock:
                    self.copied_map[src_path] = dest_path
                    self.queued_srcs.discard(src_path)
                    if item:
                        item["status"] = "copied"
                        item["destPath"] = dest_path
                self._save_state()
            except Exception as exc:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                with self.lock:
                    self.queued_srcs.discard(src_path)
                    item = self.items_by_id.get(vid_id)
                    if item:
                        item["status"] = "error"
                    self.errors.append(f"{os.path.basename(src_path)}: {exc}")
                    self.errors = self.errors[-10:]
            finally:
                with self.lock:
                    self.active_copy = None
                self.copy_queue.task_done()

    def get_thumb(self, vid_id: str) -> bytes:
        with self.lock:
            item = self.items_by_id.get(vid_id)
        if not item:
            raise KeyError("Unknown video id")

        src_path = item["path"]
        cache_key = hashlib.sha1(f"{src_path}:{item['size']}:{item['mtime']}".encode()).hexdigest()[:20]
        thumb_file = self.thumb_dir / f"{cache_key}.jpg"
        if thumb_file.is_file():
            return thumb_file.read_bytes()

        with self.thumb_sem:
            if thumb_file.is_file():
                return thumb_file.read_bytes()
            for seek_args in (["-ss", "1"], []):
                cmd = [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-skip_frame",
                    "nokey",
                    *seek_args,
                    "-i",
                    src_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:-2",
                    "-q:v",
                    "5",
                    "-f",
                    "image2",
                    "pipe:1",
                ]
                proc = subprocess.run(cmd, capture_output=True, timeout=15)
                if proc.returncode == 0 and proc.stdout:
                    tmp = thumb_file.with_suffix(".tmp")
                    tmp.write_bytes(proc.stdout)
                    os.replace(tmp, thumb_file)
                    return proc.stdout
        raise RuntimeError("Thumbnail extraction failed")

    def state_payload(self) -> dict:
        with self.lock:
            folders: dict[str, int] = {}
            top_folders: dict[str, int] = {}
            copied_count = 0
            copied_bytes = 0
            public_items = []
            for it in self.items_list:
                folders[it["folder"]] = folders.get(it["folder"], 0) + 1
                top_folders[it["topFolder"]] = top_folders.get(it["topFolder"], 0) + 1
                if it["status"] == "copied":
                    copied_count += 1
                    copied_bytes += it["size"]
                public_items.append(
                    {
                        "id": it["id"],
                        "name": it["name"],
                        "rel": it["rel"],
                        "folder": it["folder"],
                        "topFolder": it["topFolder"],
                        "size": it["size"],
                        "mtime": it["mtime"],
                        "status": it["status"],
                    }
                )
            return {
                "source": self.source,
                "dest": self.dest,
                "sourceExists": os.path.isdir(self.source),
                "items": public_items,
                "topFolders": top_folders,
                "folders": folders,
                "copiedCount": copied_count,
                "copiedBytes": copied_bytes,
                "queueSize": len(self.queued_srcs),
                "activeCopy": self.active_copy,
                "errors": list(self.errors),
            }

    def copy_status_payload(self) -> dict:
        with self.lock:
            copied_ids = [it["id"] for it in self.items_list if it["status"] == "copied"]
            queued_ids = [it["id"] for it in self.items_list if it["status"] == "queued"]
            copied_bytes = sum(it["size"] for it in self.items_list if it["status"] == "copied")
            return {
                "queueSize": len(self.queued_srcs),
                "activeCopy": self.active_copy,
                "copiedCount": len(copied_ids),
                "copiedBytes": copied_bytes,
                "copiedIds": copied_ids,
                "queuedIds": queued_ids,
                "errors": list(self.errors),
            }

    def shutdown(self) -> None:
        self.copy_queue.put(None)
        self.worker.join(timeout=5)


def make_handler(app: CullerApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send_json(self, data: dict, status: int = 200) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            qs = parse_qs(parsed.query)

            try:
                if route == "/":
                    html = STATIC_INDEX.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                    return

                if route == "/api/state":
                    app.ensure_scanned_and_reconciled()
                    self._send_json(app.state_payload())
                    return

                if route == "/api/copy-status":
                    self._send_json(app.copy_status_payload())
                    return

                if route == "/api/thumb":
                    vid_id = (qs.get("id") or [""])[0]
                    jpg = app.get_thumb(vid_id)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.send_header("Cache-Control", "public, max-age=604800, immutable")
                    self.end_headers()
                    self.wfile.write(jpg)
                    return

                if route == "/api/video":
                    vid_id = (qs.get("id") or [""])[0]
                    compat = (qs.get("compat") or ["0"])[0] == "1"
                    with app.lock:
                        item = app.items_by_id.get(vid_id)
                    if not item or not os.path.isfile(item["path"]):
                        self.send_error(HTTPStatus.NOT_FOUND, "Video not found")
                        return
                    if compat:
                        self._stream_compat(item["path"])
                    else:
                        self._stream_range(item["path"], item["ext"])
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                return
            except KeyError:
                self._send_json({"error": "Không tìm thấy video"}, 404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)

        def _stream_range(self, filepath: str, ext: str) -> None:
            total = os.path.getsize(filepath)
            ctype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(filepath)[0] or "application/octet-stream"
            range_hdr = self.headers.get("Range")
            start, end = 0, total - 1
            status = HTTPStatus.OK

            if range_hdr and range_hdr.startswith("bytes="):
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    s_str, e_str = m.groups()
                    if s_str:
                        start = int(s_str)
                        end = int(e_str) if e_str else total - 1
                    elif e_str:
                        start = max(0, total - int(e_str))
                    end = min(end, total - 1)
                    if start >= total or start > end:
                        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()

            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _stream_compat(self, filepath: str) -> None:
            """Hardware-accelerated fragmented MP4 fallback for exotic camera codecs/PCM audio."""
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                filepath,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "h264_videotoolbox",
                "-b:v",
                "8M",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Connection", "close")
                self.end_headers()
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                body = self._read_json()

                if route == "/api/copy":
                    res = app.enqueue_copy(str(body.get("id") or ""))
                    self._send_json(res)
                    return

                if route == "/api/uncopy":
                    res = app.uncopy(str(body.get("id") or ""))
                    self._send_json(res)
                    return

                if route == "/api/config":
                    app.set_config(body.get("source"), body.get("dest"))
                    self._send_json(app.state_payload())
                    return

                if route == "/api/rescan":
                    app.rescan()
                    self._send_json(app.state_payload())
                    return

                if route == "/api/pick-folder":
                    prompt = "Chọn thư mục nguồn" if body.get("kind") != "dest" else "Chọn thư mục đích để Copy video"
                    apple_script = (
                        'tell application (path to frontmost application as text) to '
                        f'POSIX path of (choose folder with prompt "{prompt}")'
                    )
                    proc = subprocess.run(
                        ["/usr/bin/osascript", "-e", apple_script],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    if proc.returncode != 0:
                        self._send_json({"cancelled": True})
                    else:
                        self._send_json({"path": proc.stdout.strip()})
                    return

                if route == "/api/reveal":
                    vid_id = body.get("id")
                    if vid_id:
                        with app.lock:
                            item = app.items_by_id.get(str(vid_id))
                        if not item:
                            self._send_json({"error": "Không tìm thấy video"}, 404)
                            return
                        subprocess.Popen(["open", "-R", item["path"]])
                    else:
                        os.makedirs(app.dest, exist_ok=True)
                        subprocess.Popen(["open", app.dest])
                    self._send_json({"ok": True})
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError as exc:
                self._send_json({"error": str(exc)}, 404)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)

    return Handler


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="SSD Video Culler")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--source", default=None)
    parser.add_argument("--dest", default=None)
    parser.add_argument("--lazy-scan", action="store_true")
    args = parser.parse_args()

    app = CullerApp(source=args.source, dest=args.dest, lazy_scan=args.lazy_scan)
    server = None
    chosen_port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = ReusableThreadingHTTPServer((args.host, candidate), make_handler(app))
            chosen_port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("Could not bind a free local port")
    url = f"http://{args.host}:{chosen_port}"
    print(f"SSD Video Culler running at {url}", flush=True)
    print(f"Source: {app.source} ({len(app.items_list)} videos)", flush=True)
    print(f"Dest:   {app.dest}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Automated verification for SSD Video Culler (dest-first scan, ExFAT filter, Range stream, collision copy & undo)."""

import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from culler import CullerApp, make_handler


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "ssd"
        dest = root / "selected"
        empty_dest = root / "empty_dest"
        cache = root / "cache"

        (src / "23:6:23 - PARK").mkdir(parents=True)
        (src / "Alishan").mkdir(parents=True)
        (src / "Edit").mkdir(parents=True)
        (src / "$RECYCLE.BIN").mkdir(parents=True)
        (src / "node_modules" / "pkg").mkdir(parents=True)
        dest.mkdir(parents=True)
        empty_dest.mkdir(parents=True)

        # Two shoots sharing camera filename DSCF1225.MOV with different sizes, plus a unique intro.mp4
        (src / "23:6:23 - PARK" / "DSCF1225.MOV").write_bytes(b"A" * 1024)
        (src / "23:6:23 - PARK" / "._DSCF1225.MOV").write_bytes(b"ghost")
        (src / "Alishan" / "DSCF1225.MOV").write_bytes(b"B" * 2048)
        (src / "Edit" / "intro.mp4").write_bytes(b"C" * 900)
        (src / "Edit" / "vite.config.mts").write_bytes(b"export default {}")
        (src / "node_modules" / "pkg" / "index.d.mts").write_bytes(b"export type X = 1;")
        (src / "$RECYCLE.BIN" / "trash.mp4").write_bytes(b"trash")

        # Pre-populate dest BEFORE app starts:
        # 1) Exact copy of PARK/DSCF1225.MOV (1024B) -> Alishan/DSCF1225.MOV (2048B) must stay uncopied!
        (dest / "DSCF1225.MOV").write_bytes(b"A" * 1024)
        # 2) Same filename intro.mp4 but compressed size (300B) -> must still mark Edit/intro.mp4 as copied!
        (dest / "intro.mp4").write_bytes(b"c" * 300)

        app = CullerApp(source=str(src), dest=str(dest), cache_dir=cache)
        assert len(app.items_list) == 3, f"Expected 3 real videos, got {len(app.items_list)}"

        by_rel = {it["rel"]: it for it in app.items_list}
        assert by_rel["23:6:23 - PARK/DSCF1225.MOV"]["status"] == "copied"
        assert by_rel["Alishan/DSCF1225.MOV"]["status"] == "idle"
        assert by_rel["Edit/intro.mp4"]["status"] == "copied"

        # Add Finder-style duplicate 'DSCF1225 2.MOV' (2048B) directly into dest & hit GET /api/state
        (dest / "DSCF1225 2.MOV").write_bytes(b"B" * 2048)

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{port}"

        try:
            # 1. GET /api/state automatically scans dest first -> all 3 now copied!
            with urllib.request.urlopen(f"{base}/api/state") as r:
                state = json.loads(r.read().decode())
            assert state["copiedCount"] == 3, f"Expected 3 copied after Finder copy, got {state['copiedCount']}"

            # 2. Switch dest to empty_dest -> copiedCount drops to 0 immediately
            cfg_req = urllib.request.Request(
                f"{base}/api/config",
                data=json.dumps({"dest": str(empty_dest)}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(cfg_req) as r:
                state_empty = json.loads(r.read().decode())
            assert state_empty["copiedCount"] == 0

            # 3. Verify HTTP 206 Partial Content byte-range streaming
            first_id = state_empty["items"][0]["id"]
            req = urllib.request.Request(f"{base}/api/video?id={first_id}", headers={"Range": "bytes=10-19"})
            with urllib.request.urlopen(req) as r:
                assert r.status == 206
                assert len(r.read()) == 10

            # 4. Copy two identically named DSCF1225.MOV clips into empty_dest -> collision-safe naming
            for it in state_empty["items"]:
                if it["name"] == "DSCF1225.MOV":
                    c_req = urllib.request.Request(
                        f"{base}/api/copy",
                        data=json.dumps({"id": it["id"]}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(c_req) as r:
                        assert r.status == 200

            app.copy_queue.join()
            copied_files = sorted(p.name for p in empty_dest.iterdir())
            assert len(copied_files) == 2, f"Collision overwrote a file: {copied_files}"

            # 5. Undo removes only the destination duplicate, leaving SSD untouched
            u_req = urllib.request.Request(
                f"{base}/api/uncopy",
                data=json.dumps({"id": first_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(u_req) as r:
                assert r.status == 200
            assert len(list(empty_dest.iterdir())) == 1
            assert (src / "23:6:23 - PARK" / "DSCF1225.MOV").is_file()
        finally:
            server.shutdown()
            server.server_close()
            app.shutdown()

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    run_tests()

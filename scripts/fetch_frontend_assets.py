"""Download only the pinned images used by the local PerkFlow frontend."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
manifest = json.loads((FRONTEND / "assets-manifest.json").read_text(encoding="utf-8"))


def fetch(item):
    target = (FRONTEND / item["destination"]).resolve()
    if not target.is_relative_to(FRONTEND.resolve()):
        raise ValueError("Asset path escapes frontend directory")
    if target.exists():
        payload = target.read_bytes()
    else:
        url = f'https://raw.githubusercontent.com/Vanzaseses/hmdp/{manifest["commit"]}/{item["source"]}'
        with urlopen(Request(url, headers={"User-Agent": "PerkFlow-asset-import"}), timeout=30) as response:
            payload = response.read()
    blob = b"blob " + str(len(payload)).encode() + b"\0" + payload
    if len(payload) != item["size"] or hashlib.sha1(blob).hexdigest() != item["gitBlobSha"]:
        raise ValueError(f'Asset verification failed: {item["source"]}')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return item["destination"]


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=6) as pool:
        for path in pool.map(fetch, manifest["files"]):
            print(path)

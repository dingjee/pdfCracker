"""Bounded parallel registry transfer for the pinned local BF16 checkpoint."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import time

import httpx

SHA = "f4f2f14b6d330a8e8df509b2de98bb4e2704a0ba6bead22e4d4b9361dc33c76f"
SIZE = 1557662848
ROOT = Path(__file__).resolve().parents[2]
PARTS = ROOT / ".local/weight-parts"
TARGET = ROOT / ".local/models/blobs" / f"sha256-{SHA}.download"
CHUNK = 64 * 1024 * 1024


def fetch(index):
    chunk_first = index * CHUNK
    last = min(SIZE, chunk_first + CHUNK) - 1
    target = PARTS / str(index)
    if target.exists() and target.stat().st_size == last - chunk_first + 1:
        return
    for attempt in range(4):
        try:
            offset = target.stat().st_size if target.exists() else 0
            if offset > last - chunk_first + 1:
                target.unlink()
                offset = 0
            first = chunk_first + offset
            with httpx.Client(follow_redirects=True, timeout=300) as client:
                url = f"https://registry.ollama.ai/v2/longwayxu/xiaomi-ocr-0/blobs/sha256:{SHA}"
                with client.stream("GET", url, headers={"Range": f"bytes={first}-{last}"}) as response:
                    response.raise_for_status()
                    if response.status_code != 206:
                        raise RuntimeError("Registry ignored the bounded range request")
                    with target.open("ab") as file:
                        for chunk in response.iter_bytes(1024 * 1024):
                            file.write(chunk)
            if target.stat().st_size != last - chunk_first + 1:
                raise RuntimeError("Incomplete range")
            print(f"Completed chunk {index}", flush=True)
            return
        except (httpx.HTTPError, RuntimeError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def main():
    PARTS.mkdir(parents=True, exist_ok=True)
    count = (SIZE + CHUNK - 1) // CHUNK
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(fetch, range(count)))
    with TARGET.open("wb") as output:
        for index in range(count):
            with (PARTS / str(index)).open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
    with TARGET.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != SHA:
            raise RuntimeError("Weight checksum mismatch")
    print("All chunks assembled and SHA256 verified", flush=True)


if __name__ == "__main__":
    main()

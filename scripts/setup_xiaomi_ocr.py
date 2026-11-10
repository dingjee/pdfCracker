"""Install the pinned Apple Silicon runtime and verified BF16 model locally.

Run with ``uv run scripts/setup_xiaomi_ocr.py``. No global packages/config changes.
Direct registry downloads also work with VPN fake-IP DNS, which recent Ollama
versions reject when following registry storage redirects during ``ollama pull``.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tarfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
VERSION = "0.40.14"
RUNTIME_SHA256 = "f3bf9a528e908da0da5a2b3a4ad3252a369d62186501dc4880261e8d9023bff5"
REGISTRY = "https://registry.ollama.ai/v2/longwayxu/xiaomi-ocr-0"
# Published BF16 package, pinned by each content digest, including vision projector.
LAYERS = {
    "b6db2b19967059bd0085fb9c12c13b8f48c53afc7863c36dbd4515c471ca543c": 118,
    "aa8b9bed474e2f5c34356c15e25c35a82f0da55ba6e76d36f710d6acdb82e2a8": 207346528,
    "f4f2f14b6d330a8e8df509b2de98bb4e2704a0ba6bead22e4d4b9361dc33c76f": 1557662848,
    "6e9469f4b690fe40583d39ac77badb6406a464ade6232ab911863fa2b6a0ce0a": 81,
}


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def download(client: httpx.Client, url: str, target: Path, sha256: str) -> None:
    if target.is_file() and digest(target) == sha256:
        print(f"Verified cached {target.name}", flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".download")
    if not temporary.is_file() or digest(temporary) != sha256:
        print(f"Downloading {target.name}", flush=True)
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with temporary.open("wb") as file:
                for chunk in response.iter_bytes(1024 * 1024):
                    file.write(chunk)
    if digest(temporary) != sha256:
        raise RuntimeError(f"SHA256 mismatch for {target.name}; file was not installed")
    temporary.replace(target)


def main() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This pinned installer targets macOS Apple Silicon.")
    subprocess.run(["uv", "sync", "--locked", "--extra", "ocr-layout"], cwd=ROOT, check=True)
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(1200, connect=30)) as client:
        archive = LOCAL / "ollama-darwin.tgz"
        download(client, f"https://github.com/ollama/ollama/releases/download/v{VERSION}/ollama-darwin.tgz",
                 archive, RUNTIME_SHA256)
        runtime = LOCAL / "ollama"
        runtime.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as package:
            package.extractall(runtime, filter="data")
        response = client.get(f"{REGISTRY}/manifests/bf16")
        response.raise_for_status()
        manifest = response.json()
        entries = [manifest["config"], *manifest["layers"]]
        if {item["digest"].removeprefix("sha256:"): item["size"] for item in entries} != LAYERS:
            raise RuntimeError("Remote model tag changed; review it before updating pinned digests.")
        for sha256, size in LAYERS.items():
            target = LOCAL / "models" / "blobs" / f"sha256-{sha256}"
            download(client, f"{REGISTRY}/blobs/sha256:{sha256}", target, sha256)
            if target.stat().st_size != size:
                raise RuntimeError(f"Size mismatch for {target.name}")
        destination = LOCAL / "models/manifests/registry.ollama.ai/longwayxu/xiaomi-ocr-0/bf16"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".download")
        temporary.write_bytes(response.content)
        temporary.replace(destination)
    (LOCAL / "xiaomi-runtime.json").write_text(json.dumps({
        "ollama_version": VERSION, "runtime_sha256": RUNTIME_SHA256,
        "model": "longwayxu/xiaomi-ocr-0:bf16", "layers": LAYERS,
        "manifest_sha256": hashlib.sha256(response.content).hexdigest(),
    }, indent=2) + "\n")
    print("Installed. Start the local server: bash scripts/start_xiaomi_ocr.sh")


if __name__ == "__main__":
    main()

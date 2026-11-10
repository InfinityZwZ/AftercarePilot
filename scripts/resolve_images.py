"""Resolve public Docker Official Image tags without a Docker daemon or credentials."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))


def resolve(repository, tag):
    with urlopen(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/{repository}:pull", timeout=30) as response:
        token = json.load(response)["token"]
    def fetch(path):
        request = Request(f"https://registry-1.docker.io/v2/library/{repository}/{path}",
                          headers={"Authorization": "Bearer " + token, "Accept": ACCEPT})
        with urlopen(request, timeout=30) as response:
            data = response.read()
        return json.loads(data), "sha256:" + hashlib.sha256(data).hexdigest()
    index, digest = fetch("manifests/" + tag)
    platform = next(item for item in index["manifests"] if item.get("platform") == {"architecture": "amd64", "os": "linux"})
    manifest, platform_digest = fetch("manifests/" + platform["digest"])
    config, _ = fetch("blobs/" + manifest["config"]["digest"])
    versions = {key: value for key, value in (item.split("=", 1) for item in config["config"]["Env"])
                if key in ("PYTHON_VERSION", "PG_MAJOR", "PG_VERSION")}
    return {"reference": f"{repository}:{tag}@{digest}", "linux_amd64_digest": platform_digest, "image_versions": versions,
            "status": "registry manifest resolved; runtime verification recorded separately"}


def main():
    values = {"python": resolve("python", "3.13-slim-bookworm"), "postgres": resolve("postgres", "17-bookworm")}
    path = ROOT / "docker" / "images.lock.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(values, indent=2))


if __name__ == "__main__":
    main()

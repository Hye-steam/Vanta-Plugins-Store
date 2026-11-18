#!/usr/bin/env python3
"""把上游插件商店同步到本仓库。

上游有两个：
  - Muse 商店（index.json 直接带 package_url）
  - AiCode 商店（index.json 不带包地址，包在各仓库的 GitHub Release 里）

同步规则：
  * 新插件 → 下载包进 packages/，索引里指向本仓库
  * 上游已下架的条目 → 本地索引同步删除，对应包文件也清掉
  * 上游改了版本 → 重新下载新包，旧包删除

产物只有两个：index.json 与 packages/*.zip。脚本可重复执行，结果幂等。
"""

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"
INDEX = ROOT / "index.json"

RAW_BASE = "https://raw.githubusercontent.com/Hye-steam/Vanta-Plugins-Store/main"

MUSE_INDEX = "https://raw.githubusercontent.com/xzyy03/Muse-Plugins-Store/main/index.json"
AICODE_INDEX = "https://raw.githubusercontent.com/jieapi/AiCode-Plugins-Store/main/index.json"

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def get(url: str, binary: bool = False, timeout: int = 60):
    headers = {"User-Agent": "vanta-store-sync"}
    if TOKEN:
        headers["Authorization"] = f"token {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8")


def load_entries(url: str) -> list:
    try:
        doc = json.loads(get(url))
    except Exception as exc:  # 上游挂了就保留现状，不要清空商店
        print(f"[warn] 读取上游失败，跳过：{url} ({exc})")
        return []
    entries = doc if isinstance(doc, list) else doc.get("plugins", [])
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def safe_name(name: str) -> str:
    """包文件名可能含中文：本地保留原名，索引里做 URL 编码。"""
    return re.sub(r"[\\/]", "_", name)


def download(url: str, target: pathlib.Path) -> bool:
    if target.is_file() and target.stat().st_size > 0:
        return True
    try:
        data = get(url, binary=True, timeout=180)
    except Exception as exc:
        print(f"[warn] 下载失败 {url}: {exc}")
        return False
    if not data:
        return False
    target.write_bytes(data)
    print(f"  下载 {target.name} ({len(data)} bytes)")
    return True


def latest_asset(repo: str):
    """返回 (version, asset_name, download_url)；失败返回 (None, None, None)。"""
    try:
        rel = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))
    except Exception as exc:
        print(f"[warn] 读 release 失败 {repo}: {exc}")
        return None, None, None
    tag = (rel.get("tag_name") or "").lstrip("v")
    asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".zip")), None)
    if not asset:
        return tag or None, None, None
    return tag or None, asset["name"], asset["browser_download_url"]


def main() -> int:
    PACKAGES.mkdir(exist_ok=True)
    merged = []
    wanted_files = set()

    # ── Muse：索引自带直链 ──
    for entry in load_entries(MUSE_INDEX):
        url = entry.get("package_url") or ""
        if not url:
            continue
        name = safe_name(urllib.parse.unquote(url.rsplit("/", 1)[-1]))
        if not download(url, PACKAGES / name):
            continue
        wanted_files.add(name)
        entry["package_url"] = f"{RAW_BASE}/packages/{urllib.parse.quote(name)}"
        merged.append(entry)

    # ── AiCode：包在各仓库的 Release 里 ──
    for entry in load_entries(AICODE_INDEX):
        repo = (entry.get("repository") or "").rstrip("/").split("github.com/")[-1]
        if not repo:
            continue
        version, asset_name, asset_url = latest_asset(repo)
        if not asset_name:
            print(f"[warn] {entry['id']} 没有可用 zip，跳过")
            continue
        name = safe_name(asset_name)
        if not download(asset_url, PACKAGES / name):
            continue
        wanted_files.add(name)
        entry["version"] = version or entry.get("version")
        entry["package_url"] = f"{RAW_BASE}/packages/{urllib.parse.quote(name)}"
        merged.append(entry)

    # ── 自有插件：local/index.json 里的条目原样保留，不随上游同步消失 ──
    local_index = ROOT / "local" / "index.json"
    if local_index.is_file():
        try:
            local_entries = json.loads(local_index.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] local/index.json 解析失败：{exc}")
            local_entries = []
        for entry in local_entries:
            url = entry.get("package_url") or ""
            name = safe_name(urllib.parse.unquote(url.rsplit("/", 1)[-1])) if url else ""
            if name and not (PACKAGES / name).is_file():
                print(f"[warn] 自有插件缺少包文件 {name}，跳过 {entry.get('id')}")
                continue
            if name:
                wanted_files.add(name)
            merged = [e for e in merged if e.get("id") != entry.get("id")] + [entry]

    if not merged:
        print("上游全部不可用，保持现有索引不变")
        return 0

    # ── 下架同步：索引里没有的包文件删掉 ──
    for path in PACKAGES.glob("*"):
        if path.is_file() and path.name not in wanted_files:
            path.unlink()
            print(f"  删除已下架 {path.name}")

    INDEX.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"索引已更新：{len(merged)} 个插件，{len(wanted_files)} 个包")
    return 0


if __name__ == "__main__":
    sys.exit(main())

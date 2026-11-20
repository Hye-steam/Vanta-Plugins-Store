#!/usr/bin/env python3
"""把上游插件商店同步到本仓库。

上游有两个：
  - Nox 商店（index.json 直接带 package_url）
  - AiCode 商店（index.json 不带包地址，包在各仓库的 GitHub Release 里）

Nox 商店仓库原名 xzyy03/Muse-Plugins-Store，2026-11 改名并换品牌。
旧地址目前仍能取到内容（GitHub 对改名仓库保留 raw 重定向），但随时可能失效，
故直接指向新地址。

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

NOX_INDEX = "https://raw.githubusercontent.com/xzyy03/Nox-Plugins-Store/main/index.json"
AICODE_INDEX = "https://raw.githubusercontent.com/jieapi/AiCode-Plugins-Store/main/index.json"

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def get(url: str, binary: bool = False, timeout: int = 60):
    headers = {"User-Agent": "vanta-store-sync"}
    if TOKEN:
        headers["Authorization"] = f"token {TOKEN}"
    # 上游包名可能含中文且未做 URL 编码（如「一束挽风.wb-gateway_1.5.2.zip」），
    # urllib 只接受 ASCII，不先转义会直接报 'ascii' codec can't encode。
    # 已编码的 %XX 会被 safe 集保留，不会二次转义。
    req = urllib.request.Request(_quote_url(url), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8")


def _quote_url(url: str) -> str:
    """只转义路径与查询串里的非 ASCII 字符，保留 scheme://host 与已有的 %XX。"""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%")
    query = urllib.parse.quote(parts.query, safe="=&%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


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
    """拉取包文件到 target；已有同名文件时比对内容，不一致就覆盖。

    不能只看「文件存在且非空」就跳过：上游换包时可能不改文件名（改版本号但沿用旧包名，
    或同名包重新构建），那样旧包会永远留在本地、索引指向的还是旧内容。
    这里用 HEAD 拿不到可靠摘要（部分 CDN 不返回 ETag 或 ETag 不稳定），
    故直接下载后比字节——包都不大，比留着一堆隐形的陈旧文件安全。
    """
    try:
        data = get(url, binary=True, timeout=180)
    except Exception as exc:
        print(f"[warn] 下载失败 {url}: {exc}")
        return False
    if not data:
        return False
    if target.is_file():
        existing = target.read_bytes()
        if existing == data:
            return True
        print(f"  更新 {target.name} ({len(existing)} -> {len(data)} bytes)")
    else:
        print(f"  下载 {target.name} ({len(data)} bytes)")
    target.write_bytes(data)
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

    # ── Nox：索引自带直链 ──
    for entry in load_entries(NOX_INDEX):
        url = entry.get("package_url") or ""
        if not url:
            continue
        name = safe_name(urllib.parse.unquote(url.rsplit("/", 1)[-1]))
        if not download(url, PACKAGES / name):
            # 下载失败但本地已有同名包：保留旧包与旧条目。
            # 否则一次网络抖动就会把插件从索引里抹掉，并在下面的清理里删掉包。
            if (PACKAGES / name).is_file() and (PACKAGES / name).stat().st_size > 0:
                print(f"  保留旧包 {name}")
                wanted_files.add(name)
                entry["package_url"] = f"{RAW_BASE}/packages/{urllib.parse.quote(name)}"
                merged.append(entry)
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
            if (PACKAGES / name).is_file() and (PACKAGES / name).stat().st_size > 0:
                print(f"  保留旧包 {name}")
                wanted_files.add(name)
                entry["version"] = version or entry.get("version")
                entry["package_url"] = f"{RAW_BASE}/packages/{urllib.parse.quote(name)}"
                merged.append(entry)
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

    # 顶层必须是 {schema_version, plugins}：写成裸数组会被客户端判为「索引内容无效」。
    document = {"schema_version": 1, "plugins": merged}
    INDEX.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"索引已更新：{len(merged)} 个插件，{len(wanted_files)} 个包")
    return 0


if __name__ == "__main__":
    sys.exit(main())

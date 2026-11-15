#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视接口聚合器
- 每次运行都重新抓取 sources.json 里的上游接口，合并站点并去重
- 上游更新后，下一次运行自动跟上，不写死任何站点数据
- 仅用标准库，GitHub Actions 直接可跑

输出:
  dist/all.json   合并后的单仓配置（一个入口，全站点）
  dist/ok.json    多仓清单（每个上游一个仓，互相隔离）
  dist/report.txt 本次抓取情况
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request

UA = "okhttp/3.12.0"
TIMEOUT = 30
RETRY = 2
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(msg, flush=True)


def fetch(url, retry=RETRY):
    last = None
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX))
            with opener.open(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retry:
                time.sleep(1.5 * (i + 1))
    raise last


def to_ascii_host(url):
    """中文域名统一转 punycode，避免下游客户端解析失败"""
    try:
        scheme, rest = url.split("://", 1)
        host = re.split(r"[/?:]", rest, 1)[0]
        if any(ord(c) > 127 for c in host):
            return scheme + "://" + host.encode("idna").decode("ascii") + rest[len(host):]
    except Exception:  # noqa: BLE001
        pass
    return url


def resolve(base_url, value):
    if not isinstance(value, str) or not value:
        return value
    if value.startswith("./"):
        return to_ascii_host(base_url.rsplit("/", 1)[0] + "/" + value[2:])
    if "://" in value:
        return to_ascii_host(value)
    return value


def norm_name(site):
    raw = str(site.get("name") or site.get("key") or "")
    return re.sub(r"[^\w\u4e00-\u9fff]", "", raw)


def site_id(site):
    """站点身份：优先 api（同一实现），没有则用 key；配合名称做二次判定"""
    api = str(site.get("api") or "").strip()
    ident = api if api else str(site.get("key") or "")
    return ident.lower(), norm_name(site)


def load_config(url):
    """拉取一个接口；若是多仓清单则展开一层，返回 [(名称, 接口地址, 配置对象)]"""
    raw = fetch(url)
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        raise ValueError("返回内容不是 JSON（可能是 hexdump / HTML / 图片）")

    if isinstance(data, dict) and isinstance(data.get("urls"), list):
        out = []
        for item in data["urls"]:
            sub = resolve(url, item.get("url"))
            if not sub:
                continue
            try:
                out.append((item.get("name") or sub, sub, json.loads(fetch(sub).decode("utf-8", "ignore"))))
            except Exception as e:  # noqa: BLE001
                log(f"    [跳过] 子仓 {sub[:60]} {e}")
        return out
    return [(None, url, data)]


def main():
    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        sources = json.load(f)["sources"]

    merged = {"sites": [], "parses": [], "lives": [], "flags": [], "spider": "", "wallpaper": ""}
    seen_sites, seen_parses, seen_lives = set(), set(), set()
    warehouse, report, ok_count = [], [], 0

    for src in sources:
        name, url = src["name"], to_ascii_host(src["url"])
        log(f"[抓取] {name}  {url}")
        try:
            configs = load_config(url)
        except Exception as e:  # noqa: BLE001
            report.append(f"{name}\t失败\t{type(e).__name__}: {e}")
            log(f"    [失败] {e}")
            continue

        ok_count += 1
        added = dup = 0
        for sub_name, sub_url, cfg in configs:
            spider = resolve(sub_url, cfg.get("spider"))
            for site in cfg.get("sites") or []:
                if not isinstance(site, dict) or not site.get("key"):
                    continue
                sid = site_id(site)
                if sid in seen_sites:
                    dup += 1
                    continue
                seen_sites.add(sid)
                if isinstance(site.get("api"), str) and site["api"].startswith("http"):
                    site["api"] = to_ascii_host(site["api"])
                jar = resolve(sub_url, site.get("jar")) or spider
                if jar:
                    site["jar"] = to_ascii_host(jar)
                merged["sites"].append(site)
                added += 1

            for p in cfg.get("parses") or []:
                key = p.get("url")
                if key and key not in seen_parses:
                    seen_parses.add(key)
                    merged["parses"].append(p)
            for l in cfg.get("lives") or []:
                key = l.get("url")
                if key and key not in seen_lives:
                    seen_lives.add(key)
                    merged["lives"].append(l)

            flags = cfg.get("flags")
            if isinstance(flags, dict):
                for k, v in flags.items():
                    if not any(x.get("key") == k for x in merged["flags"]):
                        merged["flags"].append({"key": k, "name": v})
            elif isinstance(flags, list):
                for x in flags:
                    if x not in merged["flags"]:
                        merged["flags"].append(x)

            if not merged["wallpaper"] and cfg.get("wallpaper"):
                merged["wallpaper"] = resolve(sub_url, cfg["wallpaper"])
            if not merged["spider"] and spider:
                merged["spider"] = to_ascii_host(spider)

        warehouse.append({"name": f"{name}（{added}站点）", "url": url})
        report.append(f"{name}\t成功\t站点 {added}，重复跳过 {dup}")
        log(f"    [成功] 站点 {added}，重复跳过 {dup}")

    if ok_count == 0:
        log("全部上游都抓取失败，保留上一次产物不做覆盖")
        sys.exit(1)

    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    merged["_generated"] = stamp
    merged["_sources"] = len(sources)

    with open(os.path.join(HERE, "dist", "all.json"), "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(HERE, "dist", "ok.json"), "w", encoding="utf-8") as f:
        json.dump(warehouse, f, ensure_ascii=False, indent=2)

    lines = [f"生成时间 {stamp}", f"上游 {len(sources)} 个，成功 {ok_count} 个",
             f"合并后站点 {len(merged['sites'])}，解析 {len(merged['parses'])}，直播 {len(merged['lives'])}", ""]
    lines += report
    with open(os.path.join(HERE, "dist", "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log("\n".join(lines))


if __name__ == "__main__":
    main()
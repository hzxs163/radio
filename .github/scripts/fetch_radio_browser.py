#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备用数据源：radio-browser.info → roamdial 快照格式转换
主源 (roamdial.com) 拉取失败时由 GitHub Actions 调用本脚本，
从 Radio Browser 公开 API 拉取全量电台数据，转换为
api_core_cn.json / api_rest.json 同构格式（land-dots.json 保持不变）。

用法: python3 fetch_radio_browser.py [输出目录]
输出目录默认当前目录。
"""
import json
import math
import os
import sys
import time
import urllib.request

RB_HOSTS = ["de1.api.radio-browser.info", "de2.api.radio-browser.info",
            "fi1.api.radio-browser.info", "at1.api.radio-browser.info"]
UA = "RoamDialMirror/1.0 (fallback snapshot builder)"


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_stations():
    """分页拉取全部电台（小页拉取，避免大响应超时）。"""
    out = []
    offset = 0
    limit = int(os.environ.get("RDLIMIT", "1000"))
    max_pages = int(os.environ.get("RDPAGES", "0"))  # 本地测试用：0=全量
    while True:
        data = None
        last_err = None
        for host in RB_HOSTS:
            try:
                url = ("https://%s/json/stations?hidebroken=false&limit=%d&offset=%d"
                       "&order=stationuuid&reverse=false" % (host, limit, offset))
                data = fetch_json(url, timeout=60)
                break
            except Exception as e:
                last_err = e
        if data is None:
            raise RuntimeError("Radio Browser 全部节点不可达(offset=%d): %s" % (offset, last_err))
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        print("已拉取 %d 条..." % len(out), flush=True)
        if max_pages and (offset // limit) + 1 >= max_pages:
            print("本地测试模式: 达到页数上限 %d，提前结束" % max_pages, flush=True)
            break
        if len(data) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def norm_url(u):
    if not u:
        return ""
    u = u.strip()
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u.rstrip("/")


def build(outdir):
    stations = fetch_all_stations()
    print("Radio Browser 拉取电台数: %d" % len(stations))

    # 过滤：必须有可用的 http(s) 地址
    rows = []
    for s in stations:
        u = norm_url(s.get("url") or s.get("url_resolved") or "")
        if not u or not u.startswith("https://"):
            continue
        rows.append({
            "uuid": (s.get("stationuuid") or "").strip(),
            "name": (s.get("name") or "").strip()[:200],
            "url": u,
            "tags": (s.get("tags") or "").strip()[:200],
            "state": (s.get("state") or "").strip()[:100],
            "cc": (s.get("countrycode") or "").strip().upper()[:2],
            "langs": (s.get("languagecodes") or "").strip()[:100],
            "lat": s.get("geo_lat"),
            "lng": s.get("geo_long"),
            "br": int(s.get("bitrate") or 0),
            "votes": int(s.get("votes") or 0),
            "hls": bool(s.get("hls")),
        })
    if len(rows) < 100:
        raise RuntimeError("有效电台过少(%d)，疑似数据异常，中止" % len(rows))
    print("有效电台数: %d" % len(rows))

    # 去重 uuid
    seen = set()
    uniq = []
    for r in rows:
        if r["uuid"] and r["uuid"] not in seen:
            seen.add(r["uuid"])
            uniq.append(r)
    rows = uniq

    # 构建国家/语言/流派索引
    cc_set = sorted({r["cc"] for r in rows if r["cc"]})
    countries = [[c, c, c] for c in cc_set]  # [code, 中文(降级用 code), 英文]
    cc_idx = {c: i for i, c in enumerate(cc_set)}

    lang_set = sorted({r["langs"] for r in rows if r["langs"]})
    langs = lang_set
    langs_en = lang_set
    lang_idx = {v: i for i, v in enumerate(lang_set)}

    genre_set = []
    genre_map = {}
    for r in rows:
        for tag in [t.strip() for t in r["tags"].split(",") if t.strip()]:
            if tag not in genre_map:
                genre_map[tag] = len(genre_set)
                genre_set.append(tag)
    genres = genre_set
    genres_en = genre_set

    n = len(rows)
    tzs = ["UTC"]  # Radio Browser 无时区字段，备用降级为 UTC
    tz_idx = 0

    core = {
        "rg": "cn", "v": 3, "builtAt": int(time.time() * 1000), "n": n,
        "tzs": tzs, "genres": genres, "genresEn": genres_en,
        "langs": langs, "langsEn": langs_en,
        "countries": countries,
        "lat": [], "lng": [], "prec": [], "g": [], "l": [], "c": [],
        "pop": [], "br": [], "fl": [],
    }
    rest = {
        "builtAt": int(time.time() * 1000), "rg": "c", "tz": [],
        "id": [], "name": [], "url": [], "tags": [], "state": [],
    }

    for r in rows:
        lat = r["lat"] if isinstance(r["lat"], (int, float)) and not isinstance(r["lat"], bool) else None
        lng = r["lng"] if isinstance(r["lng"], (int, float)) and not isinstance(r["lng"], bool) else None
        core["lat"].append(int(round(lat * 100)) if lat is not None else 0)
        core["lng"].append(int(round(lng * 100)) if lng is not None else 0)
        core["prec"].append(0)
        first_tag = [t.strip() for t in r["tags"].split(",") if t.strip()][:1]
        core["g"].append(genre_map[first_tag[0]] if first_tag else 0)
        core["l"].append(lang_idx.get(r["langs"], 0))
        core["c"].append(cc_idx.get(r["cc"], 0))
        core["pop"].append(min(r["votes"], 999))
        core["br"].append(r["br"])
        core["fl"].append(1 if r["hls"] else 0)

        rest["tz"].append(tz_idx)
        rest["id"].append((r["uuid"].replace("-", "") or ("%012x" % (len(rest["id"]) + 1))))
        rest["name"].append(r["name"])
        rest["url"].append(r["url"])
        rest["tags"].append(r["tags"])
        rest["state"].append(r["state"])

    with open(outdir + "/api_core_cn.json", "w", encoding="utf-8") as f:
        json.dump(core, f, ensure_ascii=False, separators=(",", ":"))
    with open(outdir + "/api_rest.json", "w", encoding="utf-8") as f:
        json.dump(rest, f, ensure_ascii=False, separators=(",", ":"))
    print("已生成 api_core_cn.json (n=%d, 国家=%d) / api_rest.json (n=%d)" % (
        n, len(countries), len(rest["id"])))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    build(out)

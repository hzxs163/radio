#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从官方快照反推「官方口径词表」，供备用源脚本 (fetch_radio_browser.py) 使用。

备用源拿到的是 Radio Browser 的原始字段，而前端的筛选、配色、地图分组、当地时间
都只认官方那套口径（11 个流派桶、99 种语言名、IANA 时区名、国家级质心坐标）。
与其在脚本里手写规则，不如把官方自己怎么归类的证据直接从官方快照里量出来：
  tag -> 流派桶      （官方 33,884 行的 tags/g 配对投票）
  坐标 -> 时区        （官方每行都有坐标和 tz，做成网格多数派）
  国家 -> 时区 / 质心 （缺坐标时兜底）
  (国家, state) -> 坐标（官方 prec>0 的行是官方自己地理编码出来的，直接复用那个点）
生成结果是一份 JSON 词表，提交进仓库；线上主源失败、备用源顶上时就靠它对齐心形。

用法: python3 build_lexicon.py [官方core路径 官方rest路径 输出路径]
默认读取仓库根目录的 api_core_cn.json / api_rest.json，
输出 .github/scripts/official_lexicon.json。
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

# 一张表只有在「样本够多 + 多数派够压倒」时才值得提交：
# 否则会把我方猜测当成官方口径写死。阈值由真留出实验挑定（见 _exp2 记录）：
# 再放宽就会把低置信条目灌进表，命中内复现率从 91% 掉到 87%，整体反而变差。
TAG_MIN_TOTAL = 3
TAG_MIN_DOM = 0.5
GRID_MIN_TOTAL = 2
GRID_MIN_DOM = 0.6
CC_MIN_TOTAL = 5
CC_MIN_DOM = 0.85
# (国家, state) 坐标表只收「官方至少算过两次」的键：它存的是官方自己的结果，不存在
# 把我们的猜测写死的问题；但只出现一次的键不值得翻倍表体积——实测放到 >=1 次，表从
# 1,013 键涨到 1,905 键，无坐标行的命中率只从 42.3% 升到 46.0%，命中内精度还略降。
STATE_MIN_ROWS = 2
# 2° 网格优于 1°（83.1% vs 79.2%）：1° 太稀，样本被切碎后多数派不稳。
DEFAULT_STEP = 2.0

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def cell_key(lat_deg, lng_deg, step):
    """把坐标量化到 step 度的网格，返回整数 key（全球 360/step × 180/step）。"""
    lat_i = int(math.floor(lat_deg / step))
    lng_i = int(math.floor(lng_deg / step))
    span = int(360 / step)
    return (lat_i + 90) * span + (lng_i + 180)


def norm(value):
    """和 fetch_radio_browser.clean() 一致的折叠空白 + 小写，保证词表 key 能直接查到。"""
    return " ".join(str(value or "").split()).lower()


def median(values):
    s = sorted(values)
    return s[len(s) // 2]


def majority(counter):
    """Counter -> (winner, total, dominance)"""
    if not counter:
        return None, 0, 0.0
    (winner, win), = counter.most_common(1)
    total = sum(counter.values())
    return winner, total, win / total


def build(core, rest, step=DEFAULT_STEP):
    n = core["n"]
    genres_en = core["genresEn"]
    tzs = core["tzs"]
    countries = core["countries"]

    # ---- tag -> 流派 ----
    tag_votes = defaultdict(Counter)
    for i in range(n):
        tags = (rest["tags"][i] or "").lower()
        if not tags:
            continue
        g = core["g"][i]
        for raw in tags.split(","):
            t = raw.strip()
            if t:
                tag_votes[t][g] += 1

    tag_genre = {}
    for t, c in tag_votes.items():
        winner, total, dom = majority(c)
        if total >= TAG_MIN_TOTAL and dom >= TAG_MIN_DOM:
            tag_genre[t] = [winner, int(round(dom * 100))]

    # ---- 坐标 -> 时区（网格多数派） ----
    grid_votes = defaultdict(Counter)
    cc_tz_votes = defaultdict(Counter)
    cc_coords = defaultdict(list)
    for i in range(n):
        cc = countries[core["c"][i]][0]
        lat = core["lat"][i] / 100.0
        lng = core["lng"][i] / 100.0
        cc_coords[cc].append((core["lat"][i], core["lng"][i]))
        t = rest["tz"][i]
        if t < 0:
            continue                        # 官方自己也没判出时区的行，不参与投票
        name = tzs[t]
        grid_votes[cell_key(lat, lng, step)][name] += 1
        cc_tz_votes[cc][name] += 1

    tz_cells = []
    for key, c in grid_votes.items():
        winner, total, dom = majority(c)
        if total >= GRID_MIN_TOTAL and dom >= GRID_MIN_DOM:
            tz_cells.append([key, winner])
    tz_cells.sort()

    cc_tz = {}
    for cc, c in cc_tz_votes.items():
        winner, total, dom = majority(c)
        if total >= CC_MIN_TOTAL and dom >= CC_MIN_DOM:
            cc_tz[cc] = winner

    cc_center = {}
    for cc, coords in cc_coords.items():
        cc_center[cc] = [median([a for a, _ in coords]), median([b for _, b in coords])]

    # ---- (国家, state) -> 坐标 ----
    # 只用 prec>0 的官方行：那些坐标正是官方自己地理编码出来的结果，备用源遇到 RB
    # 没坐标的行时复用同一个点，比把它糊到国家级质心上更接近官方（实测 1° 内 52.7% vs 9.5%）。
    state_votes = defaultdict(list)
    for i in range(n):
        if core["prec"][i] == 0:
            continue
        st = norm(rest["state"][i])
        if not st:
            continue
        cc = countries[core["c"][i]][0]
        state_votes[(cc, st)].append((core["lat"][i], core["lng"][i]))

    state_center = []
    for (cc, st), pts in state_votes.items():
        if len(pts) < STATE_MIN_ROWS:
            continue
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        state_center.append([cc, st, median(lats), median(lngs)])
    state_center.sort()

    return {
        "note": "由官方快照反推的口径词表；生成命令见 build_lexicon.py，请勿手改",
        "sourceBuiltAt": core["builtAt"],
        # core.v 是官方自己的数据版本号（2026-10-18 的快照是 1），备用源要跟官方同值，
        # 所以从词表里带过去，而不是在脚本里写死一个猜的数。
        "coreV": core["v"],
        "cellStep": step,
        "countries": countries,
        "genres": core["genres"],
        "genresEn": core["genresEn"],
        "langs": core["langs"],
        "langsEn": core["langsEn"],
        # 顺序必须和官方 core.tzs 逐元素一致（不要排序）：备用源写出的 tz 索引要和官方
        # 索引互通，前端用同一个 tzs 数组取名字，一旦重排同一 index 就指向不同时区。
        "tzs": tzs,
        "untaggedGenre": genres_en.index("Mixed"),
        "tagGenre": dict(sorted(tag_genre.items())),
        "tzCells": tz_cells,
        "ccTz": dict(sorted(cc_tz.items())),
        "ccCenter": dict(sorted(cc_center.items())),
        "stateCenter": state_center,
    }


def holdout_report(core, rest, lex):
    """用「全量拟合的表」在另一半数据上复现官方 tz / g 的比例（表本身是全量拟合的，
    所以这里的数字是上限口径，只用于比较规则形态，不当作泛化精度）。"""
    n = core["n"]
    step = lex["cellStep"]
    cell_of = {}
    for key, name in lex["tzCells"]:
        cell_of[key] = name
    state_of = {(cc, st): (lat, lng) for cc, st, lat, lng in lex["stateCenter"]}

    tz_hit = tz_tot = 0
    g_hit = g_tot = 0
    geo_hit = geo_tot = 0
    for i in range(1, n, 2):
        cc = core["countries"][core["c"][i]][0]
        t = rest["tz"][i]
        if t >= 0:
            lat, lng = core["lat"][i] / 100.0, core["lng"][i] / 100.0
            pred = cell_of.get(cell_key(lat, lng, step)) or lex["ccTz"].get(cc)
            tz_tot += 1
            if pred == core["tzs"][t]:
                tz_hit += 1
        tags = (rest["tags"][i] or "").lower()
        if tags:
            best = None
            for raw in tags.split(","):
                hit = lex["tagGenre"].get(raw.strip())
                if hit and (best is None or hit[1] > best[1]):
                    best = hit
            g_tot += 1
            if best and best[0] == core["g"][i]:
                g_hit += 1
        if core["prec"][i] > 0:          # 官方自己地理编码出来的行
            want = state_of.get((cc, norm(rest["state"][i])))
            geo_tot += 1
            if want and abs(want[0] - core["lat"][i]) <= 100 and abs(want[1] - core["lng"][i]) <= 100:
                geo_hit += 1
    return {
        "tz_reproduced": (tz_hit, tz_tot, round(tz_hit / tz_tot, 4) if tz_tot else 0),
        "genre_reproduced": (g_hit, g_tot, round(g_hit / g_tot, 4) if g_tot else 0),
        "geo_reproduced": (geo_hit, geo_tot, round(geo_hit / geo_tot, 4) if geo_tot else 0),
    }


def main():
    core_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "api_core_cn.json")
    rest_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, "api_rest.json")
    out_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "official_lexicon.json")

    with open(core_path, "r", encoding="utf-8") as f:
        core = json.load(f)
    with open(rest_path, "r", encoding="utf-8") as f:
        rest = json.load(f)

    lex = build(core, rest)
    report = holdout_report(core, rest, lex)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(lex, f, ensure_ascii=False, separators=(",", ":"))

    print("词表规模: tag=%d 个, 时区网格=%d 格, 国家时区=%d 个, 国家质心=%d 个, (国家,state)坐标=%d 个" % (
        len(lex["tagGenre"]), len(lex["tzCells"]), len(lex["ccTz"]), len(lex["ccCenter"]),
        len(lex["stateCenter"])))
    print("官方口径复现率(全量拟合表回测一半数据): tz=%s 流派=%s 坐标=%s" % (
        report["tz_reproduced"], report["genre_reproduced"], report["geo_reproduced"]))
    print("已写出 %s (%d 字节)" % (out_path, os.path.getsize(out_path)))


if __name__ == "__main__":
    main()

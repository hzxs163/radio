#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备用数据源：radio-browser.info -> roamdial 快照（官方口径）

主源 (roamdial.com) 拉取失败时由 GitHub Actions 调用本脚本，从 Radio Browser 公开 API
拉全量电台，转成 api_core_cn.json / api_rest.json。

「官方口径」不是猜的：官方 id 就是 Radio Browser stationuuid 的前 12 位十六进制，
所以能把仓库里的官方快照和实时 RB 数据按 id 连起来逐字段核对（核对结论写在各字段
注释里），再用 .github/scripts/official_lexicon.json（由 build_lexicon.py 从官方快照
反推）把语言/流派/时区/坐标投影到官方那套字典上。备用源顶上来时，前端的筛选列表、
地图配色、当地时间才不会变形。

自检（全离线，几秒完成；daily-refresh 在调用本脚本前会先跑它）:
  python3 -m unittest discover -s .github/scripts -p "test_*.py"

用法:
  python3 fetch_radio_browser.py [输出目录]            # 联网全量
  RDSAMPLE=样本.json python3 fetch_radio_browser.py out/  # 离线（自检/本地复现）
"""
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone

RB_HOSTS = ["de1.api.radio-browser.info", "de2.api.radio-browser.info",
            "fi1.api.radio-browser.info", "at1.api.radio-browser.info"]
UA = "RoamDialMirror/1.0 (fallback snapshot builder)"
PAGE_LIMIT = 1000

# 官方快照里的长度上限（逐行量出来的：name 有 1,047 行正好 64、tags 上限 40、state 上限 32，
# 且三者都不含制表符/连续空格 -> 先折叠空白再截断，残留的首尾空格是截断产物）。
NAME_MAX, TAGS_MAX, STATE_MAX = 64, 40, 32

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- ISO 639 码 / 语言名 -> 官方 langsEn 里的规范名 ----
# 只收录「官方 langsEn 里有」的映射：认不出但确实标了语言 -> 「其他」，什么都没标 -> 「未标注」。
# 这条兜底是配对样本量出来的：官方落在 Unlabeled 的 107 行，RB 侧 codes+language 全为空；
# 落在「其他」的行（"aka bo"、"sing along" 之类）都是两层表都认不出来的自由文本。
# 表里没有的 ISO 639-1 码（afar/ewé/tigrinya...）官方同样没有对应语言项，落到「其他」正好。
CODE_LANG = {
    "ar": "Arabic", "az": "Azerbaijani", "ba": "Bashkir", "be": "Belarusian",
    "bg": "Bulgarian", "bm": "Bambara", "bn": "Bengali", "bo": "Tibetan",
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "ceb": "Cebuano",
    "cmn": "Chinese", "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian", "eu": "Basque",
    "fa": "Persian", "fi": "Finnish", "fo": "Faroese", "fr": "French", "ga": "Other",
    "gl": "Galician", "gn": "Guarani", "ha": "Hausa", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "ht": "Creole", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada", "ko": "Korean",
    "ku": "Kurdish", "ky": "Kyrgyz", "la": "Latin", "lb": "Luxembourgish",
    "lg": "Luganda", "lt": "Lithuanian", "mk": "Macedonian", "ml": "Malayalam",
    "mn": "Mongolian", "ms": "Malay", "mt": "Maltese", "my": "Burmese", "nan": "Hokkien",
    "nb": "Norwegian", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian",
    "no": "Norwegian", "oc": "Occitan", "or": "Odia", "pa": "Punjabi", "pl": "Polish",
    "ps": "Pashto", "pt": "Portuguese", "qu": "Quechua", "rm": "Romansh",
    "ro": "Romanian", "ru": "Russian", "si": "Sinhala", "sk": "Slovak",
    "sl": "Slovenian", "sq": "Albanian", "sr": "Serbian", "sv": "Swedish",
    "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "th": "Thai", "tk": "Turkmen",
    "tl": "Filipino", "tr": "Turkish", "tt": "Tatar", "ug": "Uyghur", "uk": "Ukrainian",
    "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "yue": "Cantonese",
    "zh": "Chinese", "zu": "Other",
    # Radio Browser 偶尔填 ISO 639-2 的三字母码（样本里出现过 spa/ger/cmn/yue/tgl...）
    "ara": "Arabic", "aze": "Azerbaijani", "bel": "Belarusian", "ben": "Bengali",
    "bos": "Bosnian", "bul": "Bulgarian", "cat": "Catalan", "ces": "Czech",
    "cha": "Other", "chi": "Chinese", "cym": "Welsh", "dan": "Danish", "deu": "German",
    "ell": "Greek", "eng": "English", "epo": "Other", "est": "Estonian",
    "eus": "Basque", "fao": "Faroese", "fas": "Persian", "fil": "Filipino",
    "fin": "Finnish", "fra": "French", "fre": "French", "ger": "German",
    "glg": "Galician", "grn": "Guarani", "hat": "Creole", "hau": "Hausa",
    "heb": "Hebrew", "hin": "Hindi", "hrv": "Croatian", "hun": "Hungarian",
    "hye": "Armenian", "ind": "Indonesian", "isl": "Icelandic", "ita": "Italian",
    "jav": "Javanese", "jpn": "Japanese", "kan": "Kannada", "kaz": "Kazakh",
    "khm": "Khmer", "kir": "Kyrgyz", "kor": "Korean", "kur": "Kurdish",
    "lav": "Latvian", "lin": "Other", "lit": "Lithuanian", "ltz": "Luxembourgish",
    "lug": "Luganda", "mal": "Malayalam", "mkd": "Macedonian", "mon": "Mongolian",
    "msa": "Malay", "mlt": "Maltese", "mya": "Burmese", "nep": "Nepali",
    "nld": "Dutch", "nob": "Norwegian", "oci": "Occitan", "pan": "Punjabi",
    "per": "Persian", "pol": "Polish", "por": "Portuguese", "pus": "Pashto",
    "que": "Quechua", "rum": "Romanian", "ron": "Romanian", "rus": "Russian",
    "sin": "Sinhala", "slk": "Slovak", "slv": "Slovenian", "spa": "Spanish",
    "srp": "Serbian", "swe": "Swedish", "tam": "Tamil", "tel": "Telugu",
    "tgl": "Filipino", "tha": "Thai", "tib": "Tibetan", "tuk": "Turkmen",
    "tur": "Turkish", "urd": "Urdu", "uzb": "Uzbek", "vie": "Vietnamese",
    "yid": "Other", "cnr": "Montenegrin", "amh": "Amharic", "hok": "Hokkien",
}
# RB 的 language 自由文本：先整串精确查，查不到再按词切分逐词查（"swiss german"、
# "español chile"、"american english" 这类带地区/方言后缀的写法在样本里占了一大块）。
NAME_LANG = {
    "afrikaans": "Afrikaans", "albanian": "Albanian", "amharic": "Amharic",
    "arabic": "Arabic", "armenian": "Armenian", "azerbaijani": "Azerbaijani",
    "bashkir": "Bashkir", "basque": "Basque", "belarusian": "Belarusian",
    "bengali": "Bengali", "bhojpuri": "Bhojpuri", "bosnian": "Bosnian",
    "breton": "Breton", "bulgarian": "Bulgarian", "burmese": "Burmese",
    "cantonese": "Cantonese", "catalan": "Catalan", "cebuano": "Cebuano",
    "chinese": "Chinese", "中文": "Chinese", "普通话": "Chinese", "creole": "Creole",
    "croatian": "Croatian", "czech": "Czech", "danish": "Danish", "deutsch": "German",
    "dutch": "Dutch", "english": "English", "espanol": "Spanish",
    "estonian": "Estonian", "farsi": "Persian",
    "faroese": "Faroese", "filipino": "Filipino", "finnish": "Finnish",
    "francais": "French", "galician": "Galician",
    "georgian": "Georgian", "german": "German", "guarani": "Guarani",
    "hausa": "Hausa", "hebrew": "Hebrew", "hindi": "Hindi", "hokkien": "Hokkien",
    "hungarian": "Hungarian", "icelandic": "Icelandic", "indonesian": "Indonesian",
    "italian": "Italian", "japanese": "Japanese", "javanese": "Javanese",
    "kannada": "Kannada", "kazakh": "Kazakh", "khmer": "Khmer", "korean": "Korean",
    "kurdish": "Kurdish", "kyrgyz": "Kyrgyz", "latin": "Latin", "latvian": "Latvian",
    "lithuanian": "Lithuanian", "luganda": "Luganda", "luxembourgish": "Luxembourgish",
    "macedonian": "Macedonian", "malay": "Malay", "malayalam": "Malayalam",
    "maltese": "Maltese", "mandarin": "Chinese", "mongolian": "Mongolian",
    "montenegrin": "Montenegrin", "nepali": "Nepali", "norwegian": "Norwegian",
    "occitan": "Occitan", "odia": "Odia", "oriya": "Odia", "pashto": "Pashto",
    "persian": "Persian", "polish": "Polish", "portuguese": "Portuguese",
    "portugues": "Portuguese", "punjabi": "Punjabi", "quechua": "Quechua",
    "romanian": "Romanian", "romansh": "Romansh", "russian": "Russian",
    "serbian": "Serbian", "sinhala": "Sinhala", "slovak": "Slovak",
    "slovenian": "Slovenian", "spanish": "Spanish", "swahili": "Swahili",
    "swedish": "Swedish", "tagalog": "Filipino", "tamil": "Tamil", "tatar": "Tatar",
    "telugu": "Telugu", "thai": "Thai", "tibetan": "Tibetan", "turkish": "Turkish",
    "turkmen": "Turkmen", "ukrainian": "Ukrainian", "urdu": "Urdu",
    "uyghur": "Uyghur", "uighur": "Uyghur", "uzbek": "Uzbek",
    "vietnamese": "Vietnamese", "welsh": "Welsh", "castellano": "Spanish",
    "nederlands": "Dutch", "suomi": "Finnish", "ingles": "English",
}
# 样本里 RB 拼错的写法（"engilsh" 7 行、"gernan" 2 行、"deutch" 1 行、"espanish" 1 行）
TYPO_LANG = {"engilsh": "English", "gernan": "German", "deutch": "German",
             "espanish": "Spanish", "frances": "French"}
# 逐词切分用的分隔符：非字母数字、非汉字一律切开。输入先经 fold() 去过重音，
# 所以 "español chile" 折成 "espanol chile"、"português (brasil)" 折成 "portugues (brasil)"，
# 词干都能在 NAME_LANG 里命中（这类带地区后缀的写法在样本里有几十行）。
WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def fold(text):
    """去掉重音符号（NFD 分解后丢弃组合符），汉字等非拉丁字符原样保留。"""
    d = unicodedata.normalize("NFD", str(text or ""))
    return "".join(ch for ch in d if not unicodedata.combining(ch))


def clean(value, cap=None):
    """官方写法：折叠所有空白再截断（cap 省略则不截断）。"""
    s = " ".join(str(value or "").split())
    return s if cap is None else s[:cap]


def short_id(uuid):
    """官方 id = stationuuid 去掉连字符后的前 12 位十六进制（sw.js 也按这个前缀回查）。"""
    h = "".join(str(uuid or "").split("-")).lower()
    head = h[:12]
    return head if len(h) >= 12 and all(c in "0123456789abcdef" for c in head) else ""


def pick_genre(tags_raw, lex):
    """官方只有 11 个流派桶。词表覆盖不到的 tag 一律落 Mixed——实测「未命中判 Mixed」
    复现官方 85.4%，而补手写关键词只有 77.7%，所以这里刻意不加规则。"""
    table = lex["tagGenre"]
    best = None
    for raw in str(tags_raw or "").lower().split(","):
        hit = table.get(raw.strip())
        if hit and (best is None or hit[1] > best[1]):
            best = hit
    return best[0] if best else lex["untaggedGenre"]


def lang_index(name, lex):
    """语言规范名 -> 官方 langsEn 下标；不在官方表里就返回 None。"""
    return lex["langPos"].get(str(name).lower())


def lookup_lang(text, lex):
    """RB 的一个语言片段（码或词）-> 官方 langsEn 下标，认不出返回 None。

    先整串精确查，查不到再逐词查：RB 的自由文本里一堆带地区/方言后缀的写法
    （"swiss german"、"español chile"、"american english"、"português (brasil)"），
    逐词查能把它们归到主干语言上，而不必把每种拼法都枚举进表。"""
    text = fold(text)
    for probe in (text,) + tuple(WORD_RE.split(text)):
        if not probe:
            continue
        name = CODE_LANG.get(probe) or NAME_LANG.get(probe) or TYPO_LANG.get(probe)
        if name:
            idx = lang_index(name, lex)
            return idx if idx is not None else lex["otherLang"]
    return None


def pick_lang(codes_raw, language_raw, lex):
    """自由文本 language 优先、其次 languagecodes。

    顺序是配对样本量出来的：129 行 codes="de,en" 的官方 l 是 English，而它 RB 的
    language 写的是 "english,german" —— 官方取的是 language 的第一个词，不是码表的
    第一个。反过来 codes 空、只有自由文本的行也有 140 行，所以两层都得查。
    实测把顺序换过来（原先码优先）语言一致率从 83.2% 升到 92.7%。
    标了语言但两层都认不出 -> 「其他」，什么都没标 -> 「未标注」（配对样本里 107+2 行，
    无一例外）。"""
    for src in (language_raw, codes_raw):
        for part in str(src or "").lower().split(","):
            part = part.strip()
            if not part:
                continue
            idx = lookup_lang(part, lex)
            if idx is not None:
                return idx
    if str(codes_raw or "").strip() or str(language_raw or "").strip():
        return lex["otherLang"]
    return lex["unlabeledLang"]


def resolve_coords(lat, lng, cc, state_key, lex):
    """返回 (centidegree lat, centidegree lng, prec)；坐标为 None 表示这行没法定位、要丢掉。

    官方 prec 的含义是从配对样本里对出来的，几乎是确定性的：
      0 = Radio Browser 自带坐标（804 个有坐标的配对行里，802 行的官方坐标与 RB 坐标
          按百分度取整后逐位相同；剩下 2 行是 RB 事后改过坐标的陈旧条目）
      1/2/3 = RB 没坐标、官方自己对地名做地理编码，层级越高越粗。
    分档用的是「(国家,state) 在不在官方算过的点表里」，不是「state 空不空」：无坐标的
    750 个配对行里这样判对 664 行，按 state 空不空只判对 580 行（state 有值但官方认不
    出来的 90 行，官方其实落到了国家级）。表里 prec=2 的 268 行远多于 prec=1 的 45 行，
    所以命中表一律写 2；「城市级=1」要靠 gazetteer，复现不了。
    坐标先查表（无坐标行命中 42%，命中后落在 1° 内 52.7%，比国家级质心的 9.5% 准
    2.4 倍），查不到退国家级质心。"""
    if lat is not None and lng is not None:
        return int(round(lat * 100)), int(round(lng * 100)), 0
    pt = lex["stateCenter"].get((cc, state_key)) if state_key else None
    if pt:
        return pt[0], pt[1], 2
    center = lex["ccCenter"].get(cc)
    if not center:
        return None, None, None
    return center[0], center[1], 3


def resolve_tz(lat_deg, lng_deg, cc, lex):
    """先查坐标落到的 2° 网格，再退国家多数派；都没有返回 None（官方也有 16.6% 是 -1）。
    网格用 2°：真留出实测 2°(92.1%) 比 1°(90.5%) 又准又小。"""
    step = lex["cellStep"]
    span = int(360 / step)
    key = (int(math.floor(lat_deg / step)) + 90) * span + (int(math.floor(lng_deg / step)) + 180)
    return lex["tzCellMap"].get(key) or lex["ccTz"].get(cc)


def stream_url(station):
    """官方写法：取 url_resolved；是 http 且 Radio Browser 没报 SSL 错误时升级成 https
    （配对样本里官方升级的 220 行全部 ssl_error=0，没有一例是 ssl_error=1 还升级的）。
    页面本身跑在 https 上，混合内容会被浏览器拦掉，所以最终只保留 https 地址。
    查询参数不剥——官方快照 33,884 行里有 4,394 行带着 query。"""
    u = clean(station.get("url_resolved") or station.get("url"))
    if u.lower().startswith("http://") and not station.get("ssl_error"):
        u = "https://" + u[7:]
    return u if u.lower().startswith("https://") else ""


def pop_of(station):
    """官方 pop 全部落在 0..9（33,884 行没有一个 >=10），量出来是 log10 热度分档：
    min(9, floor(2*log10(max(votes, clickcount)+1))) 与官方逐行一致 70.5%、±1 档内 98.5%，
    系数 k=2 是明显尖峰（k=1.75 只有 54.2%、k=2.25 掉到 44.1%）。"""
    heat = max(int(station.get("votes") or 0), int(station.get("clickcount") or 0))
    return min(9, int(math.floor(2 * math.log10(heat + 1))))


def flags_of(station, url):
    """bit0=hls, bit1=cors 可用, bit2=https, bit3=cors 已探。
    hls 与 https 按官方口径量出来是逐行一致（hls 1,555/1,555）。当前 Radio Browser API
    已不再返回 cors 字段，所以 bit1/bit3 留空 -> 前端 fl&8==0 走「cors 未知」的乐观分支，
    实际播放线路仍由 /api/stations/<id> 现场探测决定，不会因此播不出。"""
    fl = 4 if url.lower().startswith("https://") else 0
    low = url.lower()
    if station.get("hls") or low.endswith(".m3u8") or ".m3u8?" in low:
        fl |= 1
    return fl


def country_entry(cc):
    """两位大写国家码是否可用——前端的国旗组件只认 ISO 3166-1 alpha-2。"""
    return len(cc) == 2 and cc.isascii() and cc.isalpha()


def to_rows(stations, lex):
    """把 RB 原始行投影成官方口径的行记录，并按官方过滤条件筛掉不可用项。"""
    cc_pos = {c[0]: i for i, c in enumerate(lex["countries"])}
    extras = {}
    rows, seen = [], set()
    dropped = {"no_url": 0, "no_name": 0, "dead": 0, "bad_id": 0, "dup": 0,
               "no_cc": 0, "no_place": 0}
    for s in stations:
        url = stream_url(s)
        if not url:
            dropped["no_url"] += 1      # 非 https 且不可升级：页面是 https，混合内容必被拦
            continue
        name = clean(s.get("name"), NAME_MAX)
        if not name:
            dropped["no_name"] += 1
            continue
        if int(s.get("lastcheckok") or 0) != 1:
            dropped["dead"] += 1        # 主源口径：只收 Radio Browser 校验通过的线路
            continue
        sid = short_id(s.get("stationuuid"))
        if not sid:
            dropped["bad_id"] += 1
            continue
        if sid in seen:
            dropped["dup"] += 1
            continue
        cc = str(s.get("countrycode") or "").strip().upper()
        if not country_entry(cc):
            dropped["no_cc"] += 1
            continue
        state = clean(s.get("state"), STATE_MAX)
        lat, lng = s.get("geo_lat"), s.get("geo_long")
        lat = float(lat) if isinstance(lat, (int, float)) and not isinstance(lat, bool) else None
        lng = float(lng) if isinstance(lng, (int, float)) and not isinstance(lng, bool) else None
        lat_c, lng_c, prec = resolve_coords(lat, lng, cc, state.lower(), lex)
        if lat_c is None:
            dropped["no_place"] += 1    # 官方快照没有一行缺坐标，缺了就没法进地图
            continue
        if cc in cc_pos:
            c_idx = cc_pos[cc]
        else:
            if cc not in extras:
                en = clean(s.get("country"), 40) or cc
                extras[cc] = [cc, en, en]
            c_idx = None
        tz_name = resolve_tz(lat_c / 100.0, lng_c / 100.0, cc, lex)
        rows.append({
            "id": sid, "name": name, "url": url,
            "tags": clean(s.get("tags"), TAGS_MAX), "state": state,
            "g": pick_genre(s.get("tags"), lex),
            "l": pick_lang(s.get("languagecodes"), s.get("language"), lex),
            "cc": cc, "c": c_idx, "lat": lat_c, "lng": lng_c, "prec": prec,
            "pop": pop_of(s), "br": max(0, int(s.get("bitrate") or 0)),
            "fl": flags_of(s, url),
            "tz": lex["tzPos"][tz_name] if tz_name in lex["tzPos"] else -1,
        })
        seen.add(sid)

    # 国家表：官方 209 项原序保留（前端的筛选列表和配色都按它排），官方没有的补在最后。
    countries = list(lex["countries"])
    pos = dict(cc_pos)
    for cc in sorted(extras):
        pos[cc] = len(countries)
        countries.append(extras[cc])
    for r in rows:
        if r["c"] is None:
            r["c"] = pos[r["cc"]]
    return rows, countries, dropped


def build_snapshot(rows, countries, lex, built_at):
    core = {
        "rg": "cn", "v": 3, "builtAt": built_at, "n": len(rows),
        "tzs": lex["tzs"], "genres": lex["genres"], "genresEn": lex["genresEn"],
        "langs": lex["langs"], "langsEn": lex["langsEn"], "countries": countries,
        "lat": [], "lng": [], "prec": [], "g": [], "l": [], "c": [],
        "pop": [], "br": [], "fl": [],
    }
    rest = {"builtAt": built_at, "rg": "cn", "tz": [], "id": [], "name": [],
            "url": [], "tags": [], "state": []}
    for r in rows:
        for key in ("lat", "lng", "prec", "g", "l", "c", "pop", "br", "fl"):
            core[key].append(r[key])
        for key in ("tz", "id", "name", "url", "tags", "state"):
            rest[key].append(r[key])
    return core, rest


def fetch_json(path, timeout=60):
    last = None
    for host in RB_HOSTS:
        try:
            req = urllib.request.Request("https://%s/json/%s" % (host, path),
                                         headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 单个镜像挂了属常态，换下一个
            last = e
    raise RuntimeError("Radio Browser 全部节点不可达(%s): %s" % (path, last))


def fetch_all():
    """分页拉全量。order=stationuuid 保证翻页稳定，也让同一批数据每次都产出同样的行序。"""
    out, offset = [], 0
    while True:
        page = fetch_json("stations?hidebroken=true&limit=%d&offset=%d&order=stationuuid&reverse=false"
                          % (PAGE_LIMIT, offset))
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        sys.stderr.write("已拉取 %d 条...\n" % len(out))
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.3)
    return out


def load_lexicon(path=None):
    if path is None:
        path = os.path.join(HERE, "official_lexicon.json")
    with open(path, "r", encoding="utf-8") as f:
        lex = json.load(f)
    lex["tzCellMap"] = {int(k): v for k, v in lex["tzCells"]}
    lex["tzPos"] = {name: i for i, name in enumerate(lex["tzs"])}
    lex["langPos"] = {name.lower(): i for i, name in enumerate(lex["langsEn"])}
    lex["stateCenter"] = {(cc, st): [lat, lng] for cc, st, lat, lng in lex["stateCenter"]}
    missing = [n for n in ("Unlabeled", "Other") if n.lower() not in lex["langPos"]]
    if missing:
        raise RuntimeError("词表缺少官方语言项 %s，请用 build_lexicon.py 重新生成" % missing)
    lex["unlabeledLang"] = lex["langPos"]["unlabeled"]
    lex["otherLang"] = lex["langPos"]["other"]
    return lex


def stations_from_source():
    """RDSAMPLE 既可以指原始 RB 数组，也可以直接指 fixtures/rb_pairs.json（取里面的 rows）。"""
    sample = os.environ.get("RDSAMPLE")
    if not sample:
        return fetch_all()
    with open(sample, "r", encoding="utf-8") as f:
        data = json.load(f)
    stations = data["rows"] if isinstance(data, dict) else data
    sys.stderr.write("离线样本模式: %s (%d 行)\n" % (sample, len(stations)))
    return stations


def built_at_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    outdir = argv[0] if argv else "."
    lex = load_lexicon()
    rows, countries, dropped = to_rows(stations_from_source(), lex)
    if len(rows) < 100:
        raise RuntimeError("有效电台过少(%d)，疑似上游异常，中止提交" % len(rows))
    core, rest = build_snapshot(rows, countries, lex, built_at_utc())

    os.makedirs(outdir, exist_ok=True)
    for name, data in (("api_core_cn.json", core), ("api_rest.json", rest)):
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    sys.stderr.write("过滤明细: %s\n" % json.dumps(dropped, ensure_ascii=False))
    sys.stderr.write("已生成 api_core_cn.json / api_rest.json: n=%d 国家=%d builtAt=%s\n"
                     % (core["n"], len(countries), core["builtAt"]))


if __name__ == "__main__":
    main()

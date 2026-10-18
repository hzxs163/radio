#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_radio_browser.py 的离线回归测试（只用标准库，不联网）。

跑法:
  py -3 -m unittest discover -s .github/scripts -p "test_*.py"   # Windows 本机
  python3 -m unittest discover -s .github/scripts -p "test_*.py" # Actions

fixtures/rb_pairs.json 是 2026-10-18 官方快照与 Radio Browser 实时数据的 466 行配对样本
（连接键：官方 id == stationuuid 去掉连字符后的前 12 位十六进制），want 里存的是官方那一行
的原值。字段级断言写的是「用同一套规则重放这些 RB 行能复现官方多少」，阈值取实测值再留
5 个百分点余量：RB 事后漂移（换流地址、改 tags、清空语言标注）那部分规则再对也复现不出来，
所以阈值不是 100%。改动规则后如果某个字段掉下阈值，测试会打印实际比例。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fetch_radio_browser as frb  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "rb_pairs.json")


def mini_lex():
    """给纯函数测试用的最小词表，形状和 load_lexicon() 产出的一致。"""
    langs_en = ["Unlabeled", "English", "Spanish", "German", "Other"]
    return {
        "langsEn": langs_en,
        "langPos": {n.lower(): i for i, n in enumerate(langs_en)},
        "unlabeledLang": 0,
        "otherLang": 4,
        "genres": ["流行"],
        "genresEn": ["Pop"],
        "tagGenre": {"rock": [0, 90], "pop": [0, 60]},
        "untaggedGenre": 0,
        "tzs": ["Europe/Berlin", "America/New_York"],
        "tzPos": {"Europe/Berlin": 0, "America/New_York": 1},
        "tzCellMap": {},
        "ccTz": {},
        "cellStep": 2.0,
        "ccCenter": {"DE": [5100, 1000]},
        "stateCenter": {("DE", "bayern"): [4850, 1150]},
        "countries": [["DE", "德国", "Germany"]],
    }


class PureFunctionTest(unittest.TestCase):
    def test_clean_collapses_whitespace_then_truncates(self):
        self.assertEqual(frb.clean("  a\tb\n c   d"), "a b c d")
        self.assertEqual(frb.clean("radio  one", 5), "radio")
        self.assertEqual(frb.clean(None), "")
        self.assertEqual(frb.clean(128000), "128000")

    def test_short_id_is_12_hex_prefix(self):
        self.assertEqual(frb.short_id("f82f3fed-620c-1d63-e75b-df74aa89c407"), "f82f3fed620c")
        self.assertEqual(frb.short_id("F82F3FED620C1D63E75BDF74AA89C407"), "f82f3fed620c")
        self.assertEqual(frb.short_id("not-a-uuid"), "")
        self.assertEqual(frb.short_id(""), "")
        self.assertEqual(frb.short_id(None), "")
        self.assertEqual(frb.short_id("abcdefghij" + "k" * 22), "")   # 非十六进制

    def test_country_entry_accepts_only_ascii_alpha2(self):
        self.assertTrue(frb.country_entry("DE"))
        self.assertFalse(frb.country_entry("D"))
        self.assertFalse(frb.country_entry("DEU"))
        self.assertFalse(frb.country_entry("1"))
        self.assertFalse(frb.country_entry("D1"))
        self.assertFalse(frb.country_entry(""))

    def test_pop_is_log_bucket_capped_at_nine(self):
        self.assertEqual(frb.pop_of({"votes": 0, "clickcount": 0}), 0)
        self.assertEqual(frb.pop_of({"votes": 2}), 0)
        self.assertEqual(frb.pop_of({"votes": 9}), 2)     # floor(2*log10(10))
        self.assertEqual(frb.pop_of({"votes": 100}), 4)
        self.assertEqual(frb.pop_of({"votes": 999999}), 9)
        # clickcount 与 votes 取大的那个（官方 33,884 行的 pop 全部落在 0..9）
        self.assertEqual(frb.pop_of({"votes": 1, "clickcount": 500}), 5)
        self.assertEqual(frb.pop_of({}), 0)

    def test_stream_url_upgrades_http_only_without_ssl_error(self):
        self.assertEqual(
            frb.stream_url({"url_resolved": "http://a.fm/x", "ssl_error": 0}), "https://a.fm/x")
        self.assertEqual(frb.stream_url({"url_resolved": "http://a.fm/x", "ssl_error": 1}), "")
        self.assertEqual(frb.stream_url({"url_resolved": "https://a.fm/x?y=1"}), "https://a.fm/x?y=1")
        self.assertEqual(frb.stream_url({"url": "ftp://a.fm/x"}), "")
        self.assertEqual(frb.stream_url({}), "")

    def test_flags_bits(self):
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x") & 1, 0)
        self.assertEqual(frb.flags_of({"hls": 1}, "https://a.fm/x") & 1, 1)
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x.m3u8") & 1, 1)
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x.m3u8?tok=1") & 1, 1)
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x.m3u8x") & 1, 0)  # 不是后缀就别认
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x") & 4, 4)
        self.assertEqual(frb.flags_of({"hls": 0}, "https://a.fm/x") & 10, 0)  # cors 位留空

    def test_pick_genre_uses_table_and_falls_back_to_mixed(self):
        lex = mini_lex()
        lex["untaggedGenre"] = 0
        self.assertEqual(frb.pick_genre("Rock, Classics", lex), 0)
        self.assertEqual(frb.pick_genre("", lex), 0)          # 空 tags -> Mixed 桶
        self.assertEqual(frb.pick_genre("unknownstyle", lex), 0)

    def test_pick_lang_prefers_free_text_then_codes(self):
        lex = mini_lex()
        self.assertEqual(frb.pick_lang("de", "german", lex), 3)
        self.assertEqual(frb.pick_lang("", "", lex), 0)                      # 什么都没标 -> 未标注
        self.assertEqual(frb.pick_lang("", "klingon", lex), 4)               # 标了但认不出 -> 其他
        self.assertEqual(frb.pick_lang("klingon", "", lex), 4)
        self.assertEqual(frb.pick_lang("de,en", "english,german", lex), 1)   # 官方按 language 首个词

    def test_pick_lang_folds_accents_and_splits_words(self):
        lex = mini_lex()
        self.assertEqual(frb.pick_lang("", "español chile", lex), 2)
        self.assertEqual(frb.pick_lang("", "swiss german", lex), 3)
        self.assertEqual(frb.pick_lang("spa", "", lex), 2)
        self.assertEqual(frb.pick_lang("", "ENGLISH UK", lex), 1)

    def test_resolve_coords_three_levels(self):
        lex = mini_lex()
        self.assertEqual(frb.resolve_coords(52.5, 13.4, "DE", "", lex), (5250, 1340, 0))
        self.assertEqual(frb.resolve_coords(None, None, "DE", "bayern", lex), (4850, 1150, 2))
        self.assertEqual(frb.resolve_coords(None, None, "DE", "", lex), (5100, 1000, 3))
        self.assertEqual(frb.resolve_coords(None, None, "ZZ", "", lex), (None, None, None))

    def test_resolve_tz_cell_then_country_fallback(self):
        lex = mini_lex()
        lex["tzCellMap"] = {frb_tz_cell(52.5, 13.4, 2.0): "Europe/Berlin"}
        self.assertEqual(frb.resolve_tz(52.5, 13.4, "DE", lex), "Europe/Berlin")
        lex["tzCellMap"] = {}
        lex["ccTz"] = {"DE": "Europe/Berlin"}
        self.assertEqual(frb.resolve_tz(52.5, 13.4, "DE", lex), "Europe/Berlin")
        self.assertIsNone(frb.resolve_tz(52.5, 13.4, "ZZ", lex))


def frb_tz_cell(lat, lng, step):
    span = int(360 / step)
    return (int(lat // step) + 90) * span + (int(lng // step) + 180)


class LexiconTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = frb.load_lexicon()

    def test_language_tables_only_point_at_official_names(self):
        for table, label in ((frb.CODE_LANG, "CODE_LANG"), (frb.NAME_LANG, "NAME_LANG"),
                             (frb.TYPO_LANG, "TYPO_LANG")):
            for key, name in table.items():
                self.assertEqual(key, key.strip().lower(), "%s 的键要先折叠小写: %r" % (label, key))
                self.assertIn(name, self.lex["langsEn"],
                              "%s[%r] 指向 %r，但官方 langsEn 里没有这个语言" % (label, key, name))

    def test_lexicon_dictionaries_match_official_shape(self):
        self.assertEqual(len(self.lex["langs"]), len(self.lex["langsEn"]))
        self.assertEqual(len(self.lex["genres"]), len(self.lex["genresEn"]))
        self.assertEqual(len(self.lex["tzs"]), len(set(self.lex["tzs"])))
        self.assertLess(self.lex["untaggedGenre"], len(self.lex["genres"]))
        self.assertEqual(self.lex["langsEn"][self.lex["unlabeledLang"]], "Unlabeled")
        self.assertEqual(self.lex["langsEn"][self.lex["otherLang"]], "Other")

    def test_lexicon_indices_are_in_range(self):
        for tag, (idx, _dom) in self.lex["tagGenre"].items():
            self.assertLess(idx, len(self.lex["genresEn"]))
        for key, name in self.lex["tzCells"]:
            self.assertIn(name, self.lex["tzPos"])
            self.assertIsInstance(key, int)
        for cc, name in self.lex["ccTz"].items():
            self.assertTrue(frb.country_entry(cc))
            self.assertIn(name, self.lex["tzPos"])
        for cc, (lat, lng) in self.lex["ccCenter"].items():
            self.assertTrue(frb.country_entry(cc))
            self.assertTrue(-9000 <= lat <= 9000 and -18000 <= lng <= 18000)
        for (cc, st), (lat, lng) in self.lex["stateCenter"].items():
            self.assertTrue(frb.country_entry(cc))
            self.assertEqual(st, st.strip().lower())
            self.assertTrue(-9000 <= lat <= 9000 and -18000 <= lng <= 18000)


class ToRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = frb.load_lexicon()

    def base(self, **over):
        row = {"stationuuid": "00000000-0000-4000-8000-000000000001", "name": "Test FM",
               "url_resolved": "https://a.fm/x", "url": "https://a.fm/x", "tags": "rock",
               "countrycode": "DE", "country": "Germany", "state": "Bayern",
               "language": "german", "languagecodes": "de", "votes": 10, "bitrate": 128,
               "hls": 0, "ssl_error": 0, "lastcheckok": 1, "geo_lat": 52.5, "geo_long": 13.4}
        row.update(over)
        return row

    def test_keeps_a_plain_row(self):
        rows, countries, dropped = frb.to_rows([self.base()], self.lex)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(dropped.values()), 0)
        self.assertEqual(rows[0]["id"], "000000000000")
        self.assertEqual(rows[0]["prec"], 0)          # RB 给了坐标
        self.assertEqual(rows[0]["cc"], "DE")
        self.assertEqual(countries[:len(self.lex["countries"])], self.lex["countries"])

    def test_drops_dead_non_https_nameless_and_bad_country(self):
        cases = [
            (self.base(lastcheckok=0), "dead"),
            (self.base(url_resolved="http://a.fm/x", ssl_error=1), "no_url"),
            (self.base(name="   "), "no_name"),
            (self.base(countrycode=""), "no_cc"),
            (self.base(countrycode="DEU"), "no_cc"),
            (self.base(stationuuid="zzzz"), "bad_id"),
        ]
        for row, bucket in cases:
            rows, _c, dropped = frb.to_rows([row], self.lex)
            self.assertEqual(rows, [], bucket)
            self.assertEqual(dropped[bucket], 1, bucket)

    def test_duplicate_short_id_dropped(self):
        rows, _c, dropped = frb.to_rows(
            [self.base(), self.base(stationuuid="00000000-0000-4000-8000-000000000099",
                                    name="Other Name")], self.lex)
        self.assertEqual(len(rows), 1)
        self.assertEqual(dropped["dup"], 1)

    def test_caps_applied(self):
        rows, _c, _d = frb.to_rows([self.base(
            name="n" * 100, tags="t" * 100, state="s" * 100)], self.lex)
        self.assertEqual(len(rows[0]["name"]), frb.NAME_MAX)
        self.assertEqual(len(rows[0]["tags"]), frb.TAGS_MAX)
        self.assertEqual(len(rows[0]["state"]), frb.STATE_MAX)

    def test_unknown_country_appended_after_official_list(self):
        official = len(self.lex["countries"])
        rows, countries, _d = frb.to_rows([self.base(countrycode="XX", country="Xland")], self.lex)
        self.assertEqual(len(countries), official + 1)
        self.assertEqual(countries[-1], ["XX", "Xland", "Xland"])
        self.assertEqual(rows[0]["c"], official)
        self.assertEqual(countries[:official], self.lex["countries"])   # 官方 209 项原序不动

    def test_row_without_any_geo_source_dropped(self):
        rows, _c, dropped = frb.to_rows([self.base(countrycode="XQ", geo_lat=None,
                                                   geo_long=None)], self.lex)
        self.assertEqual(rows, [])
        self.assertEqual(dropped["no_place"], 1)


class ParityTest(unittest.TestCase):
    """逐字段对官方快照的复现率。

    fixtures/rb_pairs.json 上当前规则的实测值（括号里是断言阈值）:
      id 100 (100) | cc 100 (100) | hls 位 100 (99) | https 位 100 (99) | name 99.8 (95)
      prec 95.9 (90) | br 97.6 (93) | url 94.2 (90) | state 94.0 (88) | language 90.6 (85)
      tz 83.9 (78) | pop 精确 73.4 (65) / ±1 档 98.7 (93) | 流派 72.7 (65)
    掉下来的都是 Radio Browser 自己的漂移：流地址被换、tags 被改、语言标注被清空
    （官方快照建好之后 RB 又被人编辑过），不是规则错。"""

    @classmethod
    def setUpClass(cls):
        cls.lex = frb.load_lexicon()
        with open(FIXTURE, "r", encoding="utf-8") as f:
            cls.rows = json.load(f)["rows"]
        cls.hit = {}
        cls.total = len(cls.rows)
        hits = {k: 0 for k in ("id", "name", "url", "state", "cc", "br", "pop", "pop1",
                               "fl0", "fl2", "g", "l", "prec", "tz")}
        for r in cls.rows:
            want = r["want"]
            url = frb.stream_url(r)
            cc = str(r.get("countrycode") or "").strip().upper()
            state = frb.clean(r.get("state"), frb.STATE_MAX)
            lat, lng = r.get("geo_lat"), r.get("geo_long")
            lat = float(lat) if isinstance(lat, (int, float)) and not isinstance(lat, bool) else None
            lng = float(lng) if isinstance(lng, (int, float)) and not isinstance(lng, bool) else None
            la, ln, prec = frb.resolve_coords(lat, lng, cc, state.lower(), cls.lex)
            tz = frb.resolve_tz(la / 100.0, ln / 100.0, cc, cls.lex) if la is not None else None
            tz_idx = cls.lex["tzPos"][tz] if tz in cls.lex["tzPos"] else -1
            fl = frb.flags_of(r, url)
            pop = frb.pop_of(r)
            got = {
                "id": frb.short_id(r.get("stationuuid")) == want["id"],
                "name": frb.clean(r.get("name"), frb.NAME_MAX) == want["name"],
                "url": url == want["url"],
                "state": state == want["state"],
                "cc": cc == want["cc"],
                "br": max(0, int(r.get("bitrate") or 0)) == want["br"],
                "pop": pop == want["pop"],
                "pop1": abs(pop - want["pop"]) <= 1,
                "fl0": (fl & 1) == (want["fl"] & 1),
                "fl2": (fl & 4) == (want["fl"] & 4),
                "g": frb.pick_genre(r.get("tags"), cls.lex) == want["g"],
                "l": frb.pick_lang(r.get("languagecodes"), r.get("language"), cls.lex) == want["l"],
                "prec": prec == want["prec"],
                "tz": tz_idx == want["tz"],
            }
            for key in hits:
                if got[key]:
                    hits[key] += 1
        cls.hit = hits

    def rate(self, key):
        return 100.0 * self.hit[key] / self.total

    def assert_rate(self, key, floor):
        actual = self.rate(key)
        self.assertGreaterEqual(
            actual, floor,
            "%s 复现率 %.1f%% 掉到阈值 %d%% 以下（实测基线见 fixtures/rb_pairs.json 说明）"
            % (key, actual, floor))

    def test_parity_rates(self):
        self.assertEqual(self.total, 466)
        for key, floor in (("id", 100), ("cc", 100), ("fl0", 99), ("fl2", 99),
                           ("name", 95), ("url", 90), ("state", 88), ("br", 93),
                           ("prec", 90), ("l", 85), ("tz", 78), ("pop", 65),
                           ("pop1", 93), ("g", 65)):
            self.assert_rate(key, floor)


class SnapshotTest(unittest.TestCase):
    """端到端：RDSAMPLE 指向 fixture 跑一遍，产物必须满足官方分片形状。"""

    @classmethod
    def setUpClass(cls):
        cls.lex = frb.load_lexicon()

    def run_main(self, rows):
        tmp = tempfile.mkdtemp(prefix="frb-test-")
        sample = os.path.join(tmp, "stations.json")
        with open(sample, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        old = os.environ.get("RDSAMPLE")
        os.environ["RDSAMPLE"] = sample
        try:
            frb.main([os.path.join(tmp, "out")])
        finally:
            if old is None:
                os.environ.pop("RDSAMPLE", None)
            else:
                os.environ["RDSAMPLE"] = old
        out = os.path.join(tmp, "out")
        with open(os.path.join(out, "api_core_cn.json"), encoding="utf-8") as f:
            core = json.load(f)
        with open(os.path.join(out, "api_rest.json"), encoding="utf-8") as f:
            rest = json.load(f)
        return core, rest

    def test_shards_are_consistent_and_official_shaped(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        core, rest = self.run_main(rows)

        self.assertEqual(core["builtAt"], rest["builtAt"])
        self.assertEqual(core["rg"], rest["rg"])
        self.assertEqual(core["v"], 3)
        self.assertRegex(core["builtAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        for key in ("lat", "lng", "prec", "g", "l", "c", "pop", "br", "fl"):
            self.assertEqual(len(core[key]), core["n"], key)
        for key in ("tz", "id", "name", "url", "tags", "state"):
            self.assertEqual(len(rest[key]), core["n"], key)

        self.assertEqual(len(set(rest["id"])), core["n"])
        for sid in rest["id"]:
            self.assertRegex(sid, r"^[0-9a-f]{12}$")
        for url in rest["url"]:
            self.assertTrue(url.lower().startswith("https://"), url)
            self.assertLessEqual(len(url), 20000)
        for name, tags, state in zip(rest["name"], rest["tags"], rest["state"]):
            self.assertTrue(name)
            self.assertLessEqual(len(name), frb.NAME_MAX)
            self.assertLessEqual(len(tags), frb.TAGS_MAX)
            self.assertLessEqual(len(state), frb.STATE_MAX)
            for value in (name, tags, state):
                self.assertNotIn("  ", value)
                self.assertNotIn("\t", value)
                self.assertNotIn("\n", value)
        for i in range(core["n"]):
            self.assertIn(core["prec"][i], (0, 1, 2, 3))
            self.assertTrue(0 <= core["pop"][i] <= 9)
            self.assertTrue(-9000 <= core["lat"][i] <= 9000)
            self.assertTrue(-18000 <= core["lng"][i] <= 18000)
            self.assertTrue(0 <= core["g"][i] < len(core["genres"]))
            self.assertTrue(0 <= core["l"][i] < len(core["langs"]))
            self.assertTrue(0 <= core["c"][i] < len(core["countries"]))
            self.assertTrue(-1 <= rest["tz"][i] < len(core["tzs"]))
            self.assertEqual(core["fl"][i] & 4, 4)              # 全 https
        # 前端的国旗/配色按 countries 排，备用源必须原序保留官方那 209 项
        self.assertEqual(core["countries"][:len(self.lex["countries"])], self.lex["countries"])

    def test_main_refuses_too_few_rows(self):
        with open(FIXTURE, "r", encoding="utf-8") as f:
            rows = json.load(f)["rows"][:50]
        with self.assertRaises(RuntimeError):
            self.run_main(rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)

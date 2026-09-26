import json

import pytest

import cleaner
import layout_ids
import mutate
import scanner
from layout_ids import (
    CJK_KLIDS,
    KLID_RE,
    LAYOUT_MAP,
    METADATA_VALUE_NAMES,
    TIP_KLID_RE,
    bcp47_to_klid,
    cjk_map_json,
    decimal_to_hkl,
    hkl_string_forms,
    hkl_to_decimal,
    klid_low_word,
    klid_matches_value,
    klid_to_tags,
    klid_variants,
    normalize_klid_token,
    parse_tip,
)


class TestSingleSourceOfTruth:
    """FIX-8: репозиторий содержит одну таблицу соответствий."""

    def test_scanner_and_cleaner_import_same_table(self):
        assert scanner.LAYOUT_MAP is layout_ids.LAYOUT_MAP
        assert cleaner.LAYOUT_MAP is layout_ids.LAYOUT_MAP

    def test_shared_regexes_are_the_same_objects(self):
        assert scanner._KLID_RE is KLID_RE
        assert scanner._TIP_KLID_RE is TIP_KLID_RE
        # FIX-10 (шаг 2): предикаты сопоставления переехали в mutate.
        assert mutate._TIP_KLID_RE is TIP_KLID_RE
        assert mutate._METADATA_VALUE_NAMES is METADATA_VALUE_NAMES

    def test_cleaner_variants_wrap_resolver(self, monkeypatch):
        """Подмена общего резолвера обязана влиять и на обёртку."""
        monkeypatch.setattr(
            layout_ids, "klid_variants", lambda klid: {"sentinel"}, raising=True
        )
        # cleaner реэкспортирует mutate (FIX-10), поэтому обе ссылки
        # обязаны видеть одну и ту же подмену.
        assert cleaner._klid_variants("00000419") == {"sentinel"}
        assert mutate._klid_variants("00000419") == {"sentinel"}

    def test_cleaner_tags_wrap_resolver(self, monkeypatch):
        monkeypatch.setattr(
            layout_ids, "klid_to_tags", lambda klid: {"sentinel"}, raising=True
        )
        assert cleaner._klid_to_tags("00000419") == {"sentinel"}
        assert mutate._klid_to_tags("00000419") == {"sentinel"}

    def test_scanner_normalizer_wraps_resolver(self, monkeypatch):
        monkeypatch.setattr(
            layout_ids, "normalize_klid_token", lambda raw, key: "sentinel", raising=True
        )
        assert scanner._normalize_klid_token("1033", "") == "sentinel"

    def test_cjk_map_matches_removed_ps_dictionary(self):
        """Карта в .ps1 удалена — здесь она ровно одна."""
        assert CJK_KLIDS == {
            "zh-CN": "00000804",
            "zh-TW": "00000404",
            "ja-JP": "00000411",
            "ko-KR": "00000412",
        }
        assert cleaner._CJK_FALLBACK_KLIDS is CJK_KLIDS

    def test_cjk_klids_returns_copy(self):
        copy = layout_ids.cjk_klids()
        copy["xx-XX"] = "deadbeef"
        assert "xx-XX" not in CJK_KLIDS


class TestResolvers:
    def test_parse_tip_regular(self):
        assert parse_tip("0409:00000409") == "00000409"
        assert parse_tip(" 0419:00000419 ") == "00000419"

    def test_parse_tip_cjk_guid_returns_empty(self):
        """CJK: в tip GUID, а не KLID — резолвер обязан это признать."""
        assert parse_tip("0411:{11111111-2222-3333-4444-555555555555}") == ""

    def test_parse_tip_rejects_garbage(self):
        assert parse_tip("00000409") == ""
        assert parse_tip("") == ""

    def test_klid_variants_covers_hex_and_decimal(self):
        assert {"00000409", "1033"} <= klid_variants("00000409")

    def test_klid_variants_of_hkl_input(self):
        # Десятичная запись HKL 67699721 == 0x04090409
        assert {"67699721", "04090409"} <= klid_variants("67699721")

    def test_klid_variants_of_garbage_is_identity(self):
        assert klid_variants("не-код") == {"не-код"}

    def test_normalize_hkl_prefers_parent_langid(self):
        """LANGID родительского ключа 0x???????? снимает неоднозначность.

        67699721 читается и как hex (0x67699721 -> старшее слово 0x6769), и
        как decimal (0x04090409 -> старшее слово 0x0409). Родитель 0x00000409
        выбирает decimal-прочтение.
        """
        assert normalize_klid_token("67699721", r"CTF\...\0x00000409") == "04090409"

    def test_normalize_falls_back_to_decimal_without_match(self):
        """Без совпадения с родительским LANGID работает документированный
        fallback: чтение как десятичное число (1033 -> 00000409)."""
        assert normalize_klid_token("1033", r"CTF\...\0x00000309") == "00000409"

    def test_hkl_roundtrip(self):
        hkl = decimal_to_hkl(0x0419, 0x0419)
        assert hkl == 68748313
        assert hkl_to_decimal(hkl) == (0x0419, 0x0419)

    def test_hkl_string_forms(self):
        assert hkl_string_forms(0x0419, 0x0419) == {"68748313", "04190419"}

    def test_klid_low_word(self):
        assert klid_low_word("00000419") == 0x0419
        assert klid_low_word("04190419") == 0x0419
        assert klid_low_word("мусор") == 0

    def test_bcp47_to_klid(self):
        assert bcp47_to_klid("ru-RU") == "00000419"
        assert bcp47_to_klid("xx-XX") == ""

    def test_klid_to_tags(self):
        assert "en-US" in klid_to_tags("00000409")

    def test_klid_matches_value_covers_tip_and_tag(self):
        assert klid_matches_value("00000409", "0409:00000409")
        assert klid_matches_value("00000419", "ru-RU")
        assert not klid_matches_value("00000419", "00000409")
        assert not klid_matches_value("00000419", "  ")

    def test_cjk_map_json_roundtrip(self):
        assert json.loads(cjk_map_json()) == CJK_KLIDS


class TestTableIntegrity:
    """Таблица — не «просто словарь»: значения обязаны быть валидны."""

    @pytest.mark.parametrize(("tag", "klid"), sorted(LAYOUT_MAP.items()))
    def test_klid_values_are_canonical(self, tag, klid):
        assert KLID_RE.match(klid), f"{tag}: {klid} не 8 HEX"
        assert normalize_klid_token(klid) == klid.lower()

    def test_reverse_index_matches_forward(self):
        for klid, tags in layout_ids.KLID_TO_TAGS.items():
            for tag in tags:
                assert bcp47_to_klid(tag) == klid, tag

    def test_klid_to_tags_is_symmetric_with_index(self):
        for klid, tags in layout_ids.KLID_TO_TAGS.items():
            assert {t.lower() for t in klid_to_tags(klid)} == set(tags)

    def test_bcp47_lookup_is_case_insensitive(self):
        """Регистр тега не должен решать, найдёт резолвер KLID или нет."""
        assert bcp47_to_klid("RU-ru") == bcp47_to_klid("ru-RU") == "00000419"
        assert bcp47_to_klid("  ru-RU  ") == "00000419"

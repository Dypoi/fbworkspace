"""Uji daftar harga: parsing pricelist, format rupiah, pencarian model/storage."""

from __future__ import annotations

import pytest

from fbworkspace.pricelist import (
    PriceEntry,
    canonical_key,
    default_entries,
    find_entries,
    find_exact,
    format_rupiah,
    is_ready,
    label_for,
    parse_price_line,
    storages_for,
)


class TestFormat:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (1_100_000, "1.100.000"),
            (2_800_000, "2.800.000"),
            (0, "0"),
            (None, ""),
        ],
    )
    def test_rupiah(self, amount, expected):
        assert format_rupiah(amount) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("iPhone 11", "iphone 11"),
            ("iphone xr", "iphone xr"),
            ("iPhone SE gen 2", "iphone se gen2"),
            ("iphone se 2020", "iphone se gen2"),
            ("11", "iphone 11"),
        ],
    )
    def test_canonical_key(self, raw, expected):
        assert canonical_key(raw) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("iphone 11", "iPhone 11"),
            ("iphone xr", "iPhone XR"),
            ("iphone se gen2", "iPhone SE (2020)"),
            ("iphone 8", "iPhone 8"),
        ],
    )
    def test_label_for(self, key, expected):
        assert label_for(key) == expected


class TestParsing:
    @pytest.mark.parametrize(
        ("line", "key", "storage", "price"),
        [
            ("- iphone Se gen2 64gb : 1.700.000", "iphone se gen2", 64, 1_700_000),
            ("⁠iphone 8 64gb: 1.100.000", "iphone 8", 64, 1_100_000),
            ("iphone 8 256gb: 1.300.000", "iphone 8", 256, 1_300_000),
            ("iPhone Xr 128 GB - 2.300.000", "iphone xr", 128, 2_300_000),
            ("iphone 11 64gb 2,4jt", "iphone 11", 64, 2_400_000),
            ("iphone 11 256gb 2,8 juta", "iphone 11", 256, 2_800_000),
        ],
    )
    def test_parse_price_line(self, line, key, storage, price):
        entry = parse_price_line(line)
        assert entry is not None
        assert entry.model_key == key
        assert entry.storage == storage
        assert entry.price == price

    def test_parse_line_without_storage(self):
        entry = parse_price_line("iphone 11 - 2.400.000")
        assert entry is not None
        assert entry.model_key == "iphone 11"
        assert entry.storage is None
        assert entry.price == 2_400_000

    @pytest.mark.parametrize("line", ["", "   ", "tanpa harga", "-"])
    def test_parse_invalid(self, line):
        assert parse_price_line(line) is None


class TestLookup:
    def test_default_list_matches_user_pricelist(self):
        entries = default_entries()
        assert len(entries) == 9
        assert find_exact(entries, "iphone 11", 128).price == 2_600_000
        assert find_exact(entries, "iphone se gen2", 64).price == 1_700_000
        assert find_exact(entries, "iphone xr", 128).price == 2_300_000

    def test_storages_sorted(self):
        assert storages_for(default_entries(), "iphone 11") == [64, 128, 256]
        assert storages_for(default_entries(), "iphone 8") == [64, 256]

    def test_unknown_model(self):
        assert find_entries(default_entries(), "iphone 14") == []
        assert find_exact(default_entries(), "iphone 14", 128) is None

    def test_ready_flag(self):
        entries = default_entries() + [
            PriceEntry("iphone 11", "iPhone 11", 512, 3_200_000, ready=False)
        ]
        assert is_ready(entries, "iphone 11", 64) is True
        assert is_ready(entries, "iphone 11", 512) is False

    def test_ready_falls_back_to_model_when_storage_unknown(self):
        entries = default_entries()
        assert is_ready(entries, "iphone 11", 512) is True

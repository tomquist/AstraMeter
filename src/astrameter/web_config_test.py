import json

import pytest

from astrameter.web_config import (
    _validate_config_payload,
    config_to_json,
    read_config_as_dict,
    write_config_from_dict,
)


@pytest.fixture()
def ini_path(tmp_path):
    return str(tmp_path / "test.ini")


# ---------- read_config_as_dict ----------


def test_read_nonexistent_file(ini_path):
    sections, order = read_config_as_dict(ini_path)
    assert sections == {}
    assert order == []


def test_read_simple(ini_path):
    with open(ini_path, "w") as f:
        f.write("[SEC1]\nKEY = val\n\n[SEC2]\nA = 1\n")
    sections, order = read_config_as_dict(ini_path)
    assert order == ["SEC1", "SEC2"]
    assert sections == {"SEC1": {"KEY": "val"}, "SEC2": {"A": "1"}}


def test_read_preserves_key_case(ini_path):
    with open(ini_path, "w") as f:
        f.write("[S]\nMyKey = v\nANOTHER_KEY = w\n")
    sections, _ = read_config_as_dict(ini_path)
    assert "MyKey" in sections["S"]
    assert "ANOTHER_KEY" in sections["S"]


# ---------- _validate_config_payload ----------


def test_validate_good_payload():
    _validate_config_payload({"SEC": {"K": "V"}}, ["SEC"])


def test_validate_order_not_list():
    with pytest.raises(ValueError, match="order"):
        _validate_config_payload({"S": {}}, "S")  # type: ignore[arg-type]


def test_validate_empty_section_name():
    with pytest.raises(ValueError, match="Invalid section name"):
        _validate_config_payload({"": {"K": "V"}}, [""])


def test_validate_section_name_with_bracket():
    with pytest.raises(ValueError, match="Invalid section name"):
        _validate_config_payload({"a]b": {}}, ["a]b"])


def test_validate_section_not_dict():
    with pytest.raises(ValueError, match="must map to an object"):
        _validate_config_payload({"S": "bad"}, ["S"])  # type: ignore[dict-item]


def test_validate_empty_key():
    with pytest.raises(ValueError, match="Invalid key"):
        _validate_config_payload({"S": {"": "v"}}, ["S"])


def test_validate_key_with_newline():
    with pytest.raises(ValueError, match="Invalid key"):
        _validate_config_payload({"S": {"a\nb": "v"}}, ["S"])


def test_validate_value_with_newline():
    with pytest.raises(ValueError, match="Invalid value"):
        _validate_config_payload({"S": {"k": "a\nb"}}, ["S"])


def test_validate_duplicate_order():
    with pytest.raises(ValueError, match="duplicate section names"):
        _validate_config_payload({"S": {"k": "v"}}, ["S", "S"])


# ---------- write_config_from_dict — new file ----------


def test_write_new_file(ini_path):
    write_config_from_dict(
        ini_path,
        {"SEC1": {"A": "1"}, "SEC2": {"B": "2"}},
        ["SEC1", "SEC2"],
    )
    sections, order = read_config_as_dict(ini_path)
    assert order == ["SEC1", "SEC2"]
    assert sections["SEC1"] == {"A": "1"}
    assert sections["SEC2"] == {"B": "2"}


def test_write_new_file_respects_order(ini_path):
    write_config_from_dict(
        ini_path,
        {"SEC2": {"B": "2"}, "SEC1": {"A": "1"}},
        ["SEC1", "SEC2"],
    )
    _, order = read_config_as_dict(ini_path)
    assert order == ["SEC1", "SEC2"]


# ---------- write_config_from_dict — update existing ----------


def test_update_preserves_comments(ini_path):
    with open(ini_path, "w") as f:
        f.write("# top comment\n[SEC]\n# about KEY\nKEY = old\n")
    write_config_from_dict(ini_path, {"SEC": {"KEY": "new"}}, ["SEC"])
    with open(ini_path) as f:
        content = f.read()
    assert "# top comment" in content
    assert "# about KEY" in content
    assert "KEY = new" in content
    assert "old" not in content


def test_update_preserves_semicolon_comments(ini_path):
    with open(ini_path, "w") as f:
        f.write("[SEC]\n; semicolon comment\nKEY = val\n")
    write_config_from_dict(ini_path, {"SEC": {"KEY": "val"}}, ["SEC"])
    with open(ini_path) as f:
        content = f.read()
    assert "; semicolon comment" in content


def test_update_adds_new_key(ini_path):
    with open(ini_path, "w") as f:
        f.write("[SEC]\nA = 1\n")
    write_config_from_dict(ini_path, {"SEC": {"A": "1", "B": "2"}}, ["SEC"])
    sections, _ = read_config_as_dict(ini_path)
    assert sections["SEC"] == {"A": "1", "B": "2"}


def test_update_removes_key(ini_path):
    with open(ini_path, "w") as f:
        f.write("[SEC]\nA = 1\nB = 2\n")
    write_config_from_dict(ini_path, {"SEC": {"A": "1"}}, ["SEC"])
    sections, _ = read_config_as_dict(ini_path)
    assert sections["SEC"] == {"A": "1"}


def test_update_removes_section(ini_path):
    with open(ini_path, "w") as f:
        f.write("[KEEP]\nA = 1\n\n[DROP]\nB = 2\n")
    write_config_from_dict(ini_path, {"KEEP": {"A": "1"}}, ["KEEP"])
    sections, order = read_config_as_dict(ini_path)
    assert order == ["KEEP"]
    assert "DROP" not in sections


def test_update_adds_section(ini_path):
    with open(ini_path, "w") as f:
        f.write("[OLD]\nA = 1\n")
    write_config_from_dict(
        ini_path,
        {"OLD": {"A": "1"}, "NEW": {"B": "2"}},
        ["OLD", "NEW"],
    )
    sections, order = read_config_as_dict(ini_path)
    assert "NEW" in order
    assert sections["NEW"] == {"B": "2"}


def test_update_reorders_sections(ini_path):
    with open(ini_path, "w") as f:
        f.write("[A]\nX = 1\n\n[B]\nY = 2\n\n[C]\nZ = 3\n")
    write_config_from_dict(
        ini_path,
        {"C": {"Z": "3"}, "A": {"X": "1"}, "B": {"Y": "2"}},
        ["C", "A", "B"],
    )
    _, order = read_config_as_dict(ini_path)
    assert order == ["C", "A", "B"]


def test_update_preserves_case(ini_path):
    with open(ini_path, "w") as f:
        f.write("[My_Section]\nMyKey = old\n")
    write_config_from_dict(ini_path, {"My_Section": {"MyKey": "new"}}, ["My_Section"])
    sections, _ = read_config_as_dict(ini_path)
    assert "MyKey" in sections["My_Section"]
    assert sections["My_Section"]["MyKey"] == "new"


# ---------- round-trip ----------


def test_roundtrip_identity(ini_path):
    """Writing back the same data that was read should not change the file."""
    original = "[SEC1]\nA = 1\nB = 2\n\n[SEC2]\nC = 3\n"
    with open(ini_path, "w") as f:
        f.write(original)
    sections, order = read_config_as_dict(ini_path)
    write_config_from_dict(ini_path, sections, order)
    with open(ini_path) as f:
        result = f.read()
    assert result.strip() == original.strip()


def test_roundtrip_with_comments(ini_path):
    original = "# file header\n[SEC]\n# key comment\nKEY = val\n"
    with open(ini_path, "w") as f:
        f.write(original)
    sections, order = read_config_as_dict(ini_path)
    write_config_from_dict(ini_path, sections, order)
    with open(ini_path) as f:
        result = f.read()
    assert result.strip() == original.strip()


# ---------- config_to_json ----------


def test_config_to_json(ini_path):
    with open(ini_path, "w") as f:
        f.write("[SEC]\nK = V\n")
    result = json.loads(config_to_json(ini_path))
    assert result == {"sections": {"SEC": {"K": "V"}}, "order": ["SEC"]}


def test_config_to_json_nonexistent(ini_path):
    result = json.loads(config_to_json(ini_path))
    assert result == {"sections": {}, "order": []}


# ---------- sections in order not in dict are skipped ----------


def test_order_with_extra_names(ini_path):
    """Section names in *order* that aren't in *sections* are ignored."""
    write_config_from_dict(
        ini_path,
        {"A": {"X": "1"}},
        ["MISSING", "A"],
    )
    sections, order = read_config_as_dict(ini_path)
    assert order == ["A"]
    assert sections == {"A": {"X": "1"}}


def test_sections_not_in_order_appended(ini_path):
    """Sections present in *sections* but absent from *order* are appended."""
    write_config_from_dict(
        ini_path,
        {"A": {"X": "1"}, "B": {"Y": "2"}},
        ["A"],
    )
    _, order = read_config_as_dict(ini_path)
    assert "A" in order
    assert "B" in order
    assert order.index("A") < order.index("B")


# ---------- _atomic_write_lines fallbacks ----------


def test_atomic_write_creates_file(ini_path):
    from astrameter.web_config import _atomic_write_lines

    _atomic_write_lines(ini_path, ["hello\n", "world\n"])
    with open(ini_path) as f:
        assert f.read() == "hello\nworld\n"


def test_atomic_write_overwrites(ini_path):
    from astrameter.web_config import _atomic_write_lines

    with open(ini_path, "w") as f:
        f.write("old content\n")
    _atomic_write_lines(ini_path, ["new\n"])
    with open(ini_path) as f:
        assert f.read() == "new\n"

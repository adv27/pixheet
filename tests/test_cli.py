import os
import re
import subprocess
import sys
import zipfile

import pytest
from images import noise
from PIL import Image

from pixheet import cli
from pixheet.cli import main

LINE = re.compile(
    r"^(?P<name>.+)  (?P<ow>\d+)x(?P<oh>\d+) -> (?P<w>\d+)x(?P<h>\d+)  "
    r"(?P<colors>[\d,]+) colors  (?P<cells>[\d,]+) cells  \d+\.\d\ds$"
)


@pytest.fixture
def images(tmp_path):
    paths = []
    for i, size in enumerate([(40, 30), (25, 50)]):
        path = tmp_path / f"img{i}.png"
        noise(*size, 100 + i, seed=i).save(path)
        paths.append(path)
    return paths


def test_single_input_writes_next_to_it(images, capsys):
    assert main([str(images[0])]) == 0
    out = images[0].with_suffix(".xlsx")
    assert zipfile.ZipFile(out).testzip() is None
    line = capsys.readouterr().out.strip()
    m = LINE.match(line)
    assert m, line
    assert m["name"] == str(images[0])
    assert (m["ow"], m["oh"], m["w"], m["h"]) == ("40", "30", "40", "30")
    assert m["colors"] == "100"
    assert m["cells"] == "1,200"


def test_output_option(images, tmp_path):
    dst = tmp_path / "custom.xlsx"
    assert main([str(images[0]), "-o", str(dst)]) == 0
    assert dst.exists()
    assert not images[0].with_suffix(".xlsx").exists()


def test_multiple_inputs(images, capsys):
    assert main([str(p) for p in images]) == 0
    assert all(p.with_suffix(".xlsx").exists() for p in images)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(LINE.match(line) for line in lines)


def test_output_with_multiple_inputs_is_an_error(images, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main([str(p) for p in images] + ["-o", str(tmp_path / "x.xlsx")])
    assert exc.value.code == 2
    assert "single INPUT" in capsys.readouterr().err
    assert not (tmp_path / "x.xlsx").exists()


def test_failure_keeps_going(images, tmp_path, capsys):
    bad = tmp_path / "bad.png"
    bad.write_text("not an image")
    missing = tmp_path / "missing.png"
    assert main([str(images[0]), str(bad), str(missing), str(images[1])]) == 1
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 2
    assert all(p.with_suffix(".xlsx").exists() for p in images)
    assert not bad.with_suffix(".xlsx").exists()
    err = captured.err.splitlines()
    assert len(err) == 2
    assert err[0].startswith(f"pixheet: {bad}: ")
    assert err[1].startswith(f"pixheet: {missing}: ")


def test_quiet(images, capsys):
    assert main([str(images[0]), "-q"]) == 0
    assert capsys.readouterr().out == ""


def test_options_are_passed(tmp_path, capsys):
    src = tmp_path / "t.png"
    Image.new("RGBA", (300, 100), (0, 0, 0, 0)).save(src)
    assert main([str(src), "--max-side", "30", "--background", "#FF0000", "--compact"]) == 0
    assert "300x100 -> 30x10" in capsys.readouterr().out
    zf = zipfile.ZipFile(src.with_suffix(".xlsx"))
    assert ' r="A1"' not in zf.read("xl/worksheets/sheet1.xml").decode()
    assert 'rgb="FFFF0000"' in zf.read("xl/styles.xml").decode()


def test_max_side_zero(tmp_path, capsys):
    src = tmp_path / "t.png"
    Image.new("RGB", (1200, 10), (1, 2, 3)).save(src)
    assert main([str(src), "--max-side", "0"]) == 0
    assert "1200x10 -> 1200x10" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--max-side", "-1"], "argument --max-side: must be >= 0, got -1"),
        (["--max-side", "big"], "argument --max-side: not an integer: 'big'"),
        (["--max-side", "1.5"], "argument --max-side: not an integer: '1.5'"),
        (["--background", "nope"], "argument --background: invalid background color 'nope'"),
        ([], "the following arguments are required: INPUT"),
    ],
)
def test_bad_arguments(images, args, message, capsys):
    argv = [str(images[0]), *args] if args else []
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage: pixheet" in err
    assert message in err


def test_wildcards_are_expanded(images, tmp_path, capsys):
    assert main([str(tmp_path / "img*.png")]) == 0
    assert all(p.with_suffix(".xlsx").exists() for p in images)
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_unmatched_wildcard_is_reported(tmp_path, capsys):
    assert main([str(tmp_path / "nothing*.png")]) == 1
    assert "nothing*.png" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert re.match(r"pixheet \d+\.\d+\.\d+", capsys.readouterr().out)


@pytest.mark.parametrize("module", ["pixheet", "pixheet.cli"])
def test_python_dash_m(images, module):
    proc = subprocess.run(
        [sys.executable, "-m", module, str(images[0])],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert LINE.match(proc.stdout.strip())
    assert images[0].with_suffix(".xlsx").exists()


def test_unencodable_file_name_does_not_stop_the_batch(tmp_path):
    # A console code page that cannot show the name must not crash the summary line.
    first = tmp_path / "\u1ea3nh.png"
    second = tmp_path / "z.png"
    for path in (first, second):
        noise(4, 3, 5).save(path)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "pixheet", str(first), str(second)],
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert first.with_suffix(".xlsx").exists()
    assert second.with_suffix(".xlsx").exists()
    assert rb"\u1ea3nh.png" in proc.stdout  # backslashreplace


def test_wildcard_in_a_folder_with_brackets(tmp_path, capsys):
    folder = tmp_path / "shots [2024]"
    decoy = tmp_path / "shots 2"
    for d in (folder, decoy):
        d.mkdir()
        noise(4, 3, 5).save(d / "x.png")
    assert main([str(folder / "*.png")]) == 0
    assert (folder / "x.xlsx").exists()
    assert not (decoy / "x.xlsx").exists()
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_existing_file_with_brackets_is_not_a_pattern(tmp_path):
    src = tmp_path / "img[1].png"
    noise(4, 3, 5).save(src)
    assert main([str(src)]) == 0
    assert src.with_suffix(".xlsx").exists()


def test_two_inputs_with_the_same_output(tmp_path, capsys):
    png, jpg = tmp_path / "a.png", tmp_path / "a.jpg"
    noise(4, 3, 5).save(png)
    noise(6, 5, 5).save(jpg)
    assert main([str(png), str(jpg)]) == 1
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert "already written from" in captured.err
    with zipfile.ZipFile(tmp_path / "a.xlsx") as zf:
        assert '<dimension ref="A1:D3"/>' in zf.read("xl/worksheets/sheet1.xml").decode()


def test_failed_input_does_not_claim_its_output(tmp_path):
    bad, good = tmp_path / "a.txt", tmp_path / "a.png"
    bad.write_text("not an image")
    noise(4, 3, 5).save(good)
    assert main([str(bad), str(good)]) == 1
    assert (tmp_path / "a.xlsx").exists()


def test_input_that_is_not_a_file_keeps_going(images, capsys):
    assert main([".", str(images[0])]) == 1
    assert images[0].with_suffix(".xlsx").exists()
    assert capsys.readouterr().err.startswith("pixheet: .: ")


def test_output_equal_to_input(images, capsys):
    before = images[0].read_bytes()
    assert main([str(images[0]), "-o", str(images[0])]) == 1
    assert images[0].read_bytes() == before
    assert "is the input image" in capsys.readouterr().err


def test_version_without_package_metadata(monkeypatch):
    def missing(name):
        raise cli.PackageNotFoundError(name)

    monkeypatch.setattr(cli, "version", missing)
    assert cli._version() == "unknown"


def test_background_none(tmp_path, capsys):
    src = tmp_path / "t.png"
    Image.new("RGBA", (3, 2), (10, 20, 30, 0)).save(src)
    assert main([str(src), "--background", "none"]) == 0
    with zipfile.ZipFile(src.with_suffix(".xlsx")) as zf:
        assert 'rgb="FF0A141E"' in zf.read("xl/styles.xml").decode()


def test_error_without_a_message(images, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(cli, "convert", fail)
    assert main([str(images[0])]) == 1
    assert capsys.readouterr().err.strip().endswith(": MemoryError")

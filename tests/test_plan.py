"""Tests of the Tk-free plan of a batch: pixheet._plan."""

import errno
import posixpath
import stat
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import UnidentifiedImageError

from pixheet._plan import (
    CONVERTING,
    DONE,
    FAILED,
    IMAGE_SUFFIXES,
    SKIPPED,
    STATES,
    STOPPED,
    WAITING,
    Converted,
    DropResult,
    Failed,
    Finished,
    Job,
    JobQueue,
    Progress,
    ReplaceQuestion,
    Row,
    Started,
    Stopped,
    Tally,
    apply_message,
    assign_outputs,
    common_folder,
    drop_note,
    eta_text,
    expand_drop,
    failure_detail,
    failure_kind,
    folder_labels,
    format_size,
    group_by_folder,
    is_image,
    middle_ellipsis,
    natural_key,
    path_key,
    path_problem,
    progress_text,
    short_reason,
    status_text,
    subfolder_note,
    summary_line,
)
from pixheet.core import ConvertResult, convert

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")

RESULT = ConvertResult(original_size=(4, 3), size=(4, 3), colors=12, cells=12, seconds=0.5)


def names(paths):
    return [path.name for path in paths]


# --- Paths ---------------------------------------------------------------------------


def test_path_key_is_the_same_for_two_spellings(tmp_path):
    assert path_key(tmp_path / "a" / ".." / "b.png") == path_key(tmp_path / "b.png")
    assert path_key(tmp_path / "." / "b.png") == path_key(str(tmp_path / "b.png"))


@windows
def test_path_key_ignores_the_case_and_the_slash_on_windows():
    assert path_key(Path(r"D:\Photos\IMG.PNG")) == path_key(Path("d:/photos/img.png"))
    assert path_key(Path(r"\\NAS\Share\a.png")) == path_key(Path(r"\\nas\share\A.png"))


def test_natural_key_sorts_as_file_explorer():
    files = ["IMG_10.jpg", "img_2.jpg", "IMG_1.jpg", "IMG_02b.jpg", "a.png", "B.png", "10.png"]
    assert sorted(files, key=natural_key) == [
        "10.png",
        "a.png",
        "B.png",
        "IMG_1.jpg",
        "img_2.jpg",
        "IMG_02b.jpg",
        "IMG_10.jpg",
    ]


def test_natural_key_is_a_total_order_for_names_that_differ():
    # The same key without the tie break: the order of the text decides.
    assert sorted(["IMG_02.jpg", "IMG_2.jpg"], key=natural_key) == ["IMG_02.jpg", "IMG_2.jpg"]
    assert sorted(["img.png", "IMG.png"], key=natural_key) == ["IMG.png", "img.png"]


def test_natural_key_with_vietnamese_names():
    files = ["Đà Lạt 10.jpg", "Hội An.jpg", "Đà Lạt 9.jpg", "đà lạt 1.jpg", "Ảnh 2.png"]
    ordered = sorted(files, key=natural_key)
    assert ordered.index("đà lạt 1.jpg") < ordered.index("Đà Lạt 9.jpg")
    assert ordered.index("Đà Lạt 9.jpg") < ordered.index("Đà Lạt 10.jpg")
    assert sorted(reversed(files), key=natural_key) == ordered


def test_natural_key_does_not_read_other_digits_as_numbers():
    # "²" and "٣" are not ASCII digits. int() must not see them.
    assert sorted(["a².png", "a1.png", "a٣.png"], key=natural_key)[0] == "a1.png"


def test_xlsx_is_not_an_image_suffix():
    assert ".xlsx" not in IMAGE_SUFFIXES
    assert all(suffix == suffix.lower() and suffix.startswith(".") for suffix in IMAGE_SUFFIXES)


def test_is_image_ignores_the_case_of_the_suffix():
    assert is_image(Path("a.PNG"))
    assert is_image(Path("b.Jpeg"))
    assert not is_image(Path("c.CR2"))
    assert not is_image(Path("png"))
    assert not is_image(Path("d.xlsx"))


# --- expand_drop with a fake disk ----------------------------------------------------

FAKE = {
    "/trip": ["IMG_10.jpg", "IMG_2.png", "notes.txt", "Day 2", "Day 10", "desktop.ini"],
    "/trip/Day 2": ["c.png", "raw.CR2", "Night"],
    "/trip/Day 2/Night": ["d.png"],
    "/trip/Day 10": ["e.png"],
    "/empty": [],
    "/converted": ["a.png", "a.xlsx", ".hidden.png.tmp", "Thumbs.db"],
}
FILES = {"/x/readme.md", "/x/one.png", "/x/TWO.JPG"}


def fake_drop(*paths, **kwargs):
    def listdir(folder):
        key = folder.as_posix()
        if key not in FAKE:
            raise PermissionError(errno.EACCES, "Access is denied", str(folder))
        return [folder / name for name in FAKE[key]]

    def isdir(path):
        return path.as_posix() in FAKE or path.as_posix() == "/locked"

    def isfile(path):
        posix = path.as_posix()
        if posix in FILES:
            return True
        parent, _, _ = posix.rpartition("/")
        return parent in FAKE and not isdir(path)

    return expand_drop(
        [Path(p) for p in paths], listdir=listdir, isdir=isdir, isfile=isfile, **kwargs
    )


def posix(paths):
    return [path.as_posix() for path in paths]


def test_drop_of_a_folder_adds_its_images_in_explorer_order():
    result = fake_drop("/trip")
    assert posix(result.images) == ["/trip/IMG_2.png", "/trip/IMG_10.jpg"]
    assert posix(result.ignored) == ["/trip/notes.txt"]  # not desktop.ini
    assert result.skipped_subfolders == {Path("/trip"): 2}
    assert (result.duplicates, result.missing) == (0, 0)


def test_drop_of_a_folder_with_subfolders_recursive():
    result = fake_drop("/trip", recursive=True)
    assert posix(result.images) == [
        "/trip/IMG_2.png",
        "/trip/IMG_10.jpg",
        "/trip/Day 2/c.png",
        "/trip/Day 2/Night/d.png",
        "/trip/Day 10/e.png",
    ]
    assert posix(result.ignored) == ["/trip/notes.txt", "/trip/Day 2/raw.CR2"]
    assert result.skipped_subfolders == {}


def test_drop_of_files_keeps_the_drop_order_and_ignores_other_files():
    result = fake_drop("/x/TWO.JPG", "/x/readme.md", "/x/one.png")
    assert posix(result.images) == ["/x/TWO.JPG", "/x/one.png"]
    assert posix(result.ignored) == ["/x/readme.md"]


def test_drop_ignores_the_outputs_and_hidden_files_of_a_folder_quietly():
    result = fake_drop("/converted")
    assert posix(result.images) == ["/converted/a.png"]
    assert result.ignored == []


def test_drop_of_an_xlsx_file_names_it():
    FILES.add("/x/old.xlsx")
    try:
        assert posix(fake_drop("/x/old.xlsx").ignored) == ["/x/old.xlsx"]
    finally:
        FILES.discard("/x/old.xlsx")


def test_drop_counts_missing_paths_and_folders_that_cannot_be_read():
    result = fake_drop("/nothing/here.png", "/locked", "/x/one.png")
    assert posix(result.images) == ["/x/one.png"]
    assert result.missing == 2


def test_drop_of_an_empty_folder():
    assert fake_drop("/empty") == DropResult()


def test_drop_without_duplicates_in_one_drop():
    result = fake_drop("/trip", "/trip/IMG_2.png", "/trip/./IMG_10.jpg")
    assert posix(result.images) == ["/trip/IMG_2.png", "/trip/IMG_10.jpg"]
    assert result.duplicates == 0


def test_drop_counts_the_known_images_as_duplicates():
    known = {path_key(Path("/trip/IMG_2.png"))}
    result = fake_drop("/trip", "/trip/IMG_2.png", known=known)
    assert posix(result.images) == ["/trip/IMG_10.jpg"]
    assert result.duplicates == 1


def test_include_subfolders_after_a_drop_adds_only_the_new_images():
    first = fake_drop("/trip")
    again = fake_drop(
        *first.skipped_subfolders, recursive=True, known={path_key(p) for p in first.images}
    )
    assert names(again.images) == ["c.png", "d.png", "e.png"]
    assert again.duplicates == 2


# --- expand_drop on the disk ---------------------------------------------------------


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_drop_on_the_disk(tmp_path):
    for name in ("IMG_10.jpg", "IMG_9.PNG", "notes.txt", "Sub/inner.png", "Sub/Deep/x.gif"):
        touch(tmp_path / "Trip" / name)
    image = touch(tmp_path / "single.webp")
    other = touch(tmp_path / "doc.pdf")
    result = expand_drop([tmp_path / "Trip", image, other, tmp_path / "gone.png"])
    assert names(result.images) == ["IMG_9.PNG", "IMG_10.jpg", "single.webp"]
    assert names(result.ignored) == ["notes.txt", "doc.pdf"]
    assert result.skipped_subfolders == {tmp_path / "Trip": 1}
    assert result.missing == 1
    deep = expand_drop([tmp_path / "Trip"], recursive=True)
    assert names(deep.images) == ["IMG_9.PNG", "IMG_10.jpg", "inner.png", "x.gif"]


def test_drop_on_the_disk_with_vietnamese_names(tmp_path):
    folder = tmp_path / "Ảnh gia đình" / "Đà Lạt"
    for name in ("Hồ Xuân Hương 2.jpg", "Hồ Xuân Hương 10.jpg", "Chợ đêm.png"):
        touch(folder / name)
    result = expand_drop([tmp_path / "Ảnh gia đình"], recursive=True)
    assert names(result.images) == ["Chợ đêm.png", "Hồ Xuân Hương 2.jpg", "Hồ Xuân Hương 10.jpg"]
    assert result.images[0].parent == folder


@windows
def test_drop_does_not_follow_a_loop_of_links(tmp_path):
    import subprocess

    touch(tmp_path / "Top" / "a.png")
    link = tmp_path / "Top" / "Loop"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "Top")],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("cannot make a junction")
    try:
        result = expand_drop([tmp_path / "Top"], recursive=True)
    finally:
        link.rmdir()
    assert names(result.images) == ["a.png"]


def test_drop_of_500_images(tmp_path):
    folder = tmp_path / "many"
    for i in range(500):
        touch(folder / f"IMG_{i}.png")
    start = time.perf_counter()
    result = expand_drop([folder])
    assert time.perf_counter() - start < 2
    assert names(result.images) == [f"IMG_{i}.png" for i in range(500)]


# --- assign_outputs ------------------------------------------------------------------


def test_outputs_go_next_to_each_image(tmp_path):
    sources = [tmp_path / "a b" / "pic.png", tmp_path / "x.tar.png"]
    assert assign_outputs(sources) == [tmp_path / "a b" / "pic.xlsx", tmp_path / "x.tar.xlsx"]


def test_outputs_go_to_the_save_to_folder(tmp_path):
    out = tmp_path / "out"
    assert assign_outputs([tmp_path / "a" / "x.png", tmp_path / "b" / "y.jpg"], out) == [
        out / "x.xlsx",
        out / "y.xlsx",
    ]


def test_rule_a_two_folders_and_one_save_to_folder_tag_both(tmp_path):
    out = tmp_path / "out"
    sources = [tmp_path / "b" / "photo.png", tmp_path / "c" / "photo.png"]
    assert assign_outputs(sources, out) == [out / "photo (b).xlsx", out / "photo (c).xlsx"]


def test_a_relative_source_gets_the_name_of_its_folder(tmp_path, monkeypatch):
    folder = tmp_path / "Photos"
    folder.mkdir()
    monkeypatch.chdir(folder)
    out = tmp_path / "out"
    sources = [Path("photo.png"), Path("sub", "photo.png")]
    assert assign_outputs(sources, out) == [out / "photo (Photos).xlsx", out / "photo (sub).xlsx"]


def test_rule_b_only_the_new_source_gets_a_tag(tmp_path):
    out = tmp_path / "out"
    first = assign_outputs([tmp_path / "b" / "photo.png"], out)
    assert first == [out / "photo.xlsx"]
    second = assign_outputs([tmp_path / "c" / "photo.png"], out, {path_key(first[0])})
    assert second == [out / "photo (c).xlsx"]


def test_rule_c_after_clear_list_the_file_alone_gets_the_plain_name(tmp_path):
    out = tmp_path / "out"
    sources = [tmp_path / "b" / "photo.png", tmp_path / "c" / "photo.png"]
    assign_outputs(sources, out)
    assert assign_outputs(sources[1:], out, taken=()) == [out / "photo.xlsx"]


def test_rule_d_two_types_in_one_folder_give_two_outputs(tmp_path):
    sources = [tmp_path / "photo.png", tmp_path / "photo.jpg"]
    assert assign_outputs(sources) == [tmp_path / "photo (png).xlsx", tmp_path / "photo (jpg).xlsx"]


def test_rule_e_the_same_tag_twice_numbers_in_the_order_of_the_paths(tmp_path):
    out = tmp_path / "out"
    a = tmp_path / "2024" / "Beach" / "photo.png"
    b = tmp_path / "2023" / "Beach" / "photo.png"
    expected = {b: out / "photo (Beach).xlsx", a: out / "photo (Beach 2).xlsx"}
    for sources in ([a, b], [b, a]):
        assert assign_outputs(sources, out) == [expected[src] for src in sources]


def test_both_the_folder_and_the_type_differ(tmp_path):
    out = tmp_path / "out"
    sources = [tmp_path / "b" / "photo.png", tmp_path / "c" / "photo.jpg"]
    assert assign_outputs(sources, out) == [
        out / "photo (b, png).xlsx",
        out / "photo (c, jpg).xlsx",
    ]


def test_the_result_does_not_depend_on_the_drop_order(tmp_path):
    out = tmp_path / "out"
    sources = [
        tmp_path / folder / f"photo.{suffix}"
        for folder in ("b", "c", "Beach", "x/Beach")
        for suffix in ("png", "jpg")
    ] + [tmp_path / "photo (b, png).png"]
    forward = dict(zip(sources, assign_outputs(sources, out), strict=True))
    backward = dict(zip(sources[::-1], assign_outputs(sources[::-1], out), strict=True))
    assert forward == backward
    assert len({path_key(dst) for dst in forward.values()}) == len(sources)


def test_a_tag_does_not_take_the_plain_name_of_another_source(tmp_path):
    sources = [tmp_path / "photo.png", tmp_path / "photo.jpg", tmp_path / "photo (png).gif"]
    assert assign_outputs(sources) == [
        tmp_path / "photo (png 2).xlsx",
        tmp_path / "photo (jpg).xlsx",
        tmp_path / "photo (png).xlsx",
    ]


def test_a_taken_tag_gets_a_number(tmp_path):
    out = tmp_path / "out"
    taken = {path_key(out / "photo.xlsx"), path_key(out / "photo (b).xlsx")}
    assert assign_outputs([tmp_path / "b" / "photo.png"], out, taken) == [out / "photo (b 2).xlsx"]


def test_a_taken_output_next_to_the_image_gets_the_type(tmp_path):
    taken = {path_key(tmp_path / "photo.xlsx")}
    assert assign_outputs([tmp_path / "photo.png"], None, taken) == [tmp_path / "photo (png).xlsx"]


def test_a_taken_output_in_the_folder_of_the_image_gets_the_type(tmp_path):
    taken = {path_key(tmp_path / "photo.xlsx")}
    result = assign_outputs([tmp_path / "photo.png"], tmp_path, taken)
    assert result == [tmp_path / "photo (png).xlsx"]


def test_an_output_is_never_a_source(tmp_path):
    # .xlsx is not an image suffix, but the rule holds for each source in the call.
    sources = [tmp_path / "sheet.xlsx"]
    assert assign_outputs(sources) == [tmp_path / "sheet (xlsx).xlsx"]


def test_the_same_source_twice_gets_two_outputs(tmp_path):
    src = tmp_path / "photo.png"
    assert assign_outputs([src, src]) == [
        tmp_path / "photo (png).xlsx",
        tmp_path / "photo (png 2).xlsx",
    ]


@windows
def test_outputs_collide_without_regard_to_case_on_windows():
    out = Path(r"E:\out")
    sources = [Path(r"D:\b\Photo.png"), Path(r"D:\c\photo.PNG")]
    assert assign_outputs(sources, out) == [
        Path(r"E:\out\Photo (b).xlsx"),
        Path(r"E:\out\photo (c).xlsx"),
    ]


@pytest.mark.parametrize("posix", [False, True])
def test_outputs_that_differ_only_in_case_get_a_tag(tmp_path, monkeypatch, posix):
    # A disk can ignore the case on all systems, for example APFS, exFAT or a share.
    if posix:
        monkeypatch.setattr("os.path.normcase", posixpath.normcase)
    one = [tmp_path / "Photo.png", tmp_path / "photo.jpg"]
    assert names(assign_outputs(one)) == ["Photo (png).xlsx", "photo (jpg).xlsx"]
    out = tmp_path / "out"
    two = [tmp_path / "a" / "Photo.PNG", tmp_path / "b" / "photo.png"]
    assert names(assign_outputs(two, out)) == ["Photo (a).xlsx", "photo (b).xlsx"]
    taken = [path_key(out / "PHOTO.xlsx")]
    tag = tmp_path.name
    assert names(assign_outputs([tmp_path / "photo.png"], out, taken)) == [f"photo ({tag}).xlsx"]
    if posix:
        # The folder tags differ only in case: a number keeps the outputs apart.
        three = [tmp_path / "A" / "x.png", tmp_path / "a" / "x.png"]
        assert names(assign_outputs(three, out)) == ["x (A).xlsx", "x (a 2).xlsx"]


def test_sources_that_differ_only_in_case_stay_two_images_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.normcase", posixpath.normcase)
    assert path_key(tmp_path / "Photo.png") != path_key(tmp_path / "photo.png")


@windows
def test_outputs_from_mixed_drives_and_shares():
    out = Path(r"E:\out")
    sources = [Path(r"C:\photo.png"), Path(r"D:\Đà Lạt\photo.png"), Path(r"\\nas\scans\photo.png")]
    assert assign_outputs(sources, out) == [
        Path(r"E:\out\photo (C).xlsx"),
        Path(r"E:\out\photo (Đà Lạt).xlsx"),
        Path(r"E:\out\photo (scans).xlsx"),
    ]
    # Next to each image, the three outputs do not collide.
    assert assign_outputs(sources) == [
        Path(r"C:\photo.xlsx"),
        Path(r"D:\Đà Lạt\photo.xlsx"),
        Path(r"\\nas\scans\photo.xlsx"),
    ]


def test_500_sources_with_collisions_are_fast_and_unique(tmp_path):
    out = tmp_path / "out"
    sources = [tmp_path / f"f{i % 50}" / f"IMG_{i // 50}.png" for i in range(500)]
    start = time.perf_counter()
    result = assign_outputs(sources, out)
    assert time.perf_counter() - start < 1
    assert len({path_key(dst) for dst in result}) == 500
    assert result[0] == out / "IMG_0 (f0).xlsx"
    assert all(dst.parent == out for dst in result)
    next_to = assign_outputs(sources)
    assert next_to == [src.with_suffix(".xlsx") for src in sources]


# --- path_problem --------------------------------------------------------------------


@windows
def test_path_problem_of_a_long_output_path():
    dst = Path("C:\\" + "x" * 250 + "\\" + "a" * 15 + ".xlsx")
    problem = path_problem(dst)
    assert problem == (
        f"The output path has {len(str(dst))} characters. Windows allows 259. "
        'Set "Save to" to a short folder, then click Retry failed.'
    )


@windows
def test_path_problem_counts_the_temporary_file():
    folder = "C:\\" + "x" * 230
    assert path_problem(Path(folder, "a.xlsx")) is not None
    assert "temporary file" in path_problem(Path(folder, "a.xlsx"))
    assert path_problem(Path("C:\\" + "x" * 200, "a.xlsx")) is None


@windows
def test_path_problem_at_the_limit():
    ok = Path("C:\\" + "x" * 200 + "\\" + "a" * (259 - 204 - 5) + ".xlsx")
    assert len(str(ok)) == 259
    assert path_problem(ok) is None
    assert path_problem(ok.with_name("b" + ok.name)) is not None


@windows
def test_path_problem_of_a_short_unc_path_is_none():
    assert path_problem(Path(r"\\nas\scans\1998\Đà Lạt\photo.xlsx")) is None


def test_path_problem_only_on_windows():
    dst = Path("/" + "x" * 300 + "/a.xlsx")
    assert path_problem(dst, platform="linux") is None
    assert path_problem(dst, platform="win32") is not None


def test_path_problem_of_a_long_file_name_on_all_systems():
    dst = Path("/out", "a" * 251 + ".xlsx")
    problem = path_problem(dst, platform="linux")
    assert problem is not None
    assert problem.startswith("The output file name has 256 bytes. The limit is 255.")
    problem = path_problem(dst, platform="win32")
    assert problem.startswith("The output file name has 256 characters. The limit is 255.")


def test_path_problem_counts_bytes_off_windows():
    # 95 letters with a mark and the suffix: 100 characters, but 290 bytes in UTF-8.
    dst = Path("/out", "ả" * 95 + ".xlsx")
    assert path_problem(dst, platform="win32") is None
    problem = path_problem(dst, platform="linux")
    assert problem.startswith("The output file name has 290 bytes. The limit is 255.")


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX path is not absolute on Windows")
def test_path_problem_of_a_long_path_off_windows():
    folder = "/" + "/".join(["x" * 200] * 20)  # 4,020 bytes, each name below the limit
    assert path_problem(Path(folder, "a.xlsx"), platform="linux") is None
    problem = path_problem(Path(folder, "y" * 70, "a.xlsx"), platform="linux")
    assert problem.startswith("The output path has 4,098 bytes. The system allows 4,095.")
    assert "Windows" not in problem
    near = Path(folder, "y" * 50, "a.xlsx")  # the path fits, the temporary file does not
    assert "temporary file next to the output needs" in path_problem(near, platform="linux")


# --- Folders -------------------------------------------------------------------------


def test_common_folder(tmp_path):
    assert common_folder([]) is None
    assert common_folder([tmp_path / "a" / "b", tmp_path / "a" / "c"]) == tmp_path / "a"
    assert common_folder([tmp_path / "a"]) == tmp_path / "a"


@windows
def test_common_folder_of_other_drives_and_shares_is_none():
    assert common_folder([Path(r"C:\a"), Path(r"D:\b")]) is None
    assert common_folder([Path(r"D:\a"), Path(r"\\nas\scans\b")]) is None
    assert common_folder([Path(r"\\nas\a\x"), Path(r"\\nas\b\y")]) is None
    assert common_folder([Path(r"\\nas\scans\x"), Path(r"\\NAS\Scans\y")]) == Path(r"\\nas\scans")
    assert common_folder([Path(r"D:\Photos\A"), Path(r"d:\photos\b")]) == Path(r"D:\Photos")


@windows
def test_folder_labels_relative_to_the_drive():
    folders = [Path(r"D:\Photos\Family"), Path(r"D:\Photos\2024\Da Lat"), Path(r"D:\Scans")]
    heading, labels = folder_labels(folders)
    assert heading == r"Folder (in D:\)"
    assert labels == {
        path_key(r"D:\Photos\Family"): r"Photos\Family",
        path_key(r"D:\Photos\2024\Da Lat"): r"Photos\2024\Da Lat",
        path_key(r"D:\Scans"): "Scans",
    }


@windows
def test_folder_labels_start_with_the_common_folder():
    folders = [Path(r"D:\Photos\2024\Đà Lạt"), Path(r"D:\Photos\2023\Hội An"), Path(r"D:\Photos")]
    heading, labels = folder_labels(folders)
    assert heading == r"Folder (in D:\)"
    assert labels[path_key(r"D:\Photos")] == "Photos"
    assert labels[path_key(r"D:\Photos\2024\Đà Lạt")] == r"Photos\2024\Đà Lạt"
    heading, labels = folder_labels([Path(r"D:\Photos\2024\A"), Path(r"D:\Photos\2024\B")])
    assert heading == r"Folder (in D:\Photos)"
    assert labels[path_key(r"D:\Photos\2024\A")] == r"2024\A"


@windows
def test_folder_labels_of_one_folder_and_of_the_drive():
    heading, labels = folder_labels([Path(r"D:\Photos\Trip"), Path(r"d:\photos\trip")])
    assert heading == r"Folder (in D:\Photos)"
    assert labels == {path_key(r"D:\Photos\Trip"): "Trip"}
    heading, labels = folder_labels([Path("D:\\"), Path(r"D:\Scans")])
    assert heading == r"Folder (in D:\)"
    assert labels == {path_key("D:\\"): "D:\\", path_key(r"D:\Scans"): "Scans"}


@windows
def test_folder_labels_on_a_share():
    heading, labels = folder_labels([Path(r"\\nas\scans\1998"), Path(r"\\nas\scans\2001\Huế")])
    assert heading == r"Folder (in \\nas\scans\)"
    assert labels[path_key(r"\\nas\scans\2001\Huế")] == r"2001\Huế"


@windows
def test_folder_labels_without_a_common_folder_are_full_paths():
    folders = [Path(r"D:\Photos"), Path(r"\\nas\scans\1998")]
    heading, labels = folder_labels(folders)
    assert heading == "Folder"
    assert labels == {path_key(f): str(f) for f in folders}


@windows
def test_folder_labels_give_the_full_folder_in_a_long_heading():
    # The window shortens the heading to the width of the column, in pixels.
    folder = Path("D:\\" + "\\".join(f"level {i}" for i in range(12)))
    heading, labels = folder_labels([folder / "a", folder / "b"])
    assert heading == f"Folder (in {folder.parent})"
    assert labels[path_key(folder / "a")] == r"level 11\a"


def test_folder_labels_of_500_folders(tmp_path):
    folders = [tmp_path / f"f{i}" / "sub" for i in range(500)]
    start = time.perf_counter()
    _heading, labels = folder_labels(folders)
    assert time.perf_counter() - start < 1
    assert len(labels) == 500
    assert labels[path_key(folders[7])] == str(Path(tmp_path.name, "f7", "sub"))


def test_middle_ellipsis_keeps_short_text():
    assert middle_ellipsis(r"D:\Photos", 20) == r"D:\Photos"


def test_middle_ellipsis_keeps_the_drive_and_the_last_folders():
    text = r"D:\Photos\2024\Trip to Da Lat\Day 2"
    assert middle_ellipsis(text, 25) == "D:\\\u2026\\Trip to Da Lat\\Day 2"
    assert middle_ellipsis(text, 12) == "D:\\\u2026\\Day 2"


def test_middle_ellipsis_keeps_the_share():
    text = r"\\nas\photos\Scans\1998\Family"
    assert middle_ellipsis(text, 26) == "\\\\nas\\photos\\\u2026\\1998\\Family"


def test_middle_ellipsis_of_posix_paths_and_vietnamese_names():
    assert middle_ellipsis("/home/an/Ảnh/Đà Lạt/Chợ đêm", 18) == "/\u2026/Đà Lạt/Chợ đêm"


def test_middle_ellipsis_of_one_long_name():
    text = "D:\\" + "x" * 50
    assert middle_ellipsis(text, 10) == "\u2026" + "x" * 9


@pytest.mark.parametrize("limit", range(0, 40))
def test_middle_ellipsis_is_never_longer_than_the_limit(limit):
    for text in (r"D:\Photos\2024\Trip to Da Lat\Day 2", r"\\nas\share\a\b", "plain name.txt"):
        assert len(middle_ellipsis(text, limit)) <= limit


def test_group_by_folder_keeps_the_order_of_the_first_path(tmp_path):
    a1, b1, a2 = tmp_path / "a" / "1.xlsx", tmp_path / "b" / "2.xlsx", tmp_path / "a" / "3.xlsx"
    assert group_by_folder([a1, b1, a2]) == [(tmp_path / "a", [a1, a2]), (tmp_path / "b", [b1])]
    assert group_by_folder([]) == []


@windows
def test_group_by_folder_ignores_the_case_on_windows():
    groups = group_by_folder([Path(r"D:\A\1.xlsx"), Path(r"d:\a\2.xlsx")])
    assert len(groups) == 1
    assert groups[0][0] == Path(r"D:\A")


# --- Errors --------------------------------------------------------------------------


def test_failure_kind(tmp_path):
    dst = tmp_path / "out" / "a.xlsx"
    temp = dst.with_name(".pixheet-0123456789abcdef.tmp")
    assert failure_kind(UnidentifiedImageError("cannot identify"), dst) == "image"
    assert failure_kind(PermissionError(errno.EACCES, "denied", str(dst)), dst) == "write"
    assert failure_kind(PermissionError(errno.EACCES, "denied", str(temp)), dst) == "write"
    assert failure_kind(PermissionError(errno.EACCES, "denied"), dst) == "write"
    assert failure_kind(OSError(errno.EROFS, "read-only", str(dst)), dst) == "write"
    other = tmp_path / "in.png"
    assert failure_kind(PermissionError(errno.EACCES, "denied", str(other)), dst) == "other"
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone", str(dst)), dst) == "missing"
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone", str(other)), dst) == "other"
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone"), dst) == "missing"
    dst.parent.mkdir()
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone"), dst) == "other"
    assert failure_kind(ValueError("bad"), dst) == "other"
    assert failure_kind(PermissionError(errno.EACCES, "denied", 3), dst) == "other"
    assert failure_kind(OSError(errno.ENOSPC, "full", str(dst)), dst) == "other"


def test_failure_kind_of_a_source_next_to_the_output(tmp_path):
    dst = tmp_path / "a.xlsx"
    src = tmp_path / "a.png"
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone", str(src)), dst) == "other"
    assert failure_kind(PermissionError(errno.EACCES, "denied", str(src)), dst) == "other"
    assert failure_kind(FileNotFoundError(errno.ENOENT, "gone", str(tmp_path)), dst) == "missing"


def test_failure_detail(tmp_path):
    dst = tmp_path / "Acme" / "a.xlsx"
    assert failure_detail("write", "denied", dst, platform="win32") == (
        f"Windows does not let the app write in {dst.parent}. "
        'Set "Save to" to a folder of your own, then click Retry failed.'
    )
    assert failure_detail("write", "denied", dst, platform="linux").startswith(
        "The system does not let the app write in "
    )
    assert failure_detail("image", "the app cannot read it", dst) == "the app cannot read it"
    assert failure_detail("other", "[Errno 28] No space", dst) == "[Errno 28] No space"
    assert failure_detail("in use", "denied", dst) == (
        "Windows cannot replace a.xlsx, because another app has the file open, for example "
        "Excel. Close the file in that app, then click Retry failed."
    )
    # The folder of the output went away, for example a share. The text tells what to do.
    assert failure_detail("missing", "No such file or directory", dst) == (
        f"The app cannot find the folder {dst.parent}. Make sure that the folder exists, "
        'or set "Save to" to a different folder. Then click Retry failed.'
    )


def rename_error(dst: Path) -> PermissionError:
    """Return the error of convert() when the rename of the temporary file over ``dst`` fails.

    core._write gives the output as the file name, and keeps the error of the rename as
    the cause. That error has the temporary file and the output.
    """
    temp = dst.with_name(".pixheet-0123456789abcdef.tmp")
    error = PermissionError(errno.EACCES, "Access is denied", str(dst))
    error.__cause__ = PermissionError(errno.EACCES, "Access is denied", str(temp), None, str(dst))
    return error


def test_failure_kind_of_an_output_that_another_app_has_open(tmp_path):
    dst = tmp_path / "a.xlsx"
    dst.write_bytes(b"old")
    assert failure_kind(rename_error(dst), dst, platform="win32") == "in use"
    # On Linux and macOS, a rename over an open file works. Thus the cause is other.
    assert failure_kind(rename_error(dst), dst, platform="linux") == "write"

    # The temporary file: the app cannot write in the folder.
    open_error = PermissionError(errno.EACCES, "Access is denied", str(dst))
    temp = dst.with_name(".pixheet-0123456789abcdef.tmp")
    open_error.__cause__ = PermissionError(errno.EACCES, "Access is denied", str(temp))
    assert failure_kind(open_error, dst, platform="win32") == "write"
    assert failure_kind(PermissionError(errno.EACCES, "denied", str(dst)), dst, "win32") == "write"

    dst.unlink()  # no file to replace, or a folder with the name of the output
    assert failure_kind(rename_error(dst), dst, platform="win32") == "write"
    dst.mkdir()
    assert failure_kind(rename_error(dst), dst, platform="win32") == "write"


@windows
def test_convert_over_an_output_that_another_app_has_open(tmp_path, gradient_png, hold_open):
    dst = tmp_path / "a.xlsx"
    convert(gradient_png, dst)
    close = hold_open(dst)
    try:
        with pytest.raises(PermissionError) as info:
            convert(gradient_png, dst)
    finally:
        close()
    assert failure_kind(info.value, dst) == "in use"
    assert short_reason("in use", str(info.value)) == "file is in use"
    assert not list(tmp_path.glob(".pixheet-*.tmp"))
    convert(gradient_png, dst)  # the other app closed the file


@windows
def test_convert_over_a_read_only_output(tmp_path, gradient_png):
    dst = tmp_path / "a.xlsx"
    convert(gradient_png, dst)
    dst.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PermissionError) as info:
            convert(gradient_png, dst)
        assert failure_kind(info.value, dst) == "write"
    finally:
        dst.chmod(stat.S_IREAD | stat.S_IWRITE)


@windows
def test_failure_kind_on_a_share():
    dst = Path(r"\\nas\scans\Đà Lạt\a.xlsx")
    error = FileNotFoundError(2, "The network path was not found", r"\\NAS\scans\đà lạt\a.xlsx")
    assert failure_kind(error, dst) == "missing"


def test_short_reason():
    assert short_reason("write", "anything") == "cannot write here"
    assert short_reason("missing", "") == "cannot find the folder"
    assert short_reason("image", "") == "not an image"
    assert short_reason("long", "") == "path too long"
    assert short_reason("in use", "Access is denied") == "file is in use"
    assert short_reason("other", "[Errno 28] No space left on device") == "No space left on device"
    assert short_reason("other", "[WinError 112] There is not enough space") == (
        "There is not enough spa\u2026"
    )
    assert short_reason("other", "") == "unknown error"
    assert short_reason("other", "two\nlines") == "two lines"
    for text in ("x" * 100, "word " * 20):
        assert len(short_reason("other", text)) == 24


# --- Jobs, rows and messages ---------------------------------------------------------


def test_job_has_its_own_skip_event(tmp_path):
    a = Job(1, tmp_path / "a.png", tmp_path / "a.xlsx")
    b = Job(2, tmp_path / "b.png", tmp_path / "b.xlsx")
    a.skip.set()
    assert not b.skip.is_set()


def test_job_queue_keeps_the_order_and_removes_jobs(tmp_path):
    jobs = [Job(i, tmp_path / f"{i}.png", tmp_path / f"{i}.xlsx") for i in range(5)]
    queue = JobQueue()
    assert queue.take() is None
    queue.put(jobs[:3])
    queue.put(jobs[3:])
    assert len(queue) == 5
    assert queue.take() is jobs[0]
    queue.remove([2, 4, 99])
    assert len(queue) == 2
    assert queue.take() is jobs[1]
    queue.clear()
    assert len(queue) == 0
    assert queue.take() is None


def test_job_queue_is_safe_for_two_threads(tmp_path):
    queue = JobQueue()
    taken = []

    def take_all():
        while len(taken) < 2_000:
            job = queue.take()
            if job is not None:
                taken.append(job.id)

    worker = threading.Thread(target=take_all)
    worker.start()
    for start in range(0, 2_000, 100):
        queue.put(
            Job(i, tmp_path / "a.png", tmp_path / "a.xlsx") for i in range(start, start + 100)
        )
    worker.join(10)
    assert taken == list(range(2_000))


def row(**kwargs):
    values = {"id": 7, "src": Path("D:/p/IMG_1.jpg"), "dst": Path("D:/p/IMG_1.xlsx")}
    values.update(kwargs)
    return Row(**values)


def test_status_text_of_each_state():
    assert status_text(row()) == "Waiting"
    renamed = row(dst=Path("D:/p/IMG_1 (jpg).xlsx"))
    assert status_text(renamed) == "Waiting \u00b7 as IMG_1 (jpg).xlsx"
    assert status_text(row(dst=Path("E:/out/IMG_1.xlsx"))) == "Waiting"
    assert status_text(row(state=CONVERTING, percent=47)) == "Converting 47%"
    assert status_text(row(state=DONE, size=2_831_155)) == "\u2713 Done \u00b7 2.7 MB"
    failed = row(state=FAILED, kind="write", error="[Errno 13] denied")
    assert status_text(failed) == "\u2715 Failed: cannot write here"
    assert status_text(row(state=SKIPPED)) == "\u2013 Skipped: output exists"
    assert status_text(row(state=STOPPED)) == "\u2013 Stopped"
    assert status_text(row(state="unknown")) == "unknown"


def test_format_size():
    assert format_size(0) == "0 bytes"
    assert format_size(1) == "1 byte"
    assert format_size(1023) == "1,023 bytes"
    assert format_size(1024) == "1.0 KB"
    assert format_size(2 * 1024 * 1024) == "2.0 MB"
    assert format_size(1024 * 1024 - 1) == "1,024.0 KB"
    assert format_size(3 * 1024**3) == "3.0 GB"
    assert format_size(5 * 1024**4) == "5,120.0 GB"


def test_apply_message_runs_a_row_through_its_states():
    waiting = row()
    converting = apply_message(waiting, Started(7))
    assert (converting.state, converting.percent) == (CONVERTING, 0)
    assert waiting.state == WAITING  # the reducer does not change its input
    half = apply_message(converting, Progress(7, 3, 6))
    assert half.percent == 50
    done = apply_message(half, Converted(7, RESULT, size=2048))
    assert (done.state, done.percent, done.size, done.result) == (DONE, 100, 2048, RESULT)


def test_apply_message_failed_and_stopped():
    converting = apply_message(row(), Started(7))
    failed = apply_message(converting, Failed(7, "denied", "write"))
    assert (failed.state, failed.error, failed.kind) == (FAILED, "denied", "write")
    assert failed.failed_output == failed.dst
    # A change of "Save to" gives a new output. The reason still names the old one.
    moved = replace(failed, dst=Path("elsewhere", "a.xlsx"))
    assert moved.failed_output == failed.dst
    again = apply_message(moved, Started(7))
    assert (again.state, again.error, again.kind) == (CONVERTING, "", "")
    assert again.failed_output == again.dst
    stopped = apply_message(apply_message(again, Progress(7, 1, 2)), Stopped(7))
    assert (stopped.state, stopped.percent) == (STOPPED, 0)


def test_apply_message_ignores_other_jobs_and_finished():
    waiting = row()
    for message in (Started(8), Progress(8, 1, 2), Failed(8, "x"), Stopped(8), Finished(False)):
        assert apply_message(waiting, message) is waiting


def test_apply_message_progress_only_for_a_converting_row():
    stopped = row(state=STOPPED)
    assert apply_message(stopped, Progress(7, 1, 2)) is stopped
    converting = row(state=CONVERTING)
    assert apply_message(converting, Progress(7, 0, 0)).percent == 0
    assert apply_message(converting, Progress(7, 9, 4)).percent == 100


def test_tally_counts_each_change():
    tally = Tally()
    for _ in range(3):
        tally.add(None, WAITING)
    tally.add(WAITING, CONVERTING)
    tally.add(CONVERTING, DONE)
    tally.add(WAITING, CONVERTING)
    tally.add(CONVERTING, FAILED)
    tally.add(WAITING, SKIPPED)
    assert tally == Tally(done=1, failed=1, skipped=1)
    assert (tally.finished, tally.total) == (3, 3)
    tally.add(FAILED, WAITING)  # Retry
    tally.add(DONE, None)  # Remove from list
    tally.add(SKIPPED, SKIPPED)
    assert tally == Tally(waiting=1, skipped=1)


def test_tally_refuses_bad_changes():
    tally = Tally()
    with pytest.raises(ValueError, match="no row is done"):
        tally.add(DONE, WAITING)
    with pytest.raises(ValueError, match="unknown state"):
        tally.add(None, "lost")
    assert tally == Tally()


def test_tally_of_500_rows_matches_a_full_count():
    import random

    rng = random.Random(5)
    states = [WAITING] * 500
    tally = Tally(waiting=500)
    for _ in range(5000):
        i = rng.randrange(500)
        new = rng.choice(STATES)
        tally.add(states[i], new)
        states[i] = new
    assert tally == Tally(**{state: states.count(state) for state in STATES})


# --- Texts ---------------------------------------------------------------------------


def test_progress_text():
    tally = Tally(done=23, failed=1, converting=1, waiting=35)
    assert progress_text(25, 60, "IMG_1073.jpg", tally) == (
        "File 25 of 60 \u00b7 23 done \u00b7 1 failed"
    )
    assert progress_text(1, 5, "a.png", Tally(waiting=4, converting=1)) == (
        "File 1 of 5 \u00b7 0 done"
    )
    tally = Tally(done=1_000, skipped=2, stopped=1, waiting=1_200)
    assert progress_text(1_004, 2_203, "x", tally) == (
        "File 1,004 of 2,203 \u00b7 1,000 done \u00b7 2 skipped \u00b7 1 stopped"
    )


def test_progress_text_of_one_file_names_it():
    assert progress_text(1, 1, "IMG_2041.jpg", Tally(converting=1)) == "File 1 of 1: IMG_2041.jpg"
    assert progress_text(1, 1, "", Tally(converting=1)) == "File 1 of 1"


@pytest.mark.parametrize(
    ("tally", "total", "stopped", "text"),
    [
        (Tally(done=1), 1, False, "Converted 1 file."),
        (Tally(done=60), 60, False, "Converted 60 files."),
        (
            Tally(done=57, failed=2, skipped=1), 60, False,
            "Converted 57 of 60 files. 2 failed. 1 skipped.",
        ),
        (Tally(done=0, failed=1), 1, False, "Converted 0 of 1 file. 1 failed."),
        (
            Tally(done=24, stopped=1, waiting=35), 60, True,
            "Stopped. Converted 24 of 60 files. 1 stopped.",
        ),
        (Tally(done=2_000), 2_000, False, "Converted 2,000 files."),
        (Tally(done=3), 3, True, "Stopped. Converted 3 of 3 files."),
    ],
)  # fmt: skip
def test_summary_line(tally, total, stopped, text):
    assert summary_line(tally, total, stopped) == text


@pytest.mark.parametrize(
    ("seconds", "done", "left", "text"),
    [
        (10, 2, 50, ""),
        (0, 3, 5, ""),
        (30, 3, 0, ""),
        (3, 3, 0.5, "about 5 s left"),
        (12, 3, 10, "about 40 s left"),
        (12, 3, 14.5, "about 1 min left"),
        (30, 10, 40, "about 2 min left"),
        (600, 3, 17.5, "about 58 min left"),
        (600, 3, 18, "about 1 h left"),
        (3600, 3, 9, "about 3 h left"),
        (7200, 3, 3_000, "about 2,000 h left"),
    ],
)
def test_eta_text(seconds, done, left, text):
    assert eta_text(seconds, done, left) == text


def test_drop_note():
    assert drop_note(DropResult(images=[Path("a.png")])) == ""
    assert drop_note(DropResult()) == "The drop has no images."
    ignored = [Path(n) for n in ("notes.txt", "IMG_1044.CR2")]
    assert drop_note(DropResult(images=[Path("a.png")], ignored=ignored)) == (
        "Ignored 2 files that are not images: notes.txt, IMG_1044.CR2."
    )
    many = [Path(f"{i}.txt") for i in range(8)]
    assert drop_note(DropResult(ignored=many)) == (
        "The drop has no images. "
        "Ignored 8 files that are not images: 0.txt, 1.txt, 2.txt and 5 more."
    )
    assert drop_note(DropResult(ignored=many[:1])) == (
        "The drop has no images. Ignored 1 file that is not an image: 0.txt."
    )
    assert drop_note(DropResult(duplicates=1)) == "1 image is already in the list."
    assert drop_note(DropResult(images=[Path("a.png")], duplicates=3, missing=1)) == (
        "3 images are already in the list. The app cannot find or read 1 dropped item."
    )


def test_drop_note_with_vietnamese_names():
    note = drop_note(DropResult(images=[Path("a.png")], ignored=[Path("D:/Ảnh/ghi chú.txt")]))
    assert note == "Ignored 1 file that is not an image: ghi chú.txt."


def test_subfolder_note():
    assert subfolder_note(DropResult()) == ""
    one = DropResult(skipped_subfolders={Path("D:/Photos/Beach"): 2})
    assert subfolder_note(one) == "The app did not open 2 subfolders of Beach."
    single = DropResult(skipped_subfolders={Path("D:/Photos/Đà Lạt"): 1})
    assert subfolder_note(single) == "The app did not open 1 subfolder of Đà Lạt."
    two = DropResult(skipped_subfolders={Path("D:/a"): 2, Path("D:/b"): 3})
    assert subfolder_note(two) == "The app did not open 5 subfolders of 2 folders."


def test_replace_question_for_many_files():
    existing = [Path(f"D:/Photos/{folder}/IMG_{i}.xlsx") for folder in "abc" for i in range(2)]
    existing.append(Path("D:/Photos/a/IMG_9.xlsx"))
    question = ReplaceQuestion(existing, other_count=53)
    assert question.heading == "7 output files exist"
    assert question.text == (
        "They are in 3 folders. The app can replace them. "
        "Or it can skip these images and convert the other 53."
    )
    assert (question.replace_label, question.skip_label) == ("Replace all", "Skip these 7")
    assert [len(files) for _folder, files in question.folders] == [3, 2, 2]


def test_replace_question_without_other_images():
    question = ReplaceQuestion([Path("D:/a/1.xlsx"), Path("D:/a/2.xlsx")], other_count=0)
    assert question.text == "They are in one folder. The app can replace them, or stop."
    assert question.skip_label is None


def test_replace_question_for_one_file():
    question = ReplaceQuestion([Path("D:/a/IMG_1042.xlsx")], other_count=1)
    assert question.heading == "IMG_1042.xlsx exists"
    assert question.text == (
        "The app can replace it. Or it can skip this image and convert the other image."
    )
    assert (question.replace_label, question.skip_label) == ("Replace", "Skip this file")
    alone = ReplaceQuestion([Path("D:/a/IMG_1042.xlsx")], other_count=0)
    assert alone.text == "The app can replace it, or stop."
    assert alone.skip_label is None

"""resolve_source, plain_name and Outputs."""

import os
from pathlib import Path

import pytest

from dock_plugin.cancel import Call, Cancelled, Failure
from dock_plugin.files import Outputs, plain_name, resolve_source


@pytest.fixture
def dirs(tmp_path):
    root = tmp_path.resolve()
    d = {k: root / k for k in ("in", "work", "outside", "out")}
    for p in d.values():
        p.mkdir()
    return d


def test_a_file_in_the_in_folder_or_the_work_folder_is_found(dirs):
    (dirs["in"] / "a.txt").write_text("x")
    (dirs["work"] / "b.txt").write_text("x")
    assert resolve_source("a.txt", str(dirs["in"]), str(dirs["work"])) == str(dirs["in"] / "a.txt")
    assert resolve_source(str(dirs["work"] / "b.txt"), str(dirs["in"]), str(dirs["work"])) == str(dirs["work"] / "b.txt")
    assert resolve_source("b.txt", None, str(dirs["work"])) == str(dirs["work"] / "b.txt")


def test_a_symlink_inside_is_followed_one_that_leaves_is_refused(dirs):
    (dirs["in"] / "a.txt").write_text("x")
    (dirs["outside"] / "secret").write_text("x")
    (dirs["in"] / "ok").symlink_to(dirs["in"] / "a.txt")
    (dirs["in"] / "leak").symlink_to(dirs["outside"] / "secret")
    assert resolve_source("ok", str(dirs["in"]), None) == str(dirs["in"] / "a.txt")
    with pytest.raises(Failure, match=r"file must be inside the in folder"):
        resolve_source("leak", str(dirs["in"]), None)


def test_a_name_that_climbs_out_is_refused(dirs):
    (dirs["outside"] / "secret").write_text("x")
    with pytest.raises(Failure, match=r"file must be inside the in folder"):
        resolve_source("../outside/secret", str(dirs["in"]), None)


def test_a_fifo_is_refused_without_waiting_for_a_writer(dirs):
    os.mkfifo(dirs["in"] / "pipe")
    with pytest.raises(Failure, match=r"pipe is not a file in the in folder"):
        resolve_source("pipe", str(dirs["in"]), None)


def test_a_missing_file_a_missing_folder_and_a_nul_are_refused_in_words(dirs):
    with pytest.raises(Failure, match=r"cannot read nope.txt: No such file"):
        resolve_source("nope.txt", str(dirs["in"]), None)
    with pytest.raises(Failure, match=r"^sample needs the in folder"):
        resolve_source("a", None, None, "sample")
    with pytest.raises(Failure, match=r"without NUL"):
        resolve_source("a\0b", str(dirs["in"]), None)


@pytest.mark.parametrize("name", ["a/b", "a\\b", ".", "..", "a\nb", "a\x7f"])
def test_plain_name_refuses_folders_dots_and_control_characters(name):
    with pytest.raises(Failure, match=r"^output must be a plain name without a folder"):
        plain_name(name, "output")


def test_plain_name_limits_the_length_in_bytes_and_returns_a_good_name():
    assert plain_name("talk", "output") == "talk"
    assert plain_name("é" * 100, "output") == "é" * 100
    with pytest.raises(Failure, match=r"^output name is too long"):
        plain_name("é" * 101, "output")
    assert plain_name("é" * 101, "output", max_bytes=300)


def test_check_refuses_an_existing_target_even_a_dangling_link(dirs):
    (dirs["out"] / "a.txt").write_text("x")
    (dirs["out"] / "dangling").symlink_to(dirs["out"] / "nowhere")
    Outputs.check([str(dirs["out"] / "free.txt")])
    with pytest.raises(Failure, match=r"^a.txt already exists"):
        Outputs.check([str(dirs["out"] / "a.txt")])
    with pytest.raises(Failure, match=r"^dangling already exists"):
        Outputs.check([str(dirs["out"] / "dangling")])


def test_publish_writes_every_file_and_leaves_no_part(dirs):
    outputs = Outputs()
    a, b = str(dirs["out"] / "a.txt"), str(dirs["out"] / "b.srt")
    outputs.publish([(a, "héllo"), (b, "two")], Call())
    assert sorted(os.listdir(dirs["out"])) == ["a.txt", "b.srt"]
    assert Path(a).read_text(encoding="utf-8") == "héllo"


def test_a_file_that_appears_meanwhile_is_never_replaced_and_this_runs_files_go(dirs):
    a, b = dirs["out"] / "a.txt", dirs["out"] / "b.txt"
    b.write_text("theirs")
    outputs = Outputs()
    with pytest.raises(Failure, match=r"^b.txt already exists"):
        outputs.publish([(str(a), "mine"), (str(b), "mine")], Call())
    assert sorted(os.listdir(dirs["out"])) == ["b.txt"]
    assert b.read_text() == "theirs"


def test_a_leftover_part_file_is_refused_and_kept(dirs):
    (dirs["out"] / ".a.txt.part").write_text("old")
    with pytest.raises(Failure, match=r"^\.a\.txt\.part already exists"):
        Outputs().publish([(str(dirs["out"] / "a.txt"), "x")], Call())
    assert (dirs["out"] / ".a.txt.part").read_text() == "old"


def test_publish_after_a_stop_writes_nothing(dirs):
    call = Call()
    call.stop()
    with pytest.raises(Cancelled):
        Outputs().publish([(str(dirs["out"] / "a.txt"), "x")], call)
    assert os.listdir(dirs["out"]) == []


def test_the_block_discards_what_was_published_when_a_cancel_comes_after(dirs):
    target = str(dirs["out"] / "a.txt")
    with pytest.raises(Cancelled), Outputs() as outputs:
        outputs.publish([(target, "x")], Call())
        assert os.listdir(dirs["out"]) == ["a.txt"]
        raise Cancelled()
    assert os.listdir(dirs["out"]) == []


def test_the_block_keeps_the_files_when_it_ends_cleanly(dirs):
    with Outputs() as outputs:
        outputs.publish([(str(dirs["out"] / "a.txt"), "x")], Call())
    assert os.listdir(dirs["out"]) == ["a.txt"]


def test_discard_removes_only_what_this_run_made(dirs):
    (dirs["out"] / "theirs.txt").write_text("x")
    outputs = Outputs()
    outputs.publish([(str(dirs["out"] / "mine.txt"), "x")], Call())
    outputs.discard()
    assert os.listdir(dirs["out"]) == ["theirs.txt"]

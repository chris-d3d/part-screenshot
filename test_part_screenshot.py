"""Tests that run without NX: C:\\CAD\\DAPS_Plugin\\python.exe -m pytest -q -p no:cacheprovider"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

import part_screenshot as cli
import tc_part_screenshot_journal as journal

JOURNAL_SOURCE = Path(journal.__file__).read_text(encoding="utf-8")


def write_test_image(path: Path, blank: bool = False) -> None:
    """Write a small image; two colors unless blank."""
    try:
        from PIL import Image
    except ImportError:
        path.write_bytes(b"not really an image")
        return
    image = Image.new("RGB", (4, 2), (255, 255, 255))
    if not blank:
        image.putpixel((1, 1), (40, 40, 40))
    image.save(path)


# --- command line -----------------------------------------------------------------

def test_cli_accepts_part_and_folder_with_defaults():
    args = cli.build_parser().parse_args(["--part", "123456/A", "--folder", r"C:\screenshots"])
    assert args.part == cli.ItemRevision("123456", "A")
    assert args.folder == Path(r"C:\screenshots")
    assert (args.width, args.height, args.image_format, args.verbose) == (1600, 1200, "jpg", False)


def test_cli_accepts_equals_form_and_options():
    args = cli.build_parser().parse_args([
        "--part=R0093916/01", "--folder=out", "--width=800", "--height=600", "--format=png", "-v",
    ])
    assert args.part == cli.ItemRevision("R0093916", "01")
    assert (args.width, args.height, args.image_format, args.verbose) == (800, 600, "png", True)


@pytest.mark.parametrize("missing", [["--part", "1/A"], ["--folder", "out"]])
def test_cli_requires_part_and_folder(missing, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.build_parser().parse_args(missing)
    assert exit_info.value.code == 2


def test_parse_part_strips_whitespace():
    assert cli.parse_part("  R0093916 / 01 ") == cli.ItemRevision("R0093916", "01")


@pytest.mark.parametrize("value", [
    "123456", "123456/", "/A", "1/A/B", "", "12 34/A", "123456/A B", "1,2/A", "1/A=B",
])
def test_parse_part_rejects_anything_but_item_slash_revision(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_part(value)


@pytest.mark.parametrize("value", ["63", "8193", "12.5", "big"])
def test_image_size_rejects_out_of_range_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.image_size(value)


def test_image_file_name_is_file_system_safe():
    assert cli.image_file_name(cli.ItemRevision("R0093916", "01"), "jpg") == "R0093916_01.jpg"
    assert cli.image_file_name(cli.ItemRevision("X:1", "A*"), "png") == "X_1_A_.png"


# --- local setup ------------------------------------------------------------------

def test_prepare_output_folder_creates_missing_folders(tmp_path):
    folder = cli.prepare_output_folder(tmp_path / "a" / "b")
    assert folder.is_dir() and folder.is_absolute()


def test_prepare_output_folder_reports_a_file_in_the_way(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    with pytest.raises(cli.UsageError, match="Cannot create output folder"):
        cli.prepare_output_folder(blocker / "sub")


def test_teamcenter_client_settings(tmp_path):
    present = tmp_path / "tccs"
    present.mkdir()
    absent = str(tmp_path / "absent")
    installs = ((absent, "http://a"), (str(present), "http://b"))
    assert cli.teamcenter_client_settings({}, installs) == (str(present), "http://b")
    assert cli.teamcenter_client_settings({"FMS_HOME": r"X:\tccs"}, installs) == (r"X:\tccs", "")
    assert cli.teamcenter_client_settings({}, ((absent, "http://a"), ("Q:\\none", ""))) == (
        "Q:\\none", ""
    )


def test_build_nx_environment_enables_headless_graphics_and_disables_autolock(tmp_path):
    python_home, nx_dir = tmp_path / "python", tmp_path / "nx"
    request_file = tmp_path / "request.json"
    environ = {"PATH": r"C:\Windows", "FMS_HOME": r"X:\tc\tccs", "NX_SSO_URL": "http://sso"}
    environment = cli.build_nx_environment(environ, nx_dir, python_home, request_file)
    assert environment["NX_ENABLE_HEADLESS_GRAPHICS"] == "1"
    assert environment["UGII_UGMGR_NOAUTOLOCK"] == "1"
    assert environment[cli.REQUEST_FILE_ENV] == str(request_file)
    assert environment["UGII_PYTHON_HOME"] == str(python_home)
    assert environment["UGII_PYTHONPATH"].split(";")[0] == str(python_home)
    assert environment["UGII_PYTHON_DLL"] == cli.python_dll_name()
    assert environment["PATH"] == f"{nx_dir / 'NXBIN'};C:\\Windows"
    assert (environment["FMS_HOME"], environment["NX_SSO_URL"]) == (r"X:\tc\tccs", "http://sso")
    assert environment["TC_ROOT"] == environment["IMAN_ROOT"] == r"X:\tc"


def test_teamcenter_environment_matches_daps_utils(tmp_path):
    teamcenter_nx = pytest.importorskip("daps_utils.teamcenter_nx")
    expected = teamcenter_nx.build_teamcenter_environment()
    actual = cli.build_nx_environment(os.environ, tmp_path, tmp_path, tmp_path / "request.json")
    keys = ("NX_SSO_APP_ID", "NX_SSO_URL", "FMS_HOME", "TC_ROOT", "IMAN_ROOT")
    assert {key: actual[key] for key in keys} == {key: expected[key] for key in keys}


def test_write_request_matches_what_the_journal_reads(tmp_path):
    request_file, image_path = cli.write_request(
        tmp_path, cli.ItemRevision("R0093916", "01"), "png", 800, 600
    )
    request = journal.load_request(request_file)
    assert request == {
        "item_id": "R0093916", "revision": "01", "image_path": str(image_path),
        "image_format": "png", "width": 800, "height": 600,
    }
    assert image_path == tmp_path / "R0093916_01.png"
    assert request["image_format"] in journal.IMAGE_FORMATS


def test_protocol_constants_match_the_journal():
    assert cli.REQUEST_FILE_ENV == journal.REQUEST_FILE_ENV
    assert cli.RESULT_PREFIX == journal.RESULT_PREFIX
    assert cli.PROGRESS_PREFIX == journal.PROGRESS_PREFIX
    assert set(cli.IMAGE_FORMATS) == set(journal.IMAGE_FORMATS)
    assert cli.JOURNAL_PATH == Path(journal.__file__).resolve()


# --- journal output parsing -------------------------------------------------------

def test_parse_result_line_ignores_other_output():
    assert cli.parse_result_line("NX session started") is None
    assert cli.parse_result_line(cli.RESULT_PREFIX + "{not json") is None
    assert cli.parse_result_line(cli.RESULT_PREFIX + "[1, 2]") is None
    assert cli.parse_result_line(cli.RESULT_PREFIX + '{"ok": true}') is None
    assert cli.parse_result_line(cli.RESULT_PREFIX + '{"ok": false, "error": "x"}') == {
        "ok": False, "error": "x",
    }


def test_session_monitor_shows_progress_and_keeps_result_and_tail(capsys):
    monitor = cli.SessionMonitor(verbose=False)
    monitor.handle_line(cli.PROGRESS_PREFIX + "Opening 1/A")
    monitor.handle_line("noise")
    monitor.handle_line(cli.RESULT_PREFIX + '{"ok": false, "error": "boom"}')
    assert capsys.readouterr().out == "Opening 1/A\n"
    assert monitor.result == {"ok": False, "error": "boom"}
    assert list(monitor.output_tail) == ["noise"]


# --- journal helpers --------------------------------------------------------------

def test_search_specs_match_daps_utils():
    teamcenter_nx = pytest.importorskip("daps_utils.teamcenter_nx")
    assert journal.build_search_specs("R0093916", "01") == teamcenter_nx.build_search_specs(
        "R0093916", "01"
    )


def test_types_to_hide_keeps_bodies_and_components_visible():
    types_seen = [
        "SHOW_HIDE_TYPE_ALL", "SHOW_HIDE_TYPE_GEOMETRY", "SHOW_HIDE_TYPE_BODIES",
        "SHOW_HIDE_TYPE_SOLID_BODIES", "SHOW_HIDE_TYPE_SHEET_BODIES",
        "SHOW_HIDE_TYPE_FACETED_BODIES", "SHOW_HIDE_TYPE_COMPONENTS",
        "SHOW_HIDE_TYPE_2D_COMPONENTS", "SHOW_HIDE_TYPE_SKETCHES", "SHOW_HIDE_TYPE_CURVES",
        "SHOW_HIDE_TYPE_CSYS", "SHOW_HIDE_TYPE_DATUMS", "SHOW_HIDE_TYPE_POINTS",
        "SHOW_HIDE_TYPE_PMI", "SHOW_HIDE_TYPE_AO_ALL", "SHOW_HIDE_TYPE_DRAWING_HOLE_TABLES ",
    ]
    assert journal.types_to_hide(types_seen) == [
        "SHOW_HIDE_TYPE_SKETCHES", "SHOW_HIDE_TYPE_CURVES", "SHOW_HIDE_TYPE_CSYS",
        "SHOW_HIDE_TYPE_DATUMS", "SHOW_HIDE_TYPE_POINTS", "SHOW_HIDE_TYPE_PMI",
        "SHOW_HIDE_TYPE_AO_ALL", "SHOW_HIDE_TYPE_DRAWING_HOLE_TABLES ",
    ]


def test_summarize_load_notes_counts_descriptions():
    notes = [("a/01", "Read-only part is modified."), ("b/01", "Read-only part is modified."),
             ("c/01", "File not found")]
    assert journal.summarize_load_notes(notes) == {
        "Read-only part is modified.": 2, "File not found": 1,
    }


class FakeUgmgr:
    def __init__(self, stays_on=False, error=None):
        self.calls, self.stays_on, self.error = [], stays_on, error

    def SetAutolockStatus(self, value):
        if self.error:
            raise self.error
        self.calls.append(value)

    def AskAutolockStatus(self):
        return self.stays_on


def test_disable_automatic_checkout_switches_autolock_off():
    ugmgr = FakeUgmgr()
    journal.disable_automatic_checkout(types.SimpleNamespace(Ugmgr=ugmgr))
    assert ugmgr.calls == [False]


@pytest.mark.parametrize("ugmgr", [
    FakeUgmgr(stays_on=True),
    FakeUgmgr(stays_on=None),  # an unexpected answer is not proof that it is off
    FakeUgmgr(error=RuntimeError("x")),
])
def test_disable_automatic_checkout_refuses_when_autolock_stays_on(ugmgr):
    with pytest.raises(journal.SafetyError, match="no part was opened"):
        journal.disable_automatic_checkout(types.SimpleNamespace(Ugmgr=ugmgr))


class FakeStatus:
    def __init__(self, notes=()):
        self.notes, self.disposed = list(notes), False
        self.NumberUnloadedParts = len(self.notes)

    def GetPartName(self, index):
        return self.notes[index][0]

    def GetStatusDescription(self, index):
        return self.notes[index][1]

    def Dispose(self):
        self.disposed = True


class FakeParts:
    """Stands in for session.Parts: rejects the first references, then opens."""

    def __init__(self, rejections, opened_part, status):
        self.rejections, self.opened_part, self.status = rejections, opened_part, status
        self.attempts, self.Display = [], None

    def OpenActiveDisplay(self, reference, option):
        self.attempts.append(reference)
        if len(self.attempts) <= self.rejections:
            raise RuntimeError("Input was not encoded part file name.")
        self.Display = self.opened_part
        return None, self.status  # NX returns None for the part in Python


@pytest.fixture
def fake_nxopen(monkeypatch):
    module = types.SimpleNamespace(DisplayPartOption=types.SimpleNamespace(AllowAdditional="add"))
    monkeypatch.setitem(sys.modules, "NXOpen", module)


def fake_uf_session(encoded="%UGMGR=V3.2 PN=R1 PRN=01"):
    return types.SimpleNamespace(Ugmgr=types.SimpleNamespace(
        EncodePartFilename=lambda item_id, revision, part_type, name: encoded
    ))


def test_open_teamcenter_part_uses_the_display_part_and_reports_the_reference(fake_nxopen):
    status = FakeStatus([("R1/01", "Read-only part is modified.")])
    parts = FakeParts(rejections=1, opened_part="PART", status=status)
    part, reference, notes = journal.open_teamcenter_part(
        types.SimpleNamespace(Parts=parts), fake_uf_session(), "R1", "01"
    )
    assert part == "PART"
    assert reference == journal.build_search_specs("R1", "01")[0]
    assert parts.attempts[0] == "%UGMGR=V3.2 PN=R1 PRN=01"
    assert notes == [("R1/01", "Read-only part is modified.")] and status.disposed


def test_open_teamcenter_part_stops_when_nx_loads_but_keeps_nothing_open(fake_nxopen):
    status = FakeStatus([("R1/01", "Update undo happened.")])
    parts = FakeParts(rejections=1, opened_part=None, status=status)
    with pytest.raises(journal.TeamcenterOpenError, match="Update undo happened"):
        journal.open_teamcenter_part(types.SimpleNamespace(Parts=parts), fake_uf_session(), "R1", "01")
    assert len(parts.attempts) == 2  # did not retry the remaining references


def test_open_teamcenter_part_reports_unique_errors_when_nothing_opens(fake_nxopen):
    parts = FakeParts(rejections=99, opened_part="PART", status=FakeStatus())
    with pytest.raises(journal.TeamcenterOpenError) as error:
        journal.open_teamcenter_part(types.SimpleNamespace(Parts=parts), fake_uf_session(), "R1", "01")
    assert str(error.value) == ("Not found in Teamcenter or access denied. NX reported: "
                                "Input was not encoded part file name.")
    assert len(parts.attempts) == 1 + len(journal.build_search_specs("R1", "01"))


class FakeBasePart:
    def __init__(self, fully_loaded=True):
        self.IsFullyLoaded = fully_loaded

    def LoadThisPartFully(self):
        self.IsFullyLoaded = True
        return FakeStatus()


class FakeComponent:
    def __init__(self, tag, display_name, prototype=None, children=(), parent=None):
        self.Tag, self.DisplayName, self.Prototype = tag, display_name, prototype
        self.children, self.Parent, self.IsSuppressed = list(children), parent, False
        self.Name = f"instance-{tag}"

    def GetChildren(self):
        return list(self.children)


class FakeAssembly:
    """Opens the components listed in ``opens``; reports CouldNotOpen for the rest."""

    def __init__(self, root, opens=()):
        self.RootComponent, self.opens, self.calls = root, set(opens), []

    def OpenComponents(self, option, components):
        self.calls.append([component.Tag for component in components])
        statuses = []
        for component in components:
            if component.Tag in self.opens:
                component.Prototype, component.DisplayName = FakeBasePart(), f"P{component.Tag}"
                statuses.append(0)  # SuccessfullyOpened
            else:
                statuses.append(2)  # CouldNotOpen
        return FakeStatus([("@DB/X0084684/02", "File not found")]), statuses


@pytest.fixture
def fake_assemblies(monkeypatch):
    option = types.SimpleNamespace(ComponentOnly="component only")
    assemblies = types.SimpleNamespace(
        ComponentAssembly=types.SimpleNamespace(OpenOption=option))
    module = types.SimpleNamespace(BasePart=FakeBasePart, Assemblies=assemblies)
    monkeypatch.setitem(sys.modules, "NXOpen", module)
    monkeypatch.setitem(sys.modules, "NXOpen.Assemblies", assemblies)


def test_fully_load_opens_unloaded_components_and_names_what_still_fails(fake_assemblies):
    root = FakeComponent(0, "R0093916/01", FakeBasePart())
    loaded = FakeComponent(1, "A/01", FakeBasePart(fully_loaded=False), parent=root)
    retried = FakeComponent(2, "", parent=root)
    missing = FakeComponent(3, "", parent=root)
    root.children = [loaded, retried, missing]
    assembly = FakeAssembly(root, opens={2})
    part = types.SimpleNamespace(IsFullyLoaded=True, ComponentAssembly=assembly)
    uf_session = types.SimpleNamespace(Assem=types.SimpleNamespace(
        AskComponentData=lambda tag: ("@DB/X0084684/02", "MODEL", "inst", None, None, None)))
    count, not_loaded, notes = journal.fully_load(part, uf_session)
    assert count == 3 and loaded.Prototype.IsFullyLoaded
    assert assembly.calls == [[2, 3]]  # each unloaded component is retried once
    assert not_loaded == [
        "@DB/X0084684/02 (in R0093916/01): Open Component: CouldNotOpen"]
    assert ("@DB/X0084684/02", "File not found") in notes


class FakeOpenParts:
    """Stands in for session.Parts.Open: fails for item IDs in ``refuse``."""

    def __init__(self, assembly, refuse=()):
        self.assembly, self.refuse, self.opened = assembly, set(refuse), []

    def Open(self, reference):
        if any(item_id in reference for item_id in self.refuse):
            raise RuntimeError("No read access to the item revision")
        self.opened.append(reference)
        self.assembly.opens.add(2)  # the component binds to the part now in session
        return "PART", FakeStatus()


def test_fully_load_opens_the_part_directly_and_retries_open_component(fake_assemblies):
    root = FakeComponent(0, "IA0084674/02", FakeBasePart())
    bound = FakeComponent(2, "X0084684/02", parent=root)
    refused = FakeComponent(3, "X0084690/02", parent=root)
    root.children = [bound, refused]
    assembly = FakeAssembly(root)
    parts = FakeOpenParts(assembly, refuse={"X0084690"})
    part = types.SimpleNamespace(IsFullyLoaded=True, ComponentAssembly=assembly)
    session = types.SimpleNamespace(Parts=parts)
    uf_session = types.SimpleNamespace(Ugmgr=types.SimpleNamespace(
        EncodePartFilename=lambda item_id, revision, part_type, name: f"PN={item_id} PRN={revision}"))
    count, not_loaded, _ = journal.fully_load(part, uf_session, session)
    assert count == 2 and isinstance(bound.Prototype, FakeBasePart)
    assert assembly.calls == [[2, 3], [2]]
    assert not_loaded == [
        "X0084690/02 (in IA0084674/02): Open Component: CouldNotOpen; opening it directly "
        "failed: No read access to the item revision"]


@pytest.mark.parametrize("name, expected", [
    ("X0084684/02", ("X0084684", "02")),
    ("R0094702-DAWGS-005, BOAT TAIL, LOWER SKIN, AFT/01", ("R0094702", "01")),
    ("@DB/X0084684/02", None),
    ("model.prt", None),
])
def test_split_part_name(name, expected):
    assert journal.split_part_name(name) == expected


def test_open_component_status_name_accepts_ints_and_members():
    assert journal.open_component_status_name(2) == "CouldNotOpen"
    assert journal.open_component_status_name("OpenComponentStatus.SuccessfullyOpened") == (
        "SuccessfullyOpened")


def test_component_part_name_falls_back_when_display_name_is_blank():
    component = FakeComponent(7, "")
    assert journal.component_part_name(component) == "instance-7"
    component.Name = ""
    assert journal.component_part_name(component).startswith("(unnamed component")


# --- journal orchestration --------------------------------------------------------

def run_capture(tmp_path, capsys, prepare=None, open_part=None, render=None):
    image_path = tmp_path / "R1_01.jpg"
    request = {"item_id": "R1", "revision": "01", "image_path": str(image_path),
               "image_format": "jpg", "width": 800, "height": 600}
    calls = []

    def default_open(item_id, revision):
        calls.append("open")
        return {"part": "PART", "reference": "@DB/ref", "components": 3,
                "components_not_loaded": [], "load_notes": {}, "writable_parts": []}

    def default_render(part):
        calls.append(("render", part))
        write_test_image(image_path)
        return {"method": "ImageExportBuilder", "hidden_objects": 2, "notes": []}

    journal.capture(
        request,
        prepare=prepare or (lambda: calls.append("prepare")),
        open_part=open_part or default_open,
        render=render or default_render,
        close_parts=lambda: calls.append("close"),
    )
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith(journal.RESULT_PREFIX)]
    assert len(lines) == 1
    return cli.parse_result_line(lines[0]), calls


def test_capture_reports_the_image_and_closes_parts(tmp_path, capsys):
    result, calls = run_capture(tmp_path, capsys)
    assert calls == ["prepare", "open", ("render", "PART"), "close"]
    assert result["ok"] and result["path"] == str(tmp_path / "R1_01.jpg")
    assert (result["components"], result["hidden_objects"], result["method"]) == (
        3, 2, "ImageExportBuilder"
    )
    assert "part" not in result


def test_capture_opens_nothing_when_autolock_cannot_be_disabled(tmp_path, capsys):
    message = "Teamcenter automatic check-out is not confirmed off (True); no part was opened."

    def unsafe():
        raise journal.SafetyError(message)

    result, calls = run_capture(tmp_path, capsys, prepare=unsafe)
    assert calls == ["close"]
    assert result == {"ok": False, "item_id": "R1", "revision": "01", "error": message}


def test_capture_reports_open_failures_and_still_closes(tmp_path, capsys):
    def missing(item_id, revision):
        raise journal.TeamcenterOpenError("Not found in Teamcenter or access denied.")

    result, calls = run_capture(tmp_path, capsys, open_part=missing)
    assert calls == ["prepare", "close"]
    assert result["error"] == "Not found in Teamcenter or access denied."


def test_capture_reports_unexpected_errors_with_their_type(tmp_path, capsys):
    def broken(part):
        raise ValueError("bad view")

    result, calls = run_capture(tmp_path, capsys, render=broken)
    assert calls[-1] == "close"
    assert result["error"] == "ValueError: bad view"


def test_capture_fails_when_no_image_was_written(tmp_path, capsys):
    result, _ = run_capture(tmp_path, capsys, render=lambda part: {"method": "x"})
    assert not result["ok"] and "did not write" in result["error"]


def test_capture_reports_the_file_the_renderer_actually_wrote(tmp_path, capsys):
    fallback = tmp_path / "R1_01.jpg.fallback.jpg"

    def batch_shade(part):
        write_test_image(fallback)
        return {"method": "UF_DISP_batch_shade", "path": str(fallback)}

    result, _ = run_capture(tmp_path, capsys, render=batch_shade)
    assert result["ok"] and result["path"] == str(fallback)
    assert result["method"] == "UF_DISP_batch_shade"


def test_batch_shade_fallback_writes_a_format_it_supports():
    assert journal.batch_shade_path(Path("a/R1_01.png"), "png") == Path("a/R1_01.jpg")
    assert journal.batch_shade_path(Path("a/R1_01.jpg"), "jpg") == Path("a/R1_01.jpg")
    assert journal.batch_shade_path(Path("a/R1_01.tif"), "tif") == Path("a/R1_01.tif")


# --- the journal must never alter part files ---------------------------------------

def _called_method_names(source: str) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


@pytest.mark.parametrize("forbidden", [
    r"^Save",                 # Part.Save/SaveAs, PartCollection.SaveAll, Layout.Save ...
    r"check_?out",            # any Teamcenter check-out call
    r"^SetPartPreviewMode",
])
def test_journal_never_saves_or_checks_out(forbidden):
    """Image capture must leave .prt files and Teamcenter untouched."""
    offenders = {name for name in _called_method_names(JOURNAL_SOURCE)
                 if re.search(forbidden, name, re.IGNORECASE)}
    assert not offenders


def test_journal_never_assigns_part_preview_mode():
    assert not re.search(r"PartPreviewMode\s*=[^=]", JOURNAL_SOURCE)


def test_journal_disables_autolock_and_discards_changes_on_close():
    assert "SetAutolockStatus(False)" in JOURNAL_SOURCE
    assert "SetAutolockStatus(True" not in JOURNAL_SOURCE
    assert "CloseAll(NXOpen.BasePart.CloseModified.CloseModified, None)" in JOURNAL_SOURCE


# --- framing ----------------------------------------------------------------------

def test_render_size_oversamples_within_limits():
    assert cli.render_size(1600, 1200) == (3200, 2400)
    assert cli.render_size(4000, 3000) == (6000, 4500)
    assert cli.render_size(8000, 100) == (8000, 100)


def drawn_box(path):
    from PIL import Image

    with Image.open(path) as image:
        box, background = cli.content_box(image.convert("RGB"))
        return box, background, image.size


def test_frame_image_centers_the_geometry_with_an_even_margin(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "render.png"
    render = image_module.new("RGB", (800, 600), (255, 255, 255))
    for x in range(40, 200):          # 160 x 80 shape in the top-left corner
        for y in range(20, 100):
            render.putpixel((x, y), (30, 30, 200))
    render.save(source)
    target = tmp_path / "final.png"
    cli.frame_image(source, target, 400, 300, "png")
    (left, top, right, bottom), background, size = drawn_box(target)
    assert size == (400, 300) and background == (255, 255, 255)
    assert (right - left) == pytest.approx(360, abs=2)   # 90% of the width
    assert (left + right) / 2 == pytest.approx(200, abs=2)
    assert (top + bottom) / 2 == pytest.approx(150, abs=2)


def test_frame_image_keeps_a_non_white_background(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "render.jpg"
    render = image_module.new("RGB", (300, 300), (211, 212, 214))
    render.paste((0, 0, 255), (100, 120, 140, 160))
    render.save(source, quality=95)
    target = tmp_path / "final.jpg"
    cli.frame_image(source, target, 200, 100, "jpg")
    box, background, size = drawn_box(target)
    assert size == (200, 100) and box is not None
    assert all(abs(a - b) <= 6 for a, b in zip(background, (211, 212, 214)))


def test_frame_image_rejects_blank_renders(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "render.png"
    image_module.new("RGB", (50, 50), (255, 255, 255)).save(source)
    with pytest.raises(cli.BlankImageError):
        cli.frame_image(source, tmp_path / "final.png", 64, 64, "png")


@pytest.mark.parametrize("image_format, pil_format", [
    ("jpg", "JPEG"), ("png", "PNG"), ("bmp", "BMP"), ("tif", "TIFF"), ("gif", "GIF"),
])
def test_save_image_writes_every_supported_format(tmp_path, image_format, pil_format):
    image_module = pytest.importorskip("PIL.Image")
    target = tmp_path / f"out.{image_format}"
    cli.save_image(image_module.new("RGB", (8, 8), (10, 20, 30)), target, image_format)
    with image_module.open(target) as image:
        assert image.format == pil_format


def test_rendered_image_only_accepts_files_in_the_work_folder(tmp_path):
    inside = tmp_path / "work" / "a.png"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    outside = tmp_path / "b.png"
    outside.write_bytes(b"x")
    assert cli.rendered_image({"ok": True, "path": str(inside)}, inside.parent) == inside
    assert cli.rendered_image({"ok": True, "path": str(outside)}, inside.parent) is None
    assert cli.rendered_image({"ok": False, "error": "x"}, inside.parent) is None


# --- end to end without NX ---------------------------------------------------------

def fake_journal_session(write_image=True, blank=False, ok=True):
    commands = []

    def run(command, environment, work_dir, on_line):
        commands.append((command, environment))
        request = journal.load_request(Path(environment[cli.REQUEST_FILE_ENV]))
        commands[-1] += (request,)
        on_line(journal.PROGRESS_PREFIX + "Opening R0093916/01 from Teamcenter...")
        if write_image:
            write_test_image(Path(request["image_path"]), blank=blank)
        if ok:
            payload = {"ok": True, "item_id": request["item_id"], "revision": request["revision"],
                       "path": request["image_path"], "width": request["width"],
                       "height": request["height"], "components": 121,
                       "components_not_loaded": ["MISSING-PART"],
                       "load_notes": {"Read-only part is modified.": 6}, "notes": []}
        else:
            payload = {"ok": False, "item_id": request["item_id"],
                       "revision": request["revision"], "error": "Not found in Teamcenter."}
        on_line(journal.RESULT_PREFIX + json.dumps(payload))
        return 0

    return run, commands


@pytest.fixture
def nx_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "check_setup", lambda ugii_base_dir, python_home: None)
    monkeypatch.setenv("UGII_BASE_DIR", str(tmp_path / "NX"))


def test_main_saves_the_image_in_a_new_folder(tmp_path, monkeypatch, capsys, nx_ready):
    run, commands = fake_journal_session()
    monkeypatch.setattr(cli, "run_journal_session", run)
    folder = tmp_path / "shots" / "new"
    assert cli.main(["--part", "R0093916/01", "--folder", str(folder)]) == 0
    assert (folder / "R0093916_01.jpg").is_file()
    assert [path.name for path in folder.iterdir()] == ["R0093916_01.jpg"]
    command, environment, request = commands[0]
    assert command == [str(tmp_path / "NX" / "NXBIN" / "run_journal.exe"), "-pim",
                       str(cli.JOURNAL_PATH)]
    assert environment["NX_ENABLE_HEADLESS_GRAPHICS"] == "1"
    if cli.pillow_available():  # NX renders a large PNG that is framed afterwards
        assert (request["image_format"], request["width"], request["height"]) == ("png", 3200, 2400)
        from PIL import Image
        with Image.open(folder / "R0093916_01.jpg") as image:
            assert (image.format, image.size) == ("JPEG", (1600, 1200))
    output = capsys.readouterr().out
    assert "Opening R0093916/01 from Teamcenter..." in output
    assert "NX load note (6x): Read-only part is modified." in output
    assert "1 component(s) could not be loaded:\n    MISSING-PART\n" in output
    assert f"Saved {folder / 'R0093916_01.jpg'} (1600x1200)" in output


def test_main_replaces_an_existing_image(tmp_path, monkeypatch, nx_ready):
    run, _ = fake_journal_session()
    monkeypatch.setattr(cli, "run_journal_session", run)
    (tmp_path / "R0093916_01.png").write_bytes(b"old")
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path), "--format", "png"]) == 0
    assert (tmp_path / "R0093916_01.png").read_bytes() != b"old"
    assert not list(tmp_path.glob("*.tmp"))


def test_main_without_pillow_publishes_what_nx_wrote(tmp_path, monkeypatch, nx_ready):
    run, commands = fake_journal_session()
    monkeypatch.setattr(cli, "run_journal_session", run)
    monkeypatch.setattr(cli, "pillow_available", lambda: False)
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path), "--width", "800",
                     "--height", "600"]) == 0
    _, _, request = commands[0]
    assert (request["image_format"], request["width"], request["height"]) == ("jpg", 800, 600)
    assert (tmp_path / "R0093916_01.jpg").is_file()


def test_main_reports_journal_failures(tmp_path, monkeypatch, capsys, nx_ready):
    run, _ = fake_journal_session(write_image=False, ok=False)
    monkeypatch.setattr(cli, "run_journal_session", run)
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path)]) == 1
    assert "FAILED: Not found in Teamcenter." in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_main_rejects_blank_images(tmp_path, monkeypatch, capsys, nx_ready):
    pytest.importorskip("PIL")
    run, _ = fake_journal_session(blank=True)
    monkeypatch.setattr(cli, "run_journal_session", run)
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path)]) == 1
    assert "The image is blank" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_main_shows_nx_output_when_there_is_no_result(tmp_path, monkeypatch, capsys, nx_ready):
    def silent(command, environment, work_dir, on_line):
        on_line("Teamcenter login failed")
        return 1

    monkeypatch.setattr(cli, "run_journal_session", silent)
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "NX exited with code 1." in captured.out and "  Teamcenter login failed" in captured.out
    assert "FAILED: NX did not report a result." in captured.err


def test_main_log_file_gets_console_output_and_all_nx_output(
    tmp_path, monkeypatch, capsys, nx_ready,
):
    run, _ = fake_journal_session()

    def chatty(command, environment, work_dir, on_line):
        on_line("NX load status: X0084684/02: File not found")
        return run(command, environment, work_dir, on_line)

    monkeypatch.setattr(cli, "run_journal_session", chatty)
    log_file = tmp_path / "logs" / "run.log"
    log_file.parent.mkdir()
    log_file.write_text("earlier run\n", encoding="utf-8")
    argv = ["--part", "R0093916/01", "--folder", str(tmp_path / "out"), "--log-file", str(log_file)]
    assert cli.main(argv) == 0
    console = capsys.readouterr().out
    assert "File not found" not in console  # NX detail stays out of the console without -v
    log = log_file.read_text(encoding="utf-8")
    assert log.startswith("earlier run\n")  # appended, not replaced
    assert "part_screenshot.py --part R0093916/01" in log
    assert "  [NX] NX load status: X0084684/02: File not found" in log
    assert "Opening R0093916/01 from Teamcenter..." in log
    assert "Saved " in log and log.rstrip().endswith("Exit code 0")
    assert sys.stdout is not None and not isinstance(sys.stdout, cli.TeeStream)


def test_main_log_file_records_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UGII_BASE_DIR", raising=False)
    log_file = tmp_path / "new" / "run.log"  # missing folders are created
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path),
                     "--log-file", str(log_file)]) == 2
    assert "UGII_BASE_DIR is not set" in log_file.read_text(encoding="utf-8")


def test_main_reports_a_log_file_it_cannot_open(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path),
                     "--log-file", str(blocker / "run.log")]) == 2
    assert "Cannot open log file" in capsys.readouterr().err


def test_main_requires_ugii_base_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UGII_BASE_DIR", raising=False)
    assert cli.main(["--part", "R0093916/01", "--folder", str(tmp_path)]) == 2
    assert "UGII_BASE_DIR is not set" in capsys.readouterr().err


def test_main_rejects_a_part_without_revision(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--part", "R0093916", "--folder", str(tmp_path)])
    assert exit_info.value.code == 2
    assert "is not ITEM_ID/REVISION, e.g. 123456/A" in capsys.readouterr().err

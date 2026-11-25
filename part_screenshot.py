"""Save an isometric image of a Teamcenter part revision, fully loaded in NX.

Run from a command prompt where UGII_BASE_DIR points at the NX installation::

    C:\\CAD\\DAPS_Plugin\\python.exe part_screenshot.py --part 123456/A --folder C:\\screenshots

A headless NX session (``run_journal.exe -pim`` with NX_ENABLE_HEADLESS_GRAPHICS=1)
runs ``tc_part_screenshot_journal.py``. It opens the revision from Teamcenter with
every component fully loaded, hides datums, sketches, curves, PMI and similar
reference objects, shows the bodies isometric (shaded with edges, white
background) and renders an image at twice the requested size. This script then
crops it to the drawn geometry and centers it with an even margin in
``<ITEM_ID>_<REVISION>.jpg`` (or ``--format``) in the folder, which is created if
it is missing. Without Pillow, NX's image is saved as rendered.

Parts are opened read-only with Teamcenter automatic check-out switched off, and
are never saved. The Teamcenter environment mirrors extract-previews-from-nx and
``daps_utils.teamcenter_nx`` in design-app-nx-plugin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

JOURNAL_PATH = Path(__file__).resolve().with_name("tc_part_screenshot_journal.py")
# Protocol shared with the journal; keep in sync with tc_part_screenshot_journal.py.
REQUEST_FILE_ENV = "PART_SCREENSHOT_REQUEST_FILE"
RESULT_PREFIX = "PART_SCREENSHOT_RESULT "
PROGRESS_PREFIX = "PART_SCREENSHOT_PROGRESS "
IMAGE_FORMATS = ("jpg", "png", "bmp", "tif", "gif")
# (FMS_HOME, NX_SSO_URL) of the Teamcenter client installs probed when FMS_HOME is unset.
TCCS_INSTALLS = (
    (r"C:\SPLM\Siemens\TC2412\TC_ROOT\tccs",
     "http://PDTCWEB.corp.divergent3d.com:7003/loginservice/tccs"),
    (r"D:\SPLM\Siemens\TC2412\TC_ROOT\tccs", ""),
)
MIN_SIZE, MAX_SIZE = 64, 8192
# NX renders at up to this multiple of the requested size; the drawn geometry is then
# cropped and scaled down into the final image, which centers it and smooths edges.
RENDER_OVERSAMPLE = 2
MAX_RENDER_SIZE = 6000
FRAME_MARGIN = 0.05  # empty border on each side of the final image, as a fraction
BACKGROUND_TOLERANCE = 24  # color difference that still counts as background
OUTPUT_TAIL_LINES = 40
PART_EXAMPLE = "123456/A"


class UsageError(Exception):
    """A problem with the command-line input or the local NX setup."""


class BlankImageError(Exception):
    """NX produced an image without any visible geometry."""


@dataclass(frozen=True)
class ItemRevision:
    item_id: str
    revision: str

    def __str__(self) -> str:
        return f"{self.item_id}/{self.revision}"


def parse_part(value: str) -> ItemRevision:
    """Parse ``ITEM_ID/REVISION`` such as ``123456/A``."""
    fields = [field.strip() for field in value.split("/")]
    if len(fields) != 2 or not all(fields):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not ITEM_ID/REVISION, e.g. {PART_EXAMPLE}"
        )
    if any(re.search(r"[,=\s]", field) for field in fields):
        raise argparse.ArgumentTypeError(
            f"{value!r}: the item ID and revision cannot contain spaces, ',' or '='"
        )
    return ItemRevision(*fields)


def image_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of pixels") from None
    if not MIN_SIZE <= size <= MAX_SIZE:
        raise argparse.ArgumentTypeError(f"must be between {MIN_SIZE} and {MAX_SIZE} pixels")
    return size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Save an isometric image of a Teamcenter part revision, fully loaded in NX.",
        epilog=rf"Example: python.exe part_screenshot.py --part {PART_EXAMPLE} "
               r"--folder C:\screenshots",
    )
    parser.add_argument(
        "--part", required=True, type=parse_part, metavar="ITEM_ID/REVISION",
        help=f"Teamcenter item ID and revision, e.g. {PART_EXAMPLE}",
    )
    parser.add_argument(
        "--folder", required=True, type=Path,
        help="folder to save the image in (created if missing)",
    )
    parser.add_argument("--width", type=image_size, default=1600, help="image width in pixels (default 1600)")
    parser.add_argument("--height", type=image_size, default=1200, help="image height in pixels (default 1200)")
    parser.add_argument(
        "--format", choices=IMAGE_FORMATS, default="jpg", dest="image_format",
        help="image file format (default jpg)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show all NX output")
    parser.add_argument(
        "--log-file", type=Path, metavar="PATH",
        help="also append all output, including every NX output line, to this file",
    )
    return parser


def image_file_name(item: ItemRevision, image_format: str) -> str:
    """Return a file-system-safe name such as ``R0093916_01.jpg``."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{item.item_id}_{item.revision}") + f".{image_format}"


def find_ugii_base_dir(environ: Mapping[str, str]) -> Path:
    value = environ.get("UGII_BASE_DIR", "").strip()
    if not value:
        raise UsageError(
            r"UGII_BASE_DIR is not set. Set it to the NX install folder, e.g. C:\CAD\NX2406."
        )
    return Path(value)


def run_journal_path(ugii_base_dir: Path) -> Path:
    return ugii_base_dir / "NXBIN" / "run_journal.exe"


def python_dll_name() -> str:
    """Return the DLL of the running Python, which NX loads to run the journal."""
    return f"python{sys.version_info.major}{sys.version_info.minor}.dll"


def check_setup(ugii_base_dir: Path, python_home: Path) -> None:
    """Fail early, with a clear message, if NX would be unable to run the journal."""
    run_journal = run_journal_path(ugii_base_dir)
    if not run_journal.is_file():
        raise UsageError(f"run_journal.exe not found at {run_journal}. Check UGII_BASE_DIR.")
    if not JOURNAL_PATH.is_file():
        raise UsageError(f"NX journal not found at {JOURNAL_PATH}. Keep it next to this script.")
    dll = python_dll_name()
    if not (python_home / dll).is_file() or not (ugii_base_dir / "NXBIN" / dll).is_file():
        raise UsageError(
            f"NX at {ugii_base_dir} cannot run journals with this Python ({python_home}, {dll}). "
            r"Run this script with the DAPS embedded Python, e.g. C:\CAD\DAPS_Plugin\python.exe."
        )


def prepare_output_folder(folder: Path) -> Path:
    """Create the output folder if needed; return it as an absolute path."""
    folder = Path(os.path.abspath(folder))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UsageError(
            f"Cannot create output folder {folder}: {error.strerror or error}"
        ) from error
    return folder


def teamcenter_client_settings(
    environ: Mapping[str, str],
    installs: Sequence[tuple[str, str]] = TCCS_INSTALLS,
) -> tuple[str, str]:
    """Return (FMS_HOME, NX_SSO_URL), preferring an FMS_HOME that is already set."""
    fms_home = environ.get("FMS_HOME", "")
    if fms_home:
        return fms_home, environ.get("NX_SSO_URL", "")
    for candidate_home, sso_url in installs:
        if Path(candidate_home).exists():
            return candidate_home, sso_url
    return installs[-1]


def build_nx_environment(
    environ: Mapping[str, str],
    ugii_base_dir: Path,
    python_home: Path,
    request_file: Path,
) -> dict[str, str]:
    """Return the run_journal environment: Teamcenter client, NX and embedded Python."""
    environment = dict(environ)
    fms_home, sso_url = teamcenter_client_settings(environment)
    tc_root = environment.get("TC_ROOT") or fms_home.replace(r"\tccs", "")
    python_paths = (
        python_home,
        python_home / "Lib" / "python_zip",
        python_home / "DLLs",
        python_home / "Lib",
        python_home / "Lib" / "site-packages",
        ugii_base_dir / "nxbin" / "python",
        ugii_base_dir / "design_tools" / "checkmate" / "python",
    )
    environment.update({
        "NX_SSO_APP_ID": "TC",
        "NX_SSO_URL": sso_url,
        "FMS_HOME": fms_home,
        "TC_ROOT": tc_root,
        "IMAN_ROOT": tc_root,
        "UGII_BASE_DIR": str(ugii_base_dir),
        "DAPS_PLUGIN_DIR": str(python_home),
        "UGII_PYTHONPATH": ";".join(str(path) for path in python_paths),
        "UGII_PYTHON_DLL": python_dll_name(),
        "UGII_PYTHON_HOME": str(python_home),
        "UGII_PYTHON_LIBRARY_DIR": str(python_home),
        "PYTHONUNBUFFERED": "1",
        # Lets Gateway.ImageExportBuilder render without a window (NX 2406 docs:
        # "Exporting an image when running non-interactive NX").
        "NX_ENABLE_HEADLESS_GRAPHICS": "1",
        # No automatic Teamcenter check-out when a part changes in memory; the
        # journal also switches autolock off and refuses to open parts otherwise.
        "UGII_UGMGR_NOAUTOLOCK": "1",
        REQUEST_FILE_ENV: str(request_file),
    })
    environment.setdefault("UGII_USER_DIR", str(python_home))
    environment["PATH"] = f"{ugii_base_dir / 'NXBIN'};{environment.get('PATH', '')}"
    return environment


def write_request(
    work_dir: Path, item: ItemRevision, image_format: str, width: int, height: int,
) -> tuple[Path, Path]:
    """Write the journal request; return (request file, image path in work_dir)."""
    image_path = work_dir / image_file_name(item, image_format)
    request_file = work_dir / "request.json"
    request_file.write_text(json.dumps({
        "item_id": item.item_id,
        "revision": item.revision,
        "image_path": str(image_path),
        "image_format": image_format,
        "width": width,
        "height": height,
    }), encoding="utf-8")
    return request_file, image_path


def parse_result_line(line: str) -> dict[str, Any] | None:
    """Return the journal result on an output line, if the line carries one."""
    if not line.startswith(RESULT_PREFIX):
        return None
    try:
        payload = json.loads(line[len(RESULT_PREFIX):])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return None
    if payload["ok"] and not isinstance(payload.get("path"), str):
        return None
    return payload


class TeeStream:
    """A text stream that writes to the console stream and to the log file."""

    def __init__(self, console: TextIO, log: TextIO) -> None:
        self.console, self.log = console, log

    def write(self, text: str) -> int:
        self.console.write(text)
        self.log.write(text)
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.console, name)


def open_log_file(path: Path, argv: Sequence[str]) -> TextIO:
    """Open the log file for appending and write a header for this run."""
    path = Path(os.path.abspath(path))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        log = path.open("a", encoding="utf-8", errors="replace")
    except OSError as error:
        raise UsageError(f"Cannot open log file {path}: {error.strerror or error}") from error
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} part_screenshot.py "
              f"{' '.join(argv)}\n")
    log.flush()
    return log


class SessionMonitor:
    """Show journal progress as it arrives and keep the result and recent output.

    NX output that is not shown on the console still goes to ``log``, if given.
    """

    def __init__(self, verbose: bool, log: TextIO | None = None) -> None:
        self.verbose = verbose
        self.log = log
        self.result: dict[str, Any] | None = None
        self.output_tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)

    def handle_line(self, line: str) -> None:
        result = parse_result_line(line)
        if result is not None:
            self.result = result
            return
        if line.startswith(PROGRESS_PREFIX):
            print(line[len(PROGRESS_PREFIX):], flush=True)
            return
        self.output_tail.append(line)
        if self.verbose:
            print(line, flush=True)  # reaches the log file through TeeStream
        elif self.log is not None:
            self.log.write(f"  [NX] {line}\n")
            self.log.flush()


def run_journal_session(
    command: Sequence[str],
    environment: Mapping[str, str],
    work_dir: Path,
    on_line: Callable[[str], None],
) -> int:
    """Run NX, passing each output line to ``on_line``; return its exit code."""
    with subprocess.Popen(
        list(command),
        env=dict(environment),
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as process:
        try:
            for line in process.stdout:
                on_line(line.rstrip("\r\n"))
        except KeyboardInterrupt:
            process.kill()
            raise
        return process.wait()


def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def render_size(width: int, height: int) -> tuple[int, int]:
    """Return the size NX renders at so the final image can be cropped and scaled down."""
    scale = min(RENDER_OVERSAMPLE, MAX_RENDER_SIZE / max(width, height))
    scale = max(scale, 1.0)
    return round(width * scale), round(height * scale)


def content_box(image: Any) -> tuple[tuple[int, int, int, int] | None, tuple[int, ...]]:
    """Return the bounding box of everything that differs from the background.

    The background color is taken from the top-left pixel. Returns (None, color)
    when the image holds nothing but background.
    """
    from PIL import Image, ImageChops

    background = image.getpixel((0, 0))
    difference = ImageChops.difference(image, Image.new(image.mode, image.size, background))
    mask = difference.convert("L").point(lambda value: 255 if value > BACKGROUND_TOLERANCE else 0)
    return mask.getbbox(), background


def frame_image(source: Path, target: Path, width: int, height: int, image_format: str) -> None:
    """Center the drawn geometry in a width x height image with an even margin."""
    from PIL import Image

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    box, background = content_box(image)
    if box is None:
        print("ERR: Couldn't center image", file=sys.stderr)
        save_image(image, target, image_format)
        return
    content = image.crop(box)
    usable_width, usable_height = width * (1 - 2 * FRAME_MARGIN), height * (1 - 2 * FRAME_MARGIN)
    scale = min(usable_width / content.width, usable_height / content.height)
    size = (max(1, round(content.width * scale)), max(1, round(content.height * scale)))
    content = content.resize(size, Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), background)
    canvas.paste(content, ((width - content.width) // 2, (height - content.height) // 2))
    save_image(canvas, target, image_format)


def save_image(image: Any, target: Path, image_format: str) -> None:
    from PIL import Image

    if image_format == "jpg":
        image.save(target, "JPEG", quality=92, optimize=True)
    elif image_format == "png":
        image.save(target, "PNG", optimize=True)
    elif image_format == "tif":
        image.save(target, "TIFF", compression="tiff_lzw")
    elif image_format == "gif":
        image.convert("P", palette=Image.ADAPTIVE).save(target, "GIF")
    else:
        image.save(target, "BMP")


def publish_image(source: Path, target: Path) -> Path:
    """Copy the image to target, replacing any earlier one in a single step."""
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target


def rendered_image(result: dict[str, Any] | None, work_dir: Path) -> Path | None:
    """Return the image NX reported, if it exists inside the work folder."""
    if not result or not result.get("ok"):
        return None
    path = Path(result["path"])
    if path.parent.resolve() != work_dir.resolve() or not path.is_file():
        return None
    return path


def take_screenshot(
    item: ItemRevision,
    folder: Path,
    image_format: str,
    width: int,
    height: int,
    ugii_base_dir: Path,
    python_home: Path,
    verbose: bool,
    log: TextIO | None = None,
) -> tuple[SessionMonitor, int, Path | None]:
    """Run one headless NX session; return its monitor, exit code and published image."""
    print(f"Starting NX from {ugii_base_dir} and connecting to Teamcenter; "
          "this can take a few minutes...", flush=True)
    monitor = SessionMonitor(verbose, log)
    target = folder / image_file_name(item, image_format)
    # With Pillow, NX renders a large lossless PNG that is then framed; without it,
    # NX writes the final image directly.
    framing = pillow_available()
    render_format, (render_width, render_height) = (
        ("png", render_size(width, height)) if framing else (image_format, (width, height))
    )
    with tempfile.TemporaryDirectory(prefix="part_screenshot_", ignore_cleanup_errors=True) as work:
        work_dir = Path(work)
        request_file, _ = write_request(work_dir, item, render_format, render_width, render_height)
        environment = build_nx_environment(os.environ, ugii_base_dir, python_home, request_file)
        command = [str(run_journal_path(ugii_base_dir)), "-pim", str(JOURNAL_PATH)]
        return_code = run_journal_session(command, environment, work_dir, monitor.handle_line)
        rendered = rendered_image(monitor.result, work_dir)
        if rendered is None:
            if monitor.result and monitor.result.get("ok"):
                monitor.result.update(ok=False, error="NX did not write the image it reported.")
            return monitor, return_code, None
        monitor.result.update(width=width, height=height)
        if not framing:
            if rendered.suffix.lower() != f".{image_format}":
                monitor.result.update(ok=False, error=(
                    f"NX could only write {rendered.suffix} here; install Pillow to convert it."
                ))
                return monitor, return_code, None
            return monitor, return_code, publish_image(rendered, target)
        framed = work_dir / f"framed_{target.name}"
        try:
            frame_image(rendered, framed, width, height, image_format)
        except BlankImageError as error:
            monitor.result.update(ok=False, error=str(error))
            return monitor, return_code, None
        return monitor, return_code, publish_image(framed, target)


def report(monitor: SessionMonitor, return_code: int, published: Path | None) -> int:
    """Print the outcome; return the process exit code."""
    result = monitor.result or {}
    if published is not None:
        for description, count in sorted(result.get("load_notes", {}).items()):
            print(f"  NX load note ({count}x): {description}")
        not_loaded = result.get("components_not_loaded") or []
        if not_loaded:
            print(f"  Warning: {len(not_loaded)} component(s) could not be loaded:")
            for line in not_loaded:
                print(f"    {line}")
        for note in result.get("notes", []):
            print(f"  Note: {note}")
        print(f"Saved {published} ({result.get('width')}x{result.get('height')})")
        return 0
    error = result.get("error") or "NX did not report a result."
    if not monitor.verbose and monitor.output_tail and not result:
        print(f"\nNX exited with code {return_code}. Last NX output:")
        print("\n".join(f"  {line}" for line in monitor.output_tail))
    print(f"FAILED: {error}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.log_file is None:
        return run(args, None)
    try:
        log = open_log_file(args.log_file, argv)
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = TeeStream(stdout, log), TeeStream(stderr, log)
    try:
        exit_code = run(args, log)
        print(f"Exit code {exit_code}", file=log)
        return exit_code
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        log.close()


def run(args: argparse.Namespace, log: TextIO | None) -> int:
    python_home = Path(sys.base_prefix)
    try:
        ugii_base_dir = find_ugii_base_dir(os.environ)
        check_setup(ugii_base_dir, python_home)
        folder = prepare_output_folder(args.folder)
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"Saving an isometric image of {args.part} to {folder}")
    try:
        monitor, return_code, published = take_screenshot(
            args.part, folder, args.image_format, args.width, args.height,
            ugii_base_dir, python_home, args.verbose, log,
        )
    except KeyboardInterrupt:
        print("\nCancelled; NX was stopped.", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"error: could not save the image in {folder}: {error}", file=sys.stderr)
        return 1
    return report(monitor, return_code, published)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    sys.exit(main())

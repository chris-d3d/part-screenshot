"""NX journal that saves an isometric image of a Teamcenter part revision.

``part_screenshot.py`` runs this journal through ``NXBIN/run_journal.exe -pim``
with ``NX_ENABLE_HEADLESS_GRAPHICS=1``. It writes a JSON request file and passes
its path in the ``PART_SCREENSHOT_REQUEST_FILE`` environment variable::

    {"item_id": "R0093916", "revision": "01",
     "image_path": "C:\\Temp\\part_screenshot_x\\R0093916_01.jpg",
     "image_format": "jpg", "width": 1600, "height": 1200}

Progress is printed on ``PART_SCREENSHOT_PROGRESS`` lines and the outcome on one
``PART_SCREENSHOT_RESULT {json}`` line.

Part files are never altered:

* Teamcenter automatic check-out (autolock) is switched off before anything is
  opened, so every part is opened read-only;
* nothing here saves or checks out a part. The view orientation, hidden objects
  and display settings changed below exist only in this NX session's memory;
* every part is closed at the end with its in-memory changes discarded.

NXOpen is imported inside the functions that need it so the helpers can be unit
tested outside NX.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from collections import Counter, deque
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable

# Protocol shared with part_screenshot.py; keep in sync.
REQUEST_FILE_ENV = "PART_SCREENSHOT_REQUEST_FILE"
RESULT_PREFIX = "PART_SCREENSHOT_RESULT "
PROGRESS_PREFIX = "PART_SCREENSHOT_PROGRESS "

# Object types tried by daps_utils.teamcenter_nx in design-app-nx-plugin.
TEAMCENTER_OBJECT_TYPE_FALLBACKS = (
    "Czinger Kit Revision",
    "Czinger Revision",
    "Divergent Non Automotive Revision",
    "D3D_Divergent",
)
# File extension -> NXOpen.Gateway.ImageExportBuilder.FileFormats member.
IMAGE_FORMATS = {"jpg": "Jpg", "png": "Png", "bmp": "Bmp", "tif": "Tiff", "gif": "Gif"}
# Formats UF_DISP_batch_shade can write; used only if headless graphics are unavailable.
BATCH_SHADE_FORMATS = frozenset({"jpg", "tif", "gif"})
# Show and Hide types that are never hidden: aggregates that include bodies, and the
# body and component types themselves (see types_to_hide).
ALWAYS_VISIBLE_TYPES = frozenset({"SHOW_HIDE_TYPE_ALL", "SHOW_HIDE_TYPE_GEOMETRY"})
# Fraction of the fitted view the geometry fills, leaving a margin around it.
VIEW_MARGIN = 0.9
# Rounds of explicitly opening unloaded components (each can reveal more below).
MAX_LOAD_ROUNDS = 5
# NXOpen.Assemblies.ComponentAssembly.OpenComponentStatus, which Python can report as an int.
OPEN_COMPONENT_STATUSES = ("SuccessfullyOpened", "DeletedByOpen", "CouldNotOpen")
DRAWING_DISPLAY_STATE = 2
MODELING_DISPLAY_STATE = 1


class SafetyError(RuntimeError):
    """The session could not be made safe for opening Teamcenter parts."""


class TeamcenterOpenError(RuntimeError):
    """No supported Teamcenter reference opened the requested part."""


class ScreenshotError(RuntimeError):
    """The part opened but no image could be produced."""


def build_search_specs(item_id: str, revision: str) -> list[str]:
    """Return the managed-mode part references used by daps_utils.teamcenter_nx."""
    references = [
        f"@DB/%#MFK#%,=item_id={item_id},object_type={object_type}/{revision}"
        for object_type in TEAMCENTER_OBJECT_TYPE_FALLBACKS
    ]
    references.extend((
        f"@DB/%#MFK#%,=item_id={item_id},item_revision_id={revision}",
        f"@DB/%#MFK#%,=item_id={item_id},revision={revision}",
        f"@DB/{item_id}/{revision}",
    ))
    return list(dict.fromkeys(references))


def types_to_hide(types: Iterable[str]) -> list[str]:
    """Return the Show and Hide types that are not model geometry.

    Bodies and components keep the visibility they were saved with; datums,
    sketches, curves, points, coordinate systems, PMI, analysis objects and other
    reference clutter are hidden so the image shows only the geometry.
    """
    return [
        name for name in types
        if name.strip() not in ALWAYS_VISIBLE_TYPES
        and "BODIES" not in name
        and "COMPONENTS" not in name
    ]


def summarize_load_notes(notes: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Count NX load status messages by description."""
    return dict(Counter(description for _, description in notes))


def disable_automatic_checkout(uf_session: Any) -> None:
    """Stop NX from checking parts out of Teamcenter when they change in memory.

    Must succeed before any part is opened; otherwise nothing is opened at all.
    """
    ugmgr = uf_session.Ugmgr
    try:
        ugmgr.SetAutolockStatus(False)
        status = ugmgr.AskAutolockStatus()
    except Exception as error:
        raise SafetyError(
            f"Could not switch off Teamcenter automatic check-out ({error}); "
            "no part was opened."
        ) from error
    # NX2406 returns a bool; anything but an explicit False is treated as still on.
    if status is not False:
        raise SafetyError(
            f"Teamcenter automatic check-out is not confirmed off ({status!r}); "
            "no part was opened."
        )


def configure_session(session: Any) -> None:
    """Load every component fully and show the geometry exactly as it was saved."""
    import NXOpen

    options = session.Parts.LoadOptions
    options.ComponentsToLoad = NXOpen.LoadOptions.LoadComponents.All
    options.UsePartialLoading = False
    options.PartLoadOption = NXOpen.LoadOptions.LoadOption.FullyLoad
    options.UseLightweightRepresentations = False
    options.AbortOnFailure = False
    updates = session.UpdateManager
    # Do not recompute WAVE links or assembly constraints while loading. If an
    # update fails anyway, keep the part open instead of the batch default (Undo),
    # which rolls back the whole open and leaves no part in the session.
    updates.InterpartDelay = True
    updates.AssemblyConstraintsDelay = True
    updates.SetDefaultUpdateFailureAction(NXOpen.Update.FailureOption.AcceptAll)


def prepare_session(session: Any, uf_session: Any) -> None:
    disable_automatic_checkout(uf_session)
    configure_session(session)


def read_load_status(status: Any) -> list[tuple[str, str]]:
    """Return (part name, description) for each entry of a PartLoadStatus."""
    if status is None:
        return []
    try:
        return [
            (str(status.GetPartName(index)), str(status.GetStatusDescription(index)))
            for index in range(status.NumberUnloadedParts)
        ]
    finally:
        status.Dispose()


def open_teamcenter_part(
    session: Any, uf_session: Any, item_id: str, revision: str,
) -> tuple[Any, str, list[tuple[str, str]]]:
    """Open a managed part as the display part with the first reference NX accepts."""
    import NXOpen

    references = []
    errors = []
    try:
        references.append(str(uf_session.Ugmgr.EncodePartFilename(item_id, revision, "", "")))
    except Exception as error:
        errors.append(f"encode {item_id}/{revision}: {error}")
    references.extend(build_search_specs(item_id, revision))
    for reference in dict.fromkeys(references):
        try:
            part, status = session.Parts.OpenActiveDisplay(
                reference, NXOpen.DisplayPartOption.AllowAdditional
            )
        except Exception as error:
            errors.append(str(error))
            continue
        notes = read_load_status(status)
        # OpenActiveDisplay can return None for the part in Python; the display part is reliable.
        part = part or session.Parts.Display
        if part is None:
            # NX accepted the reference but nothing stayed open. The remaining
            # references name the same item, so retrying would only repeat the load.
            details = "; ".join(f"{name}: {text}" for name, text in notes) or "no details"
            raise TeamcenterOpenError(
                f"NX loaded {item_id}/{revision} but no part stayed open. NX reported: {details}"
            )
        return part, reference, notes
    raise TeamcenterOpenError(
        "Not found in Teamcenter or access denied. NX reported: "
        + " | ".join(dict.fromkeys(errors))
    )


def iter_components(part: Any) -> list[Any]:
    """Return every component under the part's root component, breadth first."""
    root = part.ComponentAssembly.RootComponent
    if root is None:
        return []
    components, pending = [], deque(root.GetChildren())
    while pending:
        component = pending.popleft()
        components.append(component)
        pending.extend(component.GetChildren())
    return components


def _is_suppressed(component: Any) -> bool:
    try:
        return bool(component.IsSuppressed)
    except Exception:
        return False


def _component_key(component: Any) -> Any:
    return getattr(component, "Tag", None) or id(component)


def component_part_name(component: Any, uf_session: Any = None) -> str:
    """Return the part a component refers to, even when that part is not loaded.

    DisplayName comes from the loaded part and is blank when it did not load, so
    the instance data NX keeps in the parent assembly is asked next.
    """
    getters = [lambda: component.DisplayName]
    if uf_session is not None:
        getters.append(lambda: uf_session.Assem.AskComponentData(component.Tag)[0])
    getters.append(lambda: component.Name)
    for getter in getters:
        try:
            value = str(getter() or "").strip()
        except Exception:
            continue
        if value:
            return value
    return f"(unnamed component {getattr(component, 'JournalIdentifier', '?')})"


def describe_component(component: Any, uf_session: Any = None) -> str:
    """Return the component's part name and the assembly it is in."""
    name = component_part_name(component, uf_session)
    try:
        parent = component.Parent
    except Exception:
        parent = None
    if parent is None:
        return name
    return f"{name} (in {component_part_name(parent, uf_session)})"


def load_prototypes(components: Iterable[Any], notes: list[tuple[str, str]]) -> list[Any]:
    """Fully load every loaded prototype; return the components with no part loaded.

    Suppressed components are left out on purpose and are not returned.
    """
    import NXOpen

    unloaded = []
    for component in components:
        prototype = component.Prototype
        if not isinstance(prototype, NXOpen.BasePart):
            if not _is_suppressed(component):
                unloaded.append(component)
            continue
        if not prototype.IsFullyLoaded:
            notes.extend(read_load_status(prototype.LoadThisPartFully()))
    return unloaded


def open_component_status_name(status: Any) -> str:
    """Return the OpenComponentStatus member name for an enum member or its int value."""
    if isinstance(status, int) and 0 <= status < len(OPEN_COMPONENT_STATUSES):
        return OPEN_COMPONENT_STATUSES[status]
    return str(status).rsplit(".", 1)[-1]


def open_components(
    part: Any, components: list[Any], notes: list[tuple[str, str]],
) -> dict[Any, str]:
    """Open unloaded components as File > Open Component does; return failure reasons.

    The reasons are keyed by component and come from NX's per-component status.
    """
    import NXOpen.Assemblies

    assembly_type = NXOpen.Assemblies.ComponentAssembly
    try:
        result = part.ComponentAssembly.OpenComponents(
            assembly_type.OpenOption.ComponentOnly, components
        )
    except Exception as error:
        reason = f"Open Component failed: {error}"
        return {_component_key(component): reason for component in components}
    status, open_statuses = result if isinstance(result, tuple) else (result, [])
    notes.extend(read_load_status(status))
    reasons = {}
    for component, open_status in zip(components, open_statuses or []):
        text = open_component_status_name(open_status)
        if text != "SuccessfullyOpened":
            reasons[_component_key(component)] = f"Open Component: {text}"
    return reasons


def split_part_name(name: str) -> tuple[str, str] | None:
    """Return (item ID, revision) from a part name such as ``X0084684/02`` or
    ``R0094702-BOAT TAIL, SKIN/01``; None if the name is not in that form."""
    match = re.fullmatch(r"([A-Za-z0-9_.]+)(?:-[^/]*)?/([^/\s]+)", name.strip())
    return (match.group(1), match.group(2)) if match else None


def open_part_by_name(
    session: Any, uf_session: Any, name: str, notes: list[tuple[str, str]],
) -> str | None:
    """Open a component's part straight from Teamcenter; return NX's error, or None.

    Open Component reports only "CouldNotOpen"; opening the item revision itself
    raises the actual reason (not found, no read access, ...). Once the part is
    in the session, Open Component can bind the component to it.
    """
    item = split_part_name(name)
    if item is None:
        return f"could not open it directly: {name!r} is not ITEM_ID/REVISION"
    references, errors = [], []
    try:
        references.append(str(uf_session.Ugmgr.EncodePartFilename(item[0], item[1], "", "")))
    except Exception as error:
        errors.append(f"encode: {error}")
    references.extend(build_search_specs(*item))
    for reference in dict.fromkeys(references):
        try:
            result = session.Parts.Open(reference)
        except Exception as error:
            errors.append(str(error))
            continue
        opened, status = result if isinstance(result, tuple) else (result, None)
        notes.extend(read_load_status(status))
        print(f"Opened component part {name} directly with reference {reference}", flush=True)
        return None
    return "opening it directly failed: " + " | ".join(dict.fromkeys(errors))


def fully_load(
    part: Any, uf_session: Any = None, session: Any = None,
) -> tuple[int, list[str], list[tuple[str, str]]]:
    """Make sure the part and every component are fully loaded.

    Components that did not load with the assembly are opened explicitly, which
    can reveal further components below them, so this repeats until nothing new
    opens. A component that Open Component cannot open has its part opened straight
    from Teamcenter (when ``session`` is given), which either reveals NX's reason
    or lets a second Open Component succeed. Returns the component count, a "part (in assembly): reason" line for
    each component that still has no part loaded, and the NX load status entries.
    """
    notes: list[tuple[str, str]] = []
    if not part.IsFullyLoaded:
        notes.extend(read_load_status(part.LoadThisPartFully()))
    attempted: set[Any] = set()
    reasons: dict[Any, str] = {}
    components: list[Any] = []
    unloaded: list[Any] = []
    for _ in range(MAX_LOAD_ROUNDS):
        components = iter_components(part)
        unloaded = load_prototypes(components, notes)
        retry = [component for component in unloaded if _component_key(component) not in attempted]
        if not retry:
            break
        attempted.update(_component_key(component) for component in retry)
        failed = open_components(part, retry, notes)
        reasons.update(failed)
        if session is None:
            continue
        reopen = []
        for component in retry:
            key = _component_key(component)
            if key not in failed:
                continue
            error = open_part_by_name(
                session, uf_session, component_part_name(component, uf_session), notes)
            if error:
                reasons[key] = f"{failed[key]}; {error}"
            else:
                reopen.append(component)
        if reopen:
            second = open_components(part, reopen, notes)
            for component in reopen:
                key = _component_key(component)
                if key in second:
                    reasons[key] = (f"{second[key]} even after its part was opened "
                                    "directly from Teamcenter")
                else:
                    reasons.pop(key, None)
    not_loaded = []
    for component in unloaded:
        name = component_part_name(component, uf_session)
        reason = reasons.get(_component_key(component)) or "; ".join(dict.fromkeys(
            text for note_name, text in notes if note_name and note_name in name
        )) or "NX gave no reason"
        not_loaded.append(f"{describe_component(component, uf_session)}: {reason}")
    return len(components), not_loaded, notes


def show_modeling_layout(part: Any, uf_session: Any, notes: list[str]) -> None:
    """Show the modeling display with a single view, in memory only."""
    try:
        if uf_session.Draw.AskDisplayState() == DRAWING_DISPLAY_STATE:
            uf_session.Draw.SetDisplayState(MODELING_DISPLAY_STATE)
    except Exception as error:
        notes.append(f"Could not switch from the drawing to the model: {error}")
    try:
        layout = part.Layouts.Current
        if layout is not None and len(layout.GetViews()) > 1:
            part.Layouts.ChangeLayout(part.Layouts.FindObject("L1"))
    except Exception as error:
        notes.append(f"Could not switch to the single-view layout: {error}")


def visible_bodies(view: Any, notes: list[str]) -> list[Any]:
    """Return the solid and sheet bodies visible in the view (occurrences included)."""
    import NXOpen

    try:
        return [obj for obj in view.AskVisibleObjects() if isinstance(obj, NXOpen.Body)]
    except Exception as error:
        notes.append(f"Could not list the visible bodies, so the view fits everything: {error}")
        return []


def fit_view(view: Any, bodies: list[Any], notes: list[str]) -> None:
    """Fit the view to the bodies; Fit() alone can include objects that draw nothing."""
    if bodies:
        try:
            view.FitToObjects(bodies)
            return
        except Exception as error:
            notes.append(f"Could not fit the view to the bodies, so it fits everything: {error}")
    view.Fit()


def prepare_isometric_view(session: Any, part: Any, notes: list[str]) -> tuple[Any, int, int]:
    """Orient the work view isometric around the visible geometry, in memory only.

    Returns the view, the number of hidden objects and the number of visible bodies.
    """
    import NXOpen

    view = part.ModelingViews.WorkView
    view.TriadVisibility = False
    view.WcsVisibility = False
    hidden = 0
    scope = NXOpen.DisplayManager.ShowHideScope.AnyInAssembly
    for type_name in types_to_hide(session.DisplayManager.GetShowableHideableTypes()):
        try:
            hidden += session.DisplayManager.HideByType(type_name, scope)
        except Exception as error:
            notes.append(f"Could not hide {type_name.strip()}: {error}")
    view.ChangePerspective(False)
    view.RenderingStyle = NXOpen.View.RenderingStyleType.ShadedWithEdges
    view.Orient(NXOpen.View.Canned.Isometric, NXOpen.View.ScaleAdjustment.Fit)
    bodies = visible_bodies(view, notes)
    fit_view(view, bodies, notes)
    view.SetScale(view.Scale * VIEW_MARGIN)
    return view, hidden, len(bodies)


def export_with_image_builder(
    part: Any, path: Path, image_format: str, width: int, height: int,
) -> None:
    """Export the work view through Gateway.ImageExportBuilder (headless graphics)."""
    import NXOpen
    import NXOpen.Gateway

    formats = NXOpen.Gateway.ImageExportBuilder.FileFormats
    backgrounds = NXOpen.Gateway.ImageExportBuilder.BackgroundOptions
    builder = part.Views.CreateImageExportBuilder()
    try:
        builder.RegionMode = False
        builder.DeviceWidth = width
        builder.DeviceHeight = height
        builder.FileFormat = getattr(formats, IMAGE_FORMATS[image_format])
        builder.FileName = str(path)
        builder.BackgroundOption = backgrounds.CustomColor
        builder.SetCustomBackgroundColor([1.0, 1.0, 1.0])
        builder.EnhanceEdges = False
        builder.Commit()
    finally:
        builder.Destroy()


def batch_shade_path(path: Path, image_format: str) -> Path:
    """Return where UF_DISP_batch_shade can write an image requested at ``path``."""
    return path if image_format in BATCH_SHADE_FORMATS else path.with_suffix(".jpg")


def export_image(
    part: Any, uf_session: Any, path: Path, image_format: str, width: int, height: int,
) -> tuple[str, Path]:
    """Write the image; return the method used and the file written."""
    import NXOpen.UF

    try:
        export_with_image_builder(part, path, image_format, width, height)
        return "ImageExportBuilder", path
    except Exception as error:
        # Without headless graphics (no certified graphics card, or the variable
        # unset), fall back to the deprecated but window-free batch shade renderer.
        emit_progress(f"Image export failed ({error}); using UF_DISP_batch_shade instead.")
        _remove_quietly(path)
        fallback = batch_shade_path(path, image_format)
        uf_session.Disp.BatchShade(str(fallback), width, height, NXOpen.UF.DispShadeMethod.PHONG)
        return "UF_DISP_batch_shade", fallback


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def open_and_load(session: Any, uf_session: Any, item_id: str, revision: str) -> dict[str, Any]:
    emit_progress(f"Opening {item_id}/{revision} from Teamcenter with all components fully loaded...")
    started = time.monotonic()
    part, reference, load_notes = open_teamcenter_part(session, uf_session, item_id, revision)
    component_count, not_loaded, more_notes = fully_load(part, uf_session, session)
    load_notes.extend(more_notes)
    # Plain output lines: shown with --verbose and always written to --log-file.
    print(f"Opened {item_id}/{revision} with reference {reference}", flush=True)
    for name, description in load_notes:
        print(f"NX load status: {name}: {description}", flush=True)
    for line in not_loaded:
        print(f"Component not loaded: {line}", flush=True)
    emit_progress(f"Opened in {time.monotonic() - started:.0f} s: {component_count} component(s).")
    return {
        "part": part,
        "reference": reference,
        "components": component_count,
        "components_not_loaded": not_loaded,
        "load_notes": summarize_load_notes(load_notes),
        "writable_parts": sorted(str(p.Leaf) for p in session.Parts if not p.IsReadOnly),
    }


def render_isometric(
    session: Any, uf_session: Any, request: dict[str, Any], part: Any,
) -> dict[str, Any]:
    emit_progress("Rendering the isometric view...")
    notes: list[str] = []
    show_modeling_layout(part, uf_session, notes)
    _, hidden, body_count = prepare_isometric_view(session, part, notes)
    method, path = export_image(
        part, uf_session, Path(request["image_path"]), request["image_format"],
        request["width"], request["height"],
    )
    return {"method": method, "path": str(path), "hidden_objects": hidden,
            "visible_bodies": body_count, "notes": notes}


def close_all_parts(session: Any) -> None:
    """Close every part in the session, discarding in-memory changes (never saves)."""
    import NXOpen

    session.Parts.CloseAll(NXOpen.BasePart.CloseModified.CloseModified, None)


def capture(
    request: dict[str, Any],
    prepare: Callable[[], None],
    open_part: Callable[[str, str], dict[str, Any]],
    render: Callable[[Any], dict[str, Any]],
    close_parts: Callable[[], None],
) -> None:
    """Produce the image for the requested part; emit exactly one result line."""
    item_id, revision = request["item_id"], request["revision"]
    try:
        prepare()
        opened = open_part(item_id, revision)
        rendered = render(opened.pop("part"))
        path = Path(rendered.pop("path", request["image_path"]))
        if not path.is_file() or path.stat().st_size == 0:
            raise ScreenshotError(f"NX reported success but did not write {path}.")
        _emit({
            "ok": True,
            "item_id": item_id,
            "revision": revision,
            "path": str(path),
            "width": request["width"],
            "height": request["height"],
            **opened,
            **rendered,
        })
    except (SafetyError, TeamcenterOpenError, ScreenshotError) as error:
        _emit_failure(item_id, revision, str(error))
    except Exception as error:
        traceback.print_exc()
        _emit_failure(item_id, revision, f"{type(error).__name__}: {error}")
    finally:
        _close_quietly(close_parts)


def _close_quietly(close_parts: Callable[[], None]) -> None:
    try:
        close_parts()
    except Exception as error:
        print(f"Could not close parts: {type(error).__name__}: {error}", flush=True)


def emit_progress(message: str) -> None:
    print(PROGRESS_PREFIX + message, flush=True)


def _emit_failure(item_id: str, revision: str, error: str) -> None:
    _emit({"ok": False, "item_id": item_id, "revision": revision, "error": error})


def _emit(payload: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True), flush=True)


def load_request(path: Path) -> dict[str, Any]:
    """Return the request written by part_screenshot.py."""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    import NXOpen
    import NXOpen.UF

    request = load_request(Path(os.environ[REQUEST_FILE_ENV]))
    session = NXOpen.Session.GetSession()
    uf_session = NXOpen.UF.UFSession.GetUFSession()
    capture(
        request,
        prepare=partial(prepare_session, session, uf_session),
        open_part=partial(open_and_load, session, uf_session),
        render=partial(render_isometric, session, uf_session, request),
        close_parts=partial(close_all_parts, session),
    )


if __name__ == "__main__":
    main()

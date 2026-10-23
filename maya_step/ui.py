"""Small native Maya import dialog; no Qt version dependency."""

from functools import partial

from maya import cmds, mel

from .options import DEFAULT_OPTIONS, ImportOptions

WINDOW = "mayaStepImportWindow"
MENU = "mayaStepImportMenu"


def _browse(field, caption, file_filter=None, *_):
    arguments = {"fileMode": 1, "caption": caption}
    if file_filter:
        arguments["fileFilter"] = file_filter
    chosen = cmds.fileDialog2(**arguments)
    if chosen:
        cmds.textFieldButtonGrp(field, edit=True, text=chosen[0])


def remove_menu():
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)


def install_menu():
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)
    main_window = mel.eval("$stepMainWindow = $gMainWindow")
    cmds.menu(MENU, label="STEP", parent=main_window, tearOff=False)
    cmds.menuItem(label="Import STEP...", command=lambda *_: show())


def show():
    from .bridge import python_executable
    from .scene import ensure_plugin_loaded, import_step

    ensure_plugin_loaded()
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)
    window = cmds.window(WINDOW, title="Import STEP geometry", sizeable=True, widthHeight=(570, 355))
    cmds.columnLayout(adjustableColumn=True, rowSpacing=8, columnAttach=("both", 12))
    cmds.separator(height=4, style="none")
    cmds.text(label="Convert STEP solids and surfaces to editable polygon meshes.", align="left")
    file_field = cmds.textFieldButtonGrp(label="STEP file", buttonLabel="Browse...", columnWidth3=(120, 330, 80))

    cmds.textFieldButtonGrp(file_field, edit=True, buttonCommand=partial(
        _browse, file_field, "Choose STEP file", "STEP (*.step *.stp *.STEP *.STP)"))
    linear = cmds.floatFieldGrp(label="Detail tolerance (mm)", numberOfFields=1,
                               value1=DEFAULT_OPTIONS.linear_deflection, precision=6)
    angular = cmds.floatFieldGrp(label="Angle tolerance (deg)", numberOfFields=1,
                                value1=DEFAULT_OPTIONS.angular_deflection, precision=2)
    cmds.text(label="Smaller tolerances produce finer meshes and take longer to import.", align="left")
    scale = cmds.floatFieldGrp(label="Scale multiplier", numberOfFields=1,
                              value1=DEFAULT_OPTIONS.scale, precision=4)
    up = cmds.optionMenuGrp(label="CAD up axis")
    cmds.menuItem(label="Z")
    cmds.menuItem(label="Y")
    cmds.optionMenuGrp(up, edit=True, value=DEFAULT_OPTIONS.source_up)
    cache = cmds.checkBox(label="Reuse cached geometry", value=DEFAULT_OPTIONS.use_cache)
    cmds.separator(style="in", height=12)
    try:
        initial_python = python_executable()
    except RuntimeError:
        initial_python = ""
    python_field = cmds.textFieldButtonGrp(
        label="Converter Python", buttonLabel="Browse...", text=initial_python, columnWidth3=(120, 330, 80))

    cmds.textFieldButtonGrp(python_field, edit=True, buttonCommand=partial(
        _browse, python_field, "Choose external Python executable", None))
    cmds.text(label="Choose Python with the Open CASCADE converter installed.", align="left")

    def run(*_):
        try:
            options = ImportOptions(
                linear_deflection=cmds.floatFieldGrp(linear, query=True, value1=True),
                angular_deflection=cmds.floatFieldGrp(angular, query=True, value1=True),
                scale=cmds.floatFieldGrp(scale, query=True, value1=True),
                source_up=cmds.optionMenuGrp(up, query=True, value=True),
                use_cache=cmds.checkBox(cache, query=True, value=True))
            interpreter = cmds.textFieldButtonGrp(python_field, query=True, text=True).strip()
            if interpreter:
                cmds.optionVar(stringValue=("mayaStepPython", interpreter))
            elif cmds.optionVar(exists="mayaStepPython"):
                cmds.optionVar(remove="mayaStepPython")
            root = import_step(cmds.textFieldButtonGrp(file_field, query=True, text=True), options)
            cmds.select(root, replace=True)
        except Exception as exc:
            cmds.warning(str(exc))
            cmds.confirmDialog(title="STEP import failed", message=str(exc), button=["OK"])

    cmds.button(label="Import geometry", height=36, command=run)
    cmds.showWindow(window)
    return window

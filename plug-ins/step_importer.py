"""Maya Python file translator and undoable STEP mesh import command."""

from pathlib import Path
import inspect
import sys

# Maya executes Python plug-ins without defining __file__.
ROOT = str(Path(inspect.getfile(inspect.currentframe())).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# MPxFileTranslator is available in Maya Python API 1.0, not API 2.0.
from maya import cmds
import maya.OpenMaya as om1
import maya.OpenMayaMPx as ompx
from maya.api import OpenMaya as om2

from maya_step.bridge import convert_file
from maya_step.options import DEFAULT_OPTIONS, ImportOptions
from maya_step.scene import create_geometry

TRANSLATOR = "STEP Mesh"
COMMAND = "stepImportFile"


class StepImportCommand(ompx.MPxCommand):
    def __init__(self):
        super(StepImportCommand, self).__init__()
        self.root = None
        self.removal = None

    @staticmethod
    def creator():
        return ompx.asMPxPtr(StepImportCommand())

    def doIt(self, args):
        if args.length() != 2:
            raise RuntimeError("stepImportFile requires a STEP path and an options string")
        options = ImportOptions.from_string(args.asString(1))
        interactive = not cmds.about(batch=True)
        if interactive:
            interactive = bool(cmds.progressWindow(
                title="Import STEP", status="Tessellating CAD geometry...",
                isInterruptable=True, progress=0, maxValue=100))

        def progress():
            if cmds.progressWindow(query=True, isCancelled=True):
                raise RuntimeError("STEP import cancelled")

        last_status = [None]

        def status(message, fraction):
            progress()
            state = (message, int(100 * fraction))
            if state != last_status[0]:
                cmds.progressWindow(edit=True, status=state[0], progress=state[1])
                last_status[0] = state

        try:
            payload = convert_file(args.asString(0), options, progress if interactive else None,
                                   status if interactive else None)
            if interactive:
                progress()
                cmds.progressWindow(edit=True, status="Creating polygon meshes...", progress=90)
            # Keep redraw suspended while many mesh nodes are being added.
            if interactive:
                cmds.refresh(suspend=True)
            try:
                self.root = create_geometry(payload, options, status if interactive else None)
            finally:
                if interactive:
                    cmds.refresh(suspend=False)
        finally:
            if interactive:
                cmds.progressWindow(endProgress=True)
        self.setResult(om2.MFnDagNode(self.root).fullPathName())
        om1.MGlobal.displayInfo("Imported {} STEP bodies ({} triangles)".format(
            len(payload["meshes"]), payload.get("triangle_count", 0)))
        if payload.get("degenerate_faces"):
            om1.MGlobal.displayWarning("Skipped {} zero-area CAD faces".format(payload["degenerate_faces"]))
        if payload.get("sliver_faces"):
            om1.MGlobal.displayWarning("Skipped {} collapsed CAD sliver faces below detail tolerance "
                                      "({:.8g} mm2 total)".format(
                                          payload["sliver_faces"], payload["sliver_area_mm2"]))
        if payload.get("omitted_bodies"):
            om1.MGlobal.displayWarning("Omitted {} bodies containing only degenerate CAD geometry".format(
                payload["omitted_bodies"]))

    def isUndoable(self):
        return True

    def undoIt(self):
        if self.removal is None:
            self.removal = om2.MDagModifier()
            self.removal.deleteNode(self.root)
        self.removal.doIt()

    def redoIt(self):
        self.removal.undoIt()


class StepTranslator(ompx.MPxFileTranslator):
    @staticmethod
    def creator():
        return ompx.asMPxPtr(StepTranslator())

    def haveReadMethod(self):
        return True

    def haveWriteMethod(self):
        return False

    def canBeOpened(self):
        return False

    def defaultExtension(self):
        return "step"

    def filter(self):
        return "*.step;*.stp"

    def identifyFile(self, file_object, buffer, size):
        if Path(file_object.fullName()).suffix.lower() in (".step", ".stp"):
            return self.kIsMyFileType
        return self.kNotMyFileType

    def reader(self, file_object, options_string, access_mode):
        if access_mode != self.kImportAccessMode:
            raise RuntimeError("Use File > Import for STEP files; opening and referencing are not supported")
        cmds.stepImportFile(file_object.fullName(), options_string)


def initializePlugin(plugin_object):
    plugin = ompx.MFnPlugin(plugin_object, "Maya STEP Importer", "1.0.0", "Any")
    plugin.registerCommand(COMMAND, StepImportCommand.creator)
    try:
        plugin.registerFileTranslator(TRANSLATOR, None, StepTranslator.creator,
                                      None, DEFAULT_OPTIONS.to_string(), True)
    except Exception:
        plugin.deregisterCommand(COMMAND)
        raise
    if not cmds.about(batch=True):
        from maya_step.ui import install_menu
        install_menu()


def uninitializePlugin(plugin_object):
    if not cmds.about(batch=True):
        from maya_step.ui import remove_menu
        remove_menu()
    plugin = ompx.MFnPlugin(plugin_object)
    plugin.deregisterFileTranslator(TRANSLATOR)
    plugin.deregisterCommand(COMMAND)

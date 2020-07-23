import logging
import os.path
import random
import fnmatch
from functools import partial
import subprocess
import threading

from Qt import QtCore, QtWidgets, QtGui
from pxr import Usd, Sdf, Ar, UsdUtils

import utils
from vendor.Nodz import nodz_main
from . import text_view

import re
from pprint import pprint


reload(text_view)

reload(nodz_main)

digitSearch = re.compile(r'\b\d+\b')

logger = logging.getLogger('usd-dependency-graph')
logger.setLevel(logging.INFO)
if not len(logger.handlers):
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)
logger.propagate = False


def launch_usdview(usdfile):
    print 'launching usdview', usdfile
    subprocess.call(['usdview', usdfile], shell=True)


class DependencyWalker(object):
    def __init__(self, usdfile):
        self.usdfile = usdfile
        self.walk_attributes = True
        
        logger.info('DependencyWalker'.center(40, '-'))
        logger.info('Loading usd file: {}'.format(self.usdfile))
        self.nodes = {}
        self.edges = []
        
        self.visited_nodes = []
    
    
    def start(self):
        self.visited_nodes = []
        self.nodes = {}
        self.edges = []
        self.init_edges = []
        
        layer = Sdf.Layer.FindOrOpen(self.usdfile)
        if not layer:
            return
        
        # scrub the initial file path
        # to get around upper/lowercase drive letters
        # and junk like that
        layer_path = Sdf.ComputeAssetPathRelativeToLayer(layer, os.path.basename(self.usdfile))
        
        self.usdfile = layer_path
        
        info = {}
        info['online'] = os.path.isfile(layer_path)
        info['path'] = layer_path
        info['type'] = 'layer'
        self.nodes[layer_path] = info
        
        self.walkStageLayers(layer_path)
    
    
    def get_flat_child_list(self, path):
        ret = [path]
        for key, child in path.nameChildren.items():
            ret.extend(self.get_flat_child_list(child))
        ret = list(set(ret))
        return ret
    
    
    def flatten_ref_list(self, ref_or_payload):
        ret = []
        for itemlist in [ref_or_payload.appendedItems, ref_or_payload.explicitItems, ref_or_payload.addedItems,
                         ref_or_payload.prependedItems, ref_or_payload.orderedItems]:
            for payload in itemlist:
                ret.append(payload)
        return list(set(ret))
    
    
    def walkStageLayers(self, layer_path, level=1):
        id = '-' * (level)
        
        sublayers = []
        payloads = []
        references = []
        
        layer = Sdf.Layer.FindOrOpen(layer_path)
        if not layer:
            return
        # print id, layer.realPath
        root = layer.pseudoRoot
        # print id, 'root', root
        
        # print id, 'children'.center(40, '-')
        
        child_list = self.get_flat_child_list(root)
        
        for child in child_list:
            # print id, child
            clip_info = child.GetInfo("clips")
            # pprint(clip_info)
            
            if self.walk_attributes:
                attributes = child.attributes
                for attr in attributes:
                    # we are looking for "asset" type attributes
                    # references to external things
                    if attr.typeName == 'asset':
                        value = attr.default
                        # sometimes you get empty paths
                        if value.path:
                            resolved_path = Sdf.ComputeAssetPathRelativeToLayer(layer, value.path)
                            info = {}
                            info['online'] = os.path.isfile(resolved_path)
                            info['path'] = resolved_path
                            info['type'] = 'tex'
                            
                            self.nodes[resolved_path] = info
                            
                            if not [layer_path, resolved_path, 'tex'] in self.edges:
                                self.edges.append([layer_path, resolved_path, 'tex'])
            
            for clip_set_name in clip_info:
                clip_set = clip_info[clip_set_name]
                # print clip_set_name, clip_set.get("assetPaths"), clip_set.get("manifestAssetPath"), clip_set.get(
                #     "primPath")
                
                """
                @todo: subframe handling
                integer frames: path/basename.###.usd
                subinteger frames: path/basename.##.##.usd.
                
                @todo: non-1 increments
                """
                clip_asset_paths = clip_set.get("assetPaths")
                # don't use resolved path in case either the first or last file is missing from disk
                firstFile = str(clip_asset_paths[0].path)
                lastFile = str(clip_asset_paths[-1].path)
                firstFileNum = digitSearch.findall(firstFile)[-1]
                lastFileNum = digitSearch.findall(lastFile)[-1]
                digitRange = str(firstFileNum + '-' + lastFileNum)
                nodeName = ''
                
                firstFileParts = firstFile.split(firstFileNum)
                for i in range(len(firstFileParts) - 1):
                    nodeName += str(firstFileParts[i])
                
                nodeName += digitRange
                nodeName += firstFileParts[-1]
                
                allFilesFound = True
                for path in clip_asset_paths:
                    if (path.resolvedPath == ''):
                        allFilesFound = False
                        break
                
                # TODO : make more efficient - looping over everything currently
                # TODO: validate presence of all files in the clip seq. bg thread?
                
                manifestPath = clip_set.get("manifestAssetPath")
                # print manifestPath, type(manifestPath)
                refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, clip_asset_paths[0].path)
                clipmanifest_path = Sdf.ComputeAssetPathRelativeToLayer(layer, manifestPath.path)
                # print id, Sdf.ComputeAssetPathRelativeToLayer(layer, manifestPath.path)
                
                info = {}
                info['online'] = True
                info['path'] = refpath
                info['type'] = 'clip'
                
                self.nodes[nodeName] = info
                
                if not [layer_path, nodeName, 'clip'] in self.edges:
                    self.edges.append([layer_path, nodeName, 'clip'])
            
            if child.variantSets:
                for varset in child.variantSets:
                    # print varset.name
                    for variant_name in varset.variants.keys():
                        variant = varset.variants[variant_name]
                        payloadList = variant.primSpec.payloadList
                        
                        # so variants can host payloads and references
                        # we get to these through the variants primspec
                        # and then add them to our list of paths to inspect
                        for primspec in self.get_flat_child_list(variant.primSpec):
                            payloadList = self.flatten_ref_list(primspec.payloadList)
                            for payload in payloadList:
                                pathToResolve = payload.assetPath
                                if pathToResolve:
                                    refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, pathToResolve)
                                    payloads.append(refpath)
                                    
                                    info = {}
                                    info['online'] = True
                                    info['path'] = refpath
                                    info['type'] = 'payload'
                                    
                                    self.nodes[refpath] = info
                                    
                                    if not [layer_path, refpath, 'payload'] in self.edges:
                                        self.edges.append([layer_path, refpath, 'payload'])
                            
                            referenceList = self.flatten_ref_list(child.referenceList)
                            for reference in referenceList:
                                pathToResolve = reference.assetPath
                                if pathToResolve:
                                    refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, pathToResolve)
                                    references.append(refpath)
                                    
                                    info = {}
                                    info['online'] = True
                                    info['path'] = refpath
                                    info['type'] = 'reference'
                                    
                                    self.nodes[refpath] = info
                                    
                                    if not [layer_path, refpath, 'reference'] in self.edges:
                                        self.edges.append([layer_path, refpath, 'reference'])
            
            payloadList = self.flatten_ref_list(child.payloadList)
            for payload in payloadList:
                pathToResolve = payload.assetPath
                if pathToResolve:
                    refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, pathToResolve)
                    payloads.append(refpath)
                    
                    info = {}
                    info['online'] = True
                    info['path'] = refpath
                    info['type'] = 'payload'
                    
                    self.nodes[refpath] = info
                    
                    if not [layer_path, refpath, 'payload'] in self.edges:
                        self.edges.append([layer_path, refpath, 'payload'])
            
            referenceList = self.flatten_ref_list(child.referenceList)
            for reference in referenceList:
                pathToResolve = reference.assetPath
                if pathToResolve:
                    refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, pathToResolve)
                    references.append(refpath)
                    
                    info = {}
                    info['online'] = True
                    info['path'] = refpath
                    info['type'] = 'reference'
                    
                    self.nodes[refpath] = info
                    
                    if not [layer_path, refpath, 'reference'] in self.edges:
                        self.edges.append([layer_path, refpath, 'reference'])
        
        for rel_sublayer in layer.subLayerPaths:
            refpath = Sdf.ComputeAssetPathRelativeToLayer(layer, rel_sublayer)
            sublayers.append(refpath)
            
            info = {}
            info['online'] = True
            info['path'] = refpath
            info['type'] = 'sublayer'
            
            self.nodes[refpath] = info
            
            if not [layer_path, refpath, 'sublayer'] in self.edges:
                self.edges.append([layer_path, refpath, 'sublayer'])
        
        sublayers = list(set(sublayers))
        references = list(set(references))
        payloads = list(set(payloads))
        
        if sublayers:
            logger.debug((id, 'sublayerPaths'.center(40, '-')))
            logger.debug((id, sublayers))
        for sublayer in sublayers:
            self.walkStageLayers(sublayer, level=level + 1)
        
        if references:
            logger.debug((id, 'references'.center(40, '-')))
            logger.debug((id, references))
        for reference in references:
            self.walkStageLayers(reference, level=level + 1)
        
        if payloads:
            logger.debug((id, 'payloads'.center(40, '-')))
            logger.debug((id, payloads))
        for payload in payloads:
            self.walkStageLayers(payload, level=level + 1)
    
    
    def layerprops(self, layer):
        print 'layer props'.center(40, '-')
        
        for prop in ['anonymous', 'colorConfiguration', 'colorManagementSystem', 'comment', 'customLayerData',
                     'defaultPrim', 'dirty', 'documentation', 'empty', 'endTimeCode', 'expired', 'externalReferences',
                     'fileExtension', 'framePrecision',
                     'framesPerSecond', 'hasOwnedSubLayers', 'identifier', 'owner', 'permissionToEdit',
                     'permissionToSave', 'pseudoRoot', 'realPath', 'repositoryPath', 'rootPrimOrder', 'rootPrims',
                     'sessionOwner', 'startTimeCode', 'subLayerOffsets', 'subLayerPaths', 'timeCodesPerSecond',
                     'version']:
            print prop, getattr(layer, prop)
        print ''.center(40, '-')
        
        defaultprim = layer.defaultPrim
        if defaultprim:
            print defaultprim, type(defaultprim)


def find_node(node_coll, attr_name, attr_value):
    for x in node_coll:
        node = node_coll[x]
        if getattr(node, attr_name) == attr_value:
            return node


@QtCore.Slot(str, object)
def on_nodeMoved(nodeName, nodePos):
    # print('node {0} moved to {1}'.format(nodeName, nodePos))
    pass


class FindNodeWindow(QtWidgets.QDialog):
    def __init__(self, nodz, parent=None):
        self.nodz = nodz
        super(FindNodeWindow, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        
        self.build_ui()
    
    
    def search(self):
        search_text = self.searchTxt.text()
        
        self.foundNodeList.clear()
        if search_text == '':
            return
        
        for x in sorted(self.nodz.scene().nodes):
            if fnmatch.fnmatch(x.lower(), '*%s*' % search_text.lower()):
                self.foundNodeList.addItem(QtWidgets.QListWidgetItem(x))
    
    
    def item_selected(self, *args):
        items = self.foundNodeList.selectedItems()
        if items:
            sel = [x.text() for x in items]
            
            for x in self.nodz.scene().nodes:
                node = self.nodz.scene().nodes[x]
                if x in sel:
                    node.setSelected(True)
                else:
                    node.setSelected(False)
            self.nodz._focus()
    
    
    def build_ui(self):
        lay = QtWidgets.QVBoxLayout()
        self.setLayout(lay)
        self.searchTxt = QtWidgets.QLineEdit()
        self.searchTxt.textChanged.connect(self.search)
        lay.addWidget(self.searchTxt)
        
        self.foundNodeList = QtWidgets.QListWidget()
        self.foundNodeList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.foundNodeList.itemSelectionChanged.connect(self.item_selected)
        lay.addWidget(self.foundNodeList)


class NodeGraphWindow(QtWidgets.QDialog):
    def __init__(self, usdfile=None, parent=None):
        self.usdfile = usdfile
        self.root_node = None
        
        super(NodeGraphWindow, self).__init__(parent)
        self.settings = QtCore.QSettings("chrisg", "usd-dependency-graph")
        
        self.nodz = None
        
        self.find_win = None
        self.build_ui()
        if self.usdfile:
            self.load_file()
    
    
    def build_ui(self):
        
        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        else:
            self.resize(600, 400)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMinimizeButtonHint);
        lay = QtWidgets.QVBoxLayout()
        self.setLayout(lay)
        
        self.toolbar_lay = QtWidgets.QHBoxLayout()
        lay.addLayout(self.toolbar_lay)
        
        self.openBtn = QtWidgets.QPushButton("Open...", )
        self.openBtn.setShortcut('Ctrl+o')
        self.openBtn.clicked.connect(self.manualOpen)
        self.toolbar_lay.addWidget(self.openBtn)
        
        self.reloadBtn = QtWidgets.QPushButton("Reload")
        self.reloadBtn.setShortcut('Ctrl+r')
        self.reloadBtn.clicked.connect(self.load_file)
        self.toolbar_lay.addWidget(self.reloadBtn)
        
        self.loadTextChk = QtWidgets.QCheckBox("Load Textures")
        self.toolbar_lay.addWidget(self.loadTextChk)
        
        self.findBtn = QtWidgets.QPushButton("Find...")
        self.findBtn.setShortcut('Ctrl+f')
        self.findBtn.clicked.connect(self.findWindow)
        self.toolbar_lay.addWidget(self.findBtn)
        
        self.layoutBtn = QtWidgets.QPushButton("Layout Nodes")
        self.layoutBtn.clicked.connect(self.layout_nodes)
        self.toolbar_lay.addWidget(self.layoutBtn)
        
        toolbarspacer = QtWidgets.QSpacerItem(10, 10, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        self.toolbar_lay.addItem(toolbarspacer)
        
        logger.info('building nodes')
        configPath = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'nodz_config.json')
        
        self.nodz = nodz_main.Nodz(self, configPath=configPath)
        self.nodz.editLevel = 1
        # self.nodz.editEnabled = False
        lay.addWidget(self.nodz)
        self.nodz.initialize()
        self.nodz.fitInView(-500, -500, 500, 500)
        
        self.nodz.signal_NodeMoved.connect(on_nodeMoved)
        self.nodz.signal_NodeContextMenuEvent.connect(self.node_context_menu)
    
    
    def findWindow(self):
        if self.find_win:
            self.find_win.close()
        
        self.find_win = FindNodeWindow(self.nodz, parent=self)
        self.find_win.show()
        self.find_win.activateWindow()
    
    
    def get_node_from_name(self, node_name):
        return self.nodz.scene().nodes[node_name]
    
    
    def node_path(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        print path
    
    
    def node_upstream(self, node_name):
        start_node = self.get_node_from_name(node_name)
        connected_nodes = start_node.upstream_nodes()
        
        for node_name in self.nodz.scene().nodes:
            node = self.nodz.scene().nodes[node_name]
            if node in connected_nodes:
                node.setSelected(True)
            else:
                node.setSelected(False)
    
    
    def view_usdfile(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        layer = Sdf.Layer.FindOrOpen(path)
        
        win = text_view.TextViewer(input_text=layer.ExportToString(), parent=self)
        win.show()
    
    
    def view_usdview(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        worker = threading.Thread(target=launch_usdview, args=[path])
        worker.start()
        
        # subprocess.call(['usdview', path], shell=True)
        # os.system('usdview {}'.format(path))
    
    
    def node_context_menu(self, event, node):
        menu = QtWidgets.QMenu()
        menu.addAction("Print Node Path", partial(self.node_path, node))
        menu.addAction("Inspect layer...", partial(self.view_usdfile, node))
        menu.addAction("UsdView...", partial(self.view_usdview, node))
        menu.addAction("Select upstream", partial(self.node_upstream, node))
        
        menu.exec_(event.globalPos())
    
    
    def load_file(self):
        
        if not os.path.isfile(self.usdfile):
            raise RuntimeError("Cannot find file: %s" % self.usdfile)
        
        self.nodz.clearGraph()
        self.root_node = None
        self.setWindowTitle(self.usdfile)
        
        x = DependencyWalker(self.usdfile)
        x.walk_attributes = self.loadTextChk.isChecked()
        x.start()
        
        # get back the scrubbed initial file path
        # which will let us find the start node properly
        self.usdfile = x.usdfile
        
        nodz_scene = self.nodz.scene()
        
        # pprint(x.nodes)
        nds = []
        for i, node in enumerate(x.nodes):
            
            info = x.nodes[node]
            
            pos = QtCore.QPointF(0, 0)
            node_label = os.path.basename(node)
            
            # node colouring / etc based on the node type
            node_preset = 'node_default'
            node_icon = "sublayer.png"
            if info.get("type") == 'clip':
                node_preset = 'node_clip'
                node_icon = "clip.png"
            elif info.get("type") == 'payload':
                node_preset = 'node_payload'
                node_icon = "payload.png"
            elif info.get("type") == 'variant':
                node_preset = 'node_variant'
                node_icon = "variant.png"
            elif info.get("type") == 'specialize':
                node_preset = 'node_specialize'
                node_icon = "specialize.png"
            elif info.get("type") == 'reference':
                node_preset = 'node_reference'
                node_icon = "reference.png"

            if not node in nds:
                nodeA = self.nodz.createNode(name=node, label=node_label, preset=node_preset, position=pos)
                icon = QtGui.QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", node_icon))
                nodeA.icon = icon
                if self.usdfile == node:
                    self.root_node = nodeA
                
                if nodeA:
                    self.nodz.createAttribute(node=nodeA, name='out', index=0, preset='attr_preset_1',
                                              plug=True, socket=False, dataType=int, socketMaxConnections=-1)
                    
                    nodeA.userData = info
                    
                    if info['online'] is False:
                        self.nodz.createAttribute(node=nodeA, name='OFFLINE', index=0, preset='attr_preset_2',
                                                  plug=False, socket=False)
                
                nds.append(node)
        
        # pprint(x.edges)
        
        # print 'wiring nodes'.center(40, '-')
        # create all the node connections
        for edge in x.edges:
            
            start = edge[0]
            end = edge[1]
            port_type = edge[2]
            try:
                start_node = self.nodz.scene().nodes[start]
                self.nodz.createAttribute(node=start_node, name=port_type, index=-1, preset='attr_preset_1',
                                          plug=False, socket=True, dataType=int, socketMaxConnections=-1)
                # # sort the ports alphabetically
                # start_node.attrs = sorted(start_node.attrs)
                
                self.nodz.createConnection(end, 'out', start, port_type)
            except:
                print 'cannot find start node', start
        
        # layout nodes!
        self.nodz.arrangeGraph(self.root_node)
        # self.nodz.autoLayoutGraph()
        self.nodz._focus()
    
    
    def layout_nodes(self):
        # layout nodes!
        self.nodz.arrangeGraph(self.root_node)
        # self.nodz.autoLayoutGraph()
        
        self.nodz._focus(all=True)
    
    
    def manualOpen(self):
        """
        Manual open method for manually opening the manually opened files.
        """
        startPath = None
        if self.usdfile:
            startPath = os.path.dirname(self.usdfile)
        
        multipleFilters = "USD Files (*.usd *.usda *.usdc) (*.usd *.usda *.usdc);;All Files (*.*) (*.*)"
        filename = QtWidgets.QFileDialog.getOpenFileName(
            QtWidgets.QApplication.activeWindow(), 'Open File', startPath or '/', multipleFilters,
            None, QtWidgets.QFileDialog.DontUseNativeDialog)
        if filename[0]:
            print filename[0]
            self.usdfile = filename[0]
            self.load_file()
    
    
    def closeEvent(self, *args, **kwargs):
        """
        Window close event. Saves preferences. Impregnates your dog.
        """
        if self.find_win:
            self.find_win.close()
        
        self.settings.setValue("geometry", self.saveGeometry())
        super(NodeGraphWindow, self).closeEvent(*args)


def main(usdfile=None):
    # usdfile = utils.sanitize_path(usdfile)
    # usdfile = usdfile.encode('unicode_escape')
    
    par = QtWidgets.QApplication.activeWindow()
    win = NodeGraphWindow(usdfile=usdfile, parent=par)
    win.show()
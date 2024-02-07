# Copyright (C) 2024  Loren Eteval <loren.eteval@proton.me>
#
# This file is part of Furious.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from Furious.Interface import *
from Furious.PyFramework import *
from Furious.QtFramework import *
from Furious.QtFramework import gettext as _
from Furious.Utility import *
from Furious.Library import *
from Furious.Core import *
from Furious.TrayActions.Import import *
from Furious.Window.QRCodeWindow import *
from Furious.Window.TextEditorWindow import *

from PySide6 import QtCore
from PySide6.QtGui import *
from PySide6.QtWidgets import *
from PySide6.QtNetwork import *

from typing import Callable, Union, Sequence, MutableSequence

import ping3
import queue
import logging
import functools

__all__ = ['UserServersQTableWidget']

logger = logging.getLogger(__name__)

registerAppSettings('ActivatedItemIndex')
registerAppSettings('ServerWidgetSectionSizeTable')

needTrans = functools.partial(needTransFn, source=__name__)


class SubscriptionManager(AppQNetworkAccessManager):
    def __init__(self, parent):
        super().__init__(parent)

        self.networkReplyTable = {}

    def handleFinishedByNetworkReply(self, networkReply):
        assert isinstance(networkReply, QNetworkReply)

        unique, webURL = (
            self.networkReplyTable[networkReply]['unique'],
            self.networkReplyTable[networkReply]['webURL'],
        )

        if networkReply.error() != QNetworkReply.NetworkError.NoError:
            logger.error(f'update subs {webURL} failed. {networkReply.errorString()}')
        else:
            logger.info(f'update subs {webURL} success')

            try:
                data = networkReply.readAll().data()

                uris = list(
                    filter(
                        lambda x: x != '',
                        PyBase64Encoder.decode(data).decode().split('\n'),
                    )
                )
            except Exception as ex:
                # Any non-exit exceptions

                logger.error(f'parse share link failed: {ex}')
            else:
                parent = self.parent()

                if isinstance(parent, UserServersQTableWidget):
                    parent.deleteItemByIndex(
                        list(
                            index
                            for index, server in enumerate(AS_UserServers())
                            if server.getExtras('subsId') == unique
                        ),
                    )

                    for uri in uris:
                        parent.appendNewItem(config=uri, subsId=unique)

        # Done. Remove entry. Key should be found, but protect it anyway
        self.networkReplyTable.pop(networkReply, None)

    def configureHttpProxy(self, httpProxy: Union[str, None]) -> bool:
        useProxy = super().configureHttpProxy(httpProxy)

        if useProxy:
            logger.info(f'update subs uses proxy server {httpProxy}')
        else:
            logger.info(f'update subs uses no proxy')

        return useProxy

    def updateSubs(self, unique: str, webURL: str):
        networkReply = self.get(QNetworkRequest(QtCore.QUrl(webURL)))

        self.networkReplyTable[networkReply] = {
            'unique': unique,
            'webURL': webURL,
        }

        networkReply.finished.connect(
            functools.partial(
                self.handleFinishedByNetworkReply,
                networkReply,
            )
        )


class TestPingLatencyWorker(ItemUpdateProtocol, QtCore.QObject, QtCore.QRunnable):
    finished = QtCore.Signal(int, object)

    def __init__(self, sequence: Sequence, index: int, item: ConfigurationFactory):
        super().__init__(sequence, index, item)

        # Explictly called __init__
        QtCore.QObject.__init__(self)
        QtCore.QRunnable.__init__(self)

    def currentItemDeleted(self) -> bool:
        return super().currentItemDeleted() or FastItemDeletionSearch.isInTrash(
            self.currentItem
        )

    def updateImpl(self):
        self.finished.emit(self.currentIndex, self.currentItem)

    def updateResult(self):
        # Extra guard
        if APP() is None or APP().isExiting():
            return

        super().updateResult()

    def run(self):
        if self.currentItemDeleted():
            # Deleted. Do nothing
            return

        assert isinstance(self.currentItem, ConfigurationFactory)

        try:
            result = ping3.ping(self.currentItem.itemAddress, timeout=2, unit='ms')
        except Exception:
            # Any non-exit exceptions

            self.currentItem.setExtras('delayResult', 'Timeout')
            self.updateResult()
        else:
            if result is False:
                self.currentItem.setExtras('delayResult', 'Error')
            elif result is None:
                self.currentItem.setExtras('delayResult', 'Timeout')
            else:
                self.currentItem.setExtras('delayResult', f'{round(result)}ms')

            self.updateResult()


class TestDownloadSpeedWorker(ItemUpdateProtocol, QtCore.QObject):
    progressed = QtCore.Signal(int, object)

    def __init__(self, sequence: Sequence, index: int, item: ConfigurationFactory):
        super().__init__(sequence, index, item)

        # Explictly called __init__
        QtCore.QObject.__init__(self)

        self.hasSpeedResult = False
        self.totalBytesRead = 0

        self.coreManager = CoreManager()

        self.networkAccessManager = QNetworkAccessManager()
        self.networkReply = None
        self.elapsedTimer = QtCore.QElapsedTimer()

    def isFinished(self) -> bool:
        if isinstance(self.networkReply, QNetworkReply):
            return self.networkReply.isFinished()
        else:
            return True

    def abort(self):
        if isinstance(self.networkReply, QNetworkReply):
            self.networkReply.abort()

    def currentItemDeleted(self) -> bool:
        return super().currentItemDeleted() or FastItemDeletionSearch.isInTrash(
            self.currentItem
        )

    def updateImpl(self):
        self.progressed.emit(self.currentIndex, self.currentItem)

    def updateResult(self):
        # Extra guard
        if APP() is None or APP().isExiting():
            return

        super().updateResult()

    def run(self):
        if self.currentItemDeleted():
            # Deleted. Do nothing
            return

        assert isinstance(self.currentItem, ConfigurationFactory)

        if not self.currentItem.isValid():
            self.currentItem.setExtras('speedResult', 'Invalid')
            self.updateResult()

            return

        def coreExitCallback(config: ConfigurationFactory, exitcode: int):
            if exitcode == CoreProcess.ExitCode.ConfigurationError:
                self.currentItem.setExtras('speedResult', 'Invalid')

                return self.updateResult()
            if exitcode == CoreProcess.ExitCode.ServerStartFailure:
                self.currentItem.setExtras('speedResult', 'Start failed')

                return self.updateResult()
            if exitcode == CoreProcess.ExitCode.SystemShuttingDown:
                pass
            else:
                self.currentItem.setExtras('speedResult', f'Core exited {exitcode}')

                return self.updateResult()

        def msgCallback(line: str):
            try:
                APP().logViewerWindowCore.appendLine(line)
            except Exception:
                # Any non-exit exceptions

                pass

        self.currentItem.setExtras('speedResult', 'Starting')
        self.updateResult()

        copy = self.currentItem.deepcopy()

        if isinstance(copy, ConfigurationXray):
            # Force redirect
            copy['inbounds'] = [
                {
                    'tag': 'http',
                    'port': 20809,
                    'listen': '127.0.0.1',
                    'protocol': 'http',
                    'sniffing': {
                        'enabled': True,
                        'destOverride': [
                            'http',
                            'tls',
                        ],
                    },
                    'settings': {
                        'auth': 'noauth',
                        'udp': True,
                        'allowTransparent': False,
                    },
                },
            ]

            try:
                for outboundObject in copy['outbounds']:
                    if outboundObject['tag'] == 'proxy':
                        # Avoid confusion with potentially existing 'proxy' tag
                        outboundObject['tag'] = 'proxy20809'
            except Exception:
                # Any non-exit exceptions

                pass

            self.coreManager.start(
                copy,
                'Global',
                coreExitCallback,
                msgCallback=msgCallback,
                deepcopy=False,
                proxyModeOnly=True,
                log=False,
            )
        elif isinstance(copy, ConfigurationHysteria1) or isinstance(
            copy, ConfigurationHysteria2
        ):
            # Force redirect
            copy['http'] = {
                'listen': '127.0.0.1:20809',
                'timeout': 300,
                'disable_udp': False,
            }

            # No socks inbounds
            copy.pop('socks5', '')

            self.coreManager.start(
                copy,
                'Global',
                coreExitCallback,
                msgCallback=msgCallback,
                deepcopy=False,
                proxyModeOnly=True,
                log=False,
            )
        else:
            self.currentItem.setExtras('speedResult', 'Invalid')
            self.updateResult()

            return

        self.networkAccessManager.setProxy(
            QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, '127.0.0.1', 20809)
        )
        self.networkReply = self.networkAccessManager.get(
            QNetworkRequest(
                QtCore.QUrl(
                    'http://speed.cloudflare.com/__down?during=download&bytes=104857600'
                )
            )
        )
        self.networkReply.readyRead.connect(self.handleReadyRead)
        self.networkReply.finished.connect(self.handleFinished)
        self.elapsedTimer.start()

    @QtCore.Slot()
    def handleReadyRead(self):
        if self.coreManager.allRunning():
            self.totalBytesRead += self.networkReply.readAll().length()

            # Convert to seconds
            elapsedSecond = self.elapsedTimer.elapsed() / 1000
            downloadSpeed = self.totalBytesRead / elapsedSecond / 1024 / 1024

            # Has speed test result
            self.hasSpeedResult = True
            self.currentItem.setExtras('speedResult', f'{downloadSpeed:.2f} M/s')
            self.updateResult()

    @QtCore.Slot()
    def handleFinished(self):
        if self.networkReply.error() != QNetworkReply.NetworkError.NoError:
            if not self.hasSpeedResult:
                if not self.coreManager.allRunning():
                    # Core ExitCallback has been called
                    return

                if (
                    self.networkReply.error()
                    == QNetworkReply.NetworkError.OperationCanceledError
                ):
                    # Canceled by application
                    self.currentItem.setExtras('speedResult', 'Canceled')
                else:
                    try:
                        error = self.networkReply.error().name
                    except Exception:
                        # Any non-exit exceptions

                        error = 'Unknown Error'

                    if isinstance(error, bytes):
                        # Some old version PySide6 returns it as bytes. Protect it.
                        errorString = error.decode('utf-8', 'replace')
                    elif isinstance(error, str):
                        errorString = error
                    else:
                        errorString = 'Unknown Error'

                    if errorString.endswith('Error'):
                        self.currentItem.setExtras('speedResult', errorString[:-5])
                    else:
                        self.currentItem.setExtras('speedResult', errorString)
        else:
            if self.coreManager.allRunning():
                self.totalBytesRead += self.networkReply.readAll().length()

                # Convert to seconds
                elapsedSecond = self.elapsedTimer.elapsed() / 1000
                downloadSpeed = self.totalBytesRead / elapsedSecond / 1024 / 1024

                self.currentItem.setExtras('speedResult', f'{downloadSpeed:.2f} M/s')
            else:
                self.currentItem.setExtras('speedResult', 'Start failed')

        self.coreManager.stopAll()
        self.updateResult()


class UserServersQTableWidgetHorizontalHeader(AppQHeaderView):
    class SortOrder:
        Ascending_ = False
        Descending = True

    @staticmethod
    def emptyClickGuard():
        return False

    def __init__(self, *args, **kwargs):
        self.clickGuardFn = kwargs.pop('clickGuardFn', self.emptyClickGuard)
        self.customSortFn = kwargs.pop('customSortFn', None)

        super().__init__(QtCore.Qt.Orientation.Horizontal, *args, **kwargs)

        self.sortOrderTable = list(
            self.SortOrder.Ascending_ for i in range(self.columnCount)
        )

        self.sectionClicked.connect(self.handleSectionClicked)

    def customSort(self, clickedIndex):
        self.customSortFn(clickedIndex, reverse=self.sortOrderTable[clickedIndex])
        # Toggle
        self.sortOrderTable[clickedIndex] = not self.sortOrderTable[clickedIndex]

    @QtCore.Slot(int)
    def handleSectionClicked(self, clickedIndex: int):
        if callable(self.clickGuardFn) and self.clickGuardFn():
            # Guarded. Do nothing
            return

        if self.customSortFn is None:
            # Sorting not supported
            return

        parent = self.parent()

        if isinstance(parent, AppQTableWidget):
            # Support item activation
            activatedIndex = AS_UserActivatedItemIndex()

            if activatedIndex < 0:
                activatedServerId = None
            else:
                activatedServerId = id(AS_UserServers()[activatedIndex])

                # De-activated temporarily for sorting
                parent.activateItemByIndex(activatedIndex, False)

            self.customSort(clickedIndex)

            if activatedServerId is not None:
                foundActivatedItem = False

                for index, server in enumerate(AS_UserServers()):
                    if activatedServerId == id(server):
                        foundActivatedItem = True

                        parent.setCurrentItem(parent.item(index, 0))
                        parent.activateItemByIndex(index, True)

                        break

                # Object id should be found
                assert foundActivatedItem

        else:
            # Not yet implemented
            pass


class UserServersQTableWidgetVerticalHeader(AppQHeaderView):
    def __init__(self, *args, **kwargs):
        super().__init__(QtCore.Qt.Orientation.Vertical, *args, **kwargs)


class UserServersQTableWidgetHeaders:
    def __init__(self, name: str, func: Callable[[ConfigurationFactory], str] = None):
        self.name = name
        self.func = func

    def __call__(self, item: ConfigurationFactory) -> str:
        if callable(self.func):
            return self.func(item)
        else:
            return getattr(item, f'item{self}')

    def __eq__(self, other):
        return str(self) == str(other)

    def __str__(self):
        return self.name


needTrans(
    'Remark',
    'Protocol',
    'Address',
    'Port',
    'Transport',
    'TLS',
    'Subscription',
    'Latency',
    'Speed',
    'Edit Configuration...',
    'Move Up',
    'Move Down',
    'Duplicate',
    'Delete',
    'Select All',
    'Scroll To Activated Server',
    'Test Ping Latency',
    'Test Download Speed',
    'Clear Test Results',
    'New Empty Configuration',
    'Export Share Link To Clipboard',
    'Export As QR Code',
    'Export JSON Configuration To Clipboard',
    'Connecting',
    'Connecting. Please wait',
    'Untitled',
)


class UserServersQTableWidget(QTranslatable, AppQTableWidget):
    Headers = [
        UserServersQTableWidgetHeaders('Remark'),
        UserServersQTableWidgetHeaders('Protocol'),
        UserServersQTableWidgetHeaders('Address'),
        UserServersQTableWidgetHeaders('Port'),
        UserServersQTableWidgetHeaders('Transport'),
        UserServersQTableWidgetHeaders('TLS'),
        UserServersQTableWidgetHeaders('Subscription'),
        UserServersQTableWidgetHeaders('Latency'),
        UserServersQTableWidgetHeaders('Speed'),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.subsManager = SubscriptionManager(parent=self)

        self.testDownloadSpeedQueue = queue.Queue()
        self.testDownloadSpeedTimer = QtCore.QTimer()
        self.testDownloadSpeedTimer.timeout.connect(self.handleTestDownloadSpeedJob)
        self.testDownloadSpeedTimer.start(250)

        # Text Editor Window
        self.textEditorWindow = TextEditorWindow()

        # Delegate
        self._delegate = AppQStyledItemDelegate(parent=self)
        self.setItemDelegate(self._delegate)

        # Must set before flush all
        self.setColumnCount(len(self.Headers))

        # Flush all data to table
        self.flushAll()

        # Install custom header
        self.setHorizontalHeader(
            UserServersQTableWidgetHorizontalHeader(
                parent=self,
                clickGuardFn=lambda: False,
                customSortFn=self.customSortFn,
                sectionSizeSettingsName='ServerWidgetSectionSizeTable',
            )
        )
        self.setVerticalHeader(UserServersQTableWidgetVerticalHeader(self))

        self.horizontalHeader().setCustomSectionResizeMode()
        self.horizontalHeader().restoreSectionSize()

        self.setHorizontalHeaderLabels(list(_(str(header)) for header in self.Headers))

        # Selection
        self.setSelectionColor(AppHue.disconnectedColor())
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)

        # No drag and drop
        self.setDragEnabled(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(QtCore.Qt.DropAction.IgnoreAction)

        self.editConfigActionRef = AppQAction(
            _('Edit Configuration...'),
            icon=bootstrapIcon('pencil-square.svg'),
            callback=lambda: self.editSelectedItemConfiguration(),
            shortcut=QtCore.QKeyCombination(
                QtCore.Qt.KeyboardModifier.ControlModifier,
                QtCore.Qt.Key.Key_E,
            ),
        )

        contextMenuActions = [
            AppQAction(
                _('Move Up'),
                callback=lambda: self.moveUpSelectedItem(),
            ),
            AppQAction(
                _('Move Down'),
                callback=lambda: self.moveDownSelectedItem(),
            ),
            AppQAction(
                _('Duplicate'),
                callback=lambda: self.duplicateSelectedItem(),
            ),
            AppQAction(
                _('Delete'),
                callback=lambda: self.deleteSelectedItem(),
            ),
            AppQSeperator(),
            self.editConfigActionRef,
            AppQSeperator(),
            AppQAction(
                _('Select All'),
                callback=lambda: self.selectAll(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_A,
                ),
            ),
            AppQSeperator(),
            AppQAction(
                _('Scroll To Activated Server'),
                callback=lambda: self.scrollToActivatedItem(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_G,
                ),
            ),
            AppQSeperator(),
            AppQAction(
                _('Test Ping Latency'),
                callback=lambda: self.testSelectedItemPingLatency(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_P,
                ),
            ),
            AppQAction(
                _('Test Download Speed'),
                callback=lambda: self.testSelectedItemDownloadSpeed(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_T,
                ),
            ),
            AppQAction(
                _('Clear Test Results'),
                callback=lambda: self.clearSelectedItemTestResult(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_R,
                ),
            ),
            AppQSeperator(),
            AppQAction(
                _('New Empty Configuration'),
                callback=lambda: self.newEmptyItem(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_N,
                ),
            ),
            ImportFromFileAction(),
            ImportURIFromClipboardAction(
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_V,
                ),
            ),
            ImportJSONFromClipboardAction(
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier
                    | QtCore.Qt.KeyboardModifier.ShiftModifier,
                    QtCore.Qt.Key.Key_J,
                ),
            ),
            AppQSeperator(),
            AppQAction(
                _('Export Share Link To Clipboard'),
                callback=lambda: self.exportSelectedItemURI(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_C,
                ),
            ),
            AppQAction(
                _('Export As QR Code'),
                icon=bootstrapIcon('qr-code.svg'),
                callback=lambda: self.exportSelectedItemQR(),
            ),
            AppQAction(
                _('Export JSON Configuration To Clipboard'),
                callback=lambda: self.exportSelectedItemJSON(),
                shortcut=QtCore.QKeyCombination(
                    QtCore.Qt.KeyboardModifier.ControlModifier,
                    QtCore.Qt.Key.Key_J,
                ),
            ),
        ]

        self.contextMenu = AppQMenu(*contextMenuActions)

        # Add actions to self in order to activate shortcuts
        self.addActions(self.contextMenu.actions())
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.handleCustomContextMenuRequested)

        # Signals
        self.itemChanged.connect(self.handleItemChanged)
        self.itemSelectionChanged.connect(self.handleItemSelectionChanged)
        self.itemActivated.connect(self.handleItemActivated)
        self.itemDoubleClicked.connect(self.handleItemDoubleClicked)

        if self.activatedItem() is not None:
            self.setCurrentItem(self.activatedItem())
            self.activateItemByIndex(AS_UserActivatedItemIndex(), True)

    @QtCore.Slot(QTableWidgetItem)
    def handleItemChanged(self, item: QTableWidgetItem):
        index = item.row()

        if self.item(index, 0) is None:
            return

        itemText = self.item(index, 0).text()

        if AS_UserServers()[index].getExtras('remark') != itemText:
            AS_UserServers()[index].setExtras('remark', itemText)

    @QtCore.Slot()
    def handleItemSelectionChanged(self):
        if len(self.selectedIndex) > 1:
            self.editConfigActionRef.setDisabled(True)
        else:
            self.editConfigActionRef.setDisabled(False)

    @QtCore.Slot(QTableWidgetItem)
    def handleItemActivated(self, item: QTableWidgetItem):
        oldIndex = AS_UserActivatedItemIndex()
        newIndex = item.row()

        if oldIndex == newIndex:
            # Same item activated. Do nothing
            return

        if APP().systemTray.ConnectAction.isConnecting():
            mbox = AppQMessageBox(icon=AppQMessageBox.Icon.Information)
            mbox.setWindowTitle(_('Connecting'))
            mbox.setText(_('Connecting. Please wait'))

            if PLATFORM != 'Darwin':
                # Show the MessageBox asynchronously
                mbox.open()
            else:
                # Show the MessageBox and wait for user to close it
                mbox.exec()

            return

        if oldIndex >= 0:
            self.activateItemByIndex(oldIndex, False)

        self.activateItemByIndex(newIndex, True)

        if APP().isSystemTrayConnected():
            APP().systemTray.ConnectAction.doDisconnect()
            APP().systemTray.ConnectAction.trigger()

    @QtCore.Slot(QTableWidgetItem)
    def handleItemDoubleClicked(self, item: QTableWidgetItem):
        pass

    @QtCore.Slot(QtCore.QPoint)
    def handleCustomContextMenuRequested(self, point):
        self.contextMenu.exec(self.mapToGlobal(point))

    def customSortFn(self, clickedIndex, **kwargs):
        AS_UserServers().sort(
            key=lambda server: self.Headers[clickedIndex](server), **kwargs
        )

        self.flushAll()

    def activatedItem(self) -> QTableWidgetItem:
        return self.item(AS_UserActivatedItemIndex(), 0)

    def activateItemByIndex(self, index, activate):
        super().activateItemByIndex(index, activate)

        if activate:
            AppSettings.set('ActivatedItemIndex', str(index))

    def flushItem(self, row: int, column: int, item: ConfigurationFactory):
        header = self.Headers[column]

        oldItem = self.item(row, column)
        newItem = QTableWidgetItem(header(item))

        if oldItem is None:
            # Item does not exists
            newItem.setFont(QFont(APP().customFontName))

            if str(header) == 'Latency' or str(header) == 'Speed':
                # Test results. Align right and vcenter
                newItem.setTextAlignment(
                    QtCore.Qt.AlignmentFlag.AlignRight
                    | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
        else:
            # Use existing
            newItem.setFont(oldItem.font())
            newItem.setForeground(oldItem.foreground())

            if oldItem.textAlignment() != 0:
                newItem.setTextAlignment(oldItem.textAlignment())

        if str(header) == 'Remark':
            # Remark is editable
            newItem.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled
                | QtCore.Qt.ItemFlag.ItemIsSelectable
                | QtCore.Qt.ItemFlag.ItemIsEditable
            )
        else:
            newItem.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
            )

        self.setItem(row, column, newItem)

    def flushRow(self, row: int, item: ConfigurationFactory):
        for column in list(range(self.columnCount())):
            self.flushItem(row, column, item)

    def flushAll(self):
        if self.rowCount() == 0:
            # Should insert row
            for index, item in enumerate(AS_UserServers()):
                self.insertRow(index)
                self.flushRow(index, item)
        else:
            for index, item in enumerate(AS_UserServers()):
                self.flushRow(index, item)

    def swapItem(self, index0: int, index1: int):
        def swapSequenceItem(sequence: MutableSequence, param0: int, param1: int):
            swap = sequence[param0]

            sequence[param0] = sequence[param1]
            sequence[param1] = swap

        swapSequenceItem(AS_UserServers(), index0, index1)

        self.flushRow(index0, AS_UserServers()[index0])
        self.flushRow(index1, AS_UserServers()[index1])

        if index0 == AS_UserActivatedItemIndex():
            # De-activate
            self.activateItemByIndex(index0, False)
            # Activate
            self.activateItemByIndex(index1, True)
        elif index1 == AS_UserActivatedItemIndex():
            # De-activate
            self.activateItemByIndex(index1, False)
            # Activate
            self.activateItemByIndex(index0, True)

    def newEmptyItem(self):
        self.appendNewItem(remark=_('Untitled'), acceptInvalid=True)

    def moveUpItemByIndex(self, index):
        if index <= 0 or index >= len(AS_UserServers()):
            # The top item, or does not exist. Do nothing
            return

        self.swapItem(index, index - 1)

    def moveUpSelectedItem(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        for index in indexes:
            self.moveUpItemByIndex(index)

        with QBlockSignals(self):
            self.setCurrentIndex(self.indexFromItem(self.item(indexes[-1] - 1, 0)))

        self.selectMultipleRows(list(index - 1 for index in indexes), True)

    def moveDownItemByIndex(self, index):
        if index < 0 or index >= len(AS_UserServers()) - 1:
            # The bottom item, or does not exist. Do nothing
            return

        self.swapItem(index, index + 1)

    def moveDownSelectedItem(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        for index in indexes[::-1]:
            self.moveDownItemByIndex(index)

        with QBlockSignals(self):
            self.setCurrentIndex(self.indexFromItem(self.item(indexes[0] + 1, 0)))

        self.selectMultipleRows(list(index + 1 for index in indexes), True)

    def duplicateSelectedItem(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        for index in indexes:
            if 0 <= index < len(AS_UserServers()):
                # Do not clone subsId
                self.appendNewItem(
                    remark=AS_UserServers()[index].getExtras('remark'),
                    config=AS_UserServers()[index],
                )

    def deleteItemByIndex(self, indexes) -> int:
        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return 0

        if AS_UserActivatedItemIndex() in indexes:
            deleteActivated = True
        else:
            deleteActivated = False

        for i in range(len(indexes)):
            deleteIndex = indexes[i] - i

            with QBlockSignals(self):
                self.removeRow(deleteIndex)

            FastItemDeletionSearch.moveToTrash(AS_UserServers()[deleteIndex])

            AS_UserServers().pop(deleteIndex)

            if not deleteActivated and deleteIndex < AS_UserActivatedItemIndex():
                AppSettings.set(
                    'ActivatedItemIndex', str(AS_UserActivatedItemIndex() - 1)
                )

        if deleteActivated:
            # Set invalid first
            AppSettings.set('ActivatedItemIndex', str(-1))

            if APP().isSystemTrayConnected():
                # Trigger disconnect
                APP().systemTray.ConnectAction.trigger()

        return len(indexes)

    def deleteSelectedItem(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        mbox = QuestionDeleteMBox(icon=AppQMessageBox.Icon.Question)
        mbox.isMulti = bool(len(indexes) > 1)
        mbox.possibleRemark = f'{indexes[0] + 1} - {self.item(indexes[0], 0).text()}'
        mbox.setText(mbox.customText())

        if mbox.exec() == PySide6LegacyEnumValueWrapper(
            AppQMessageBox.StandardButton.No
        ):
            # Do not delete
            return

        self.deleteItemByIndex(indexes)

    def editSelectedItemConfiguration(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        if len(indexes) != 1:
            # Should not reach here
            return

        index = indexes[0]
        title = f'{index + 1} - ' + AS_UserServers()[index].getExtras('remark')

        self.textEditorWindow.currentIndex = index
        self.textEditorWindow.customWindowTitle = title
        self.textEditorWindow.setWindowTitle(title)
        self.textEditorWindow.setPlainText(AS_UserServers()[index].toJSONString(), True)
        self.textEditorWindow.show()

    def scrollToActivatedItem(self):
        activatedItem = self.activatedItem()

        self.setCurrentItem(activatedItem)
        self.scrollToItem(activatedItem)

    def testSelectedItemPingLatency(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        # Real selected factory
        references = list(AS_UserServers()[index] for index in indexes)

        for index, reference in zip(indexes, references):
            worker = TestPingLatencyWorker(AS_UserServers(), index, reference)

            worker.setAutoDelete(True)
            worker.finished.connect(
                lambda paramIndex, paramFactory: self.flushItem(
                    paramIndex, self.Headers.index('Latency'), paramFactory
                )
            )

            QtCore.QThreadPool.globalInstance().start(worker)

    @QtCore.Slot()
    def handleTestDownloadSpeedJob(self):
        try:
            index, server = self.testDownloadSpeedQueue.get_nowait()
        except queue.Empty:
            # Queue is empty

            return

        def testDownloadSpeed(counter=0, timeout=5000, step=100):
            worker = TestDownloadSpeedWorker(AS_UserServers(), index, server)
            worker.progressed.connect(
                lambda paramIndex, paramFactory: self.flushItem(
                    paramIndex, self.Headers.index('Speed'), paramFactory
                )
            )
            worker.run()

            while (
                not APP().isExiting() and not worker.isFinished() and counter < timeout
            ):
                PySide6LegacyEventLoopWait(step)

                counter += step

            if not worker.isFinished():
                worker.abort()

                while not worker.isFinished():
                    PySide6LegacyEventLoopWait(step)

            if APP().isExiting():
                # Stop timer
                self.testDownloadSpeedTimer.stop()

        testDownloadSpeed()

    def testSelectedItemDownloadSpeed(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        # Real selected factory
        references = list(AS_UserServers()[index] for index in indexes)

        for index, reference in zip(indexes, references):
            try:
                # Served by FIFO
                self.testDownloadSpeedQueue.put_nowait((index, reference))
            except Exception:
                # Any non-exit exceptions

                pass

    def clearSelectedItemTestResult(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        for index in indexes:
            factory = AS_UserServers()[index]
            factory.setExtras('delayResult', '')
            factory.setExtras('speedResult', '')

            self.flushItem(index, self.Headers.index('Latency'), factory)
            self.flushItem(index, self.Headers.index('Speed'), factory)

    def updateSubs(self, httpProxy: Union[str, None]):
        self.subsManager.configureHttpProxy(httpProxy)

        for key, value in AS_UserSubscription().items():
            self.subsManager.updateSubs(key, value.get('webURL', ''))

    def appendNewItemByFactory(self, factory: ConfigurationFactory):
        AS_UserServers().append(factory)

        row = self.rowCount()

        self.insertRow(row)
        self.flushRow(row, factory)

        if len(AS_UserServers()) == 1:
            # The first one. Click it
            self.setCurrentItem(self.item(0, 0))

            # Try to be user-friendly in some extreme cases
            if not APP().isSystemTrayConnected():
                # Activate automatically
                self.activateItemByIndex(0, True)

    def appendNewItem(self, **kwargs):
        acceptInvalid = kwargs.pop('acceptInvalid', False)

        model = {
            'remark': kwargs.pop('remark', ''),
            'config': kwargs.pop('config', ''),
            'subsId': kwargs.pop('subsId', ''),
        }
        tostr = f'{model}'

        factory = constructFromAny(model.pop('config', ''), **model)

        if factory.isValid():
            self.appendNewItemByFactory(factory)
        else:
            if acceptInvalid:
                self.appendNewItemByFactory(factory)
            else:
                logger.error(f'invalid item: {tostr}')

    def exportSelectedItemURI(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        # TODO: MessageBox?
        QApplication.clipboard().setText(
            '\n'.join(list(AS_UserServers()[index].toURI() for index in indexes))
        )

    def exportSelectedItemQR(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        window = QRCodeWindow()
        window.initTabByIndex(indexes)

        if window.tabCount() > 0:
            window.show()

    def exportSelectedItemJSON(self):
        indexes = self.selectedIndex

        if len(indexes) == 0:
            # Nothing selected. Do nothing
            return

        # TODO: MessageBox?
        QApplication.clipboard().setText(
            '\n'.join(list(AS_UserServers()[index].toJSONString() for index in indexes))
        )

    def showTabAndSpaces(self):
        self.textEditorWindow.showTabAndSpaces()

    def hideTabAndSpaces(self):
        self.textEditorWindow.hideTabAndSpaces()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.deleteSelectedItem()
        else:
            super().keyPressEvent(event)

    def disconnectedCallback(self):
        super().disconnectedCallback()

        self.activateItemByIndex(AS_UserActivatedItemIndex(), True)

    def connectedCallback(self):
        super().connectedCallback()

        self.activateItemByIndex(AS_UserActivatedItemIndex(), True)

    def retranslate(self):
        self.setHorizontalHeaderLabels(list(_(str(header)) for header in self.Headers))
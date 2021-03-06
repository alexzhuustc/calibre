#!/usr/bin/env python
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'

import time, textwrap, json
from bisect import bisect_right
from base64 import b64encode
from future_builtins import map
from threading import Thread
from Queue import Queue, Empty
from collections import namedtuple
from functools import partial
from urlparse import urlparse

from PyQt4.Qt import (
    QWidget, QVBoxLayout, QApplication, QSize, QNetworkAccessManager, QMenu, QIcon,
    QNetworkReply, QTimer, QNetworkRequest, QUrl, Qt, QNetworkDiskCache, QToolBar,
    pyqtSlot, pyqtSignal, QFontDatabase)
from PyQt4.QtWebKit import QWebView, QWebInspector, QWebPage

from calibre import prints
from calibre.constants import iswindows
from calibre.ebooks.oeb.polish.container import OEB_FONTS
from calibre.ebooks.oeb.polish.parsing import parse
from calibre.ebooks.oeb.base import serialize, OEB_DOCS
from calibre.ptempfile import PersistentTemporaryDirectory
from calibre.gui2 import error_dialog
from calibre.gui2.tweak_book import current_container, editors, tprefs, actions, TOP
from calibre.gui2.viewer.documentview import apply_settings
from calibre.gui2.viewer.config import config
from calibre.gui2.widgets2 import HistoryLineEdit2
from calibre.utils.ipc.simple_worker import offload_worker

shutdown = object()

def get_data(name):
    'Get the data for name. Returns a unicode string if name is a text document/stylesheet'
    if name in editors:
        return editors[name].get_raw_data()
    return current_container().raw_data(name)

class FontCache(object):

    def __init__(self):
        self.cache = {}
        self.entry = namedtuple('Entry', 'size hash families')

    def remove_fonts(self):
        for font_id in self.cache:
            QFontDatabase.removeApplicationFont(font_id)
        self.cache.clear()

    def add_font(self, data):
        existing = None
        for font_id, entry in self.cache.iteritems():
            if entry.size == len(data) and entry.hash == hash(data):
                existing = entry
                break
        if existing is None:
            font_id = QFontDatabase.addApplicationFontFromData(data)
            if font_id > -1:
                families = frozenset(map(lambda x:icu_lower(unicode(x)), QFontDatabase.applicationFontFamilies(font_id)))
                self.cache[font_id] = self.entry(len(data), hash(data), families)

font_cache = FontCache()

# Parsing of html to add linenumbers {{{
def parse_html(raw):
    root = parse(raw, decoder=lambda x:x.decode('utf-8'), line_numbers=True, linenumber_attribute='data-lnum')
    return serialize(root, 'text/html').encode('utf-8')

class ParseItem(object):

    __slots__ = ('name', 'length', 'fingerprint', 'parsed_data')

    def __init__(self, name):
        self.name = name
        self.length, self.fingerprint = 0, None
        self.parsed_data = None

class ParseWorker(Thread):

    daemon = True
    SLEEP_TIME = 1

    def __init__(self):
        Thread.__init__(self)
        self.requests = Queue()
        self.request_count = 0
        self.parse_items = {}

    def run(self):
        mod, func = 'calibre.gui2.tweak_book.preview', 'parse_html'
        try:
            # Connect to the worker and send a dummy job to initialize it
            self.worker = offload_worker(priority='low')
            self.worker(mod, func, '<p></p>')
        except:
            import traceback
            traceback.print_exc()
            return

        while True:
            time.sleep(self.SLEEP_TIME)
            x = self.requests.get()
            requests = [x]
            while True:
                try:
                    requests.append(self.requests.get_nowait())
                except Empty:
                    break
            if shutdown in requests:
                self.worker.shutdown()
                break
            request = sorted(requests, reverse=True)[0]
            del requests
            pi, data = request[1:]
            try:
                res = self.worker(mod, func, data)
            except:
                import traceback
                traceback.print_exc()
            else:
                parsed_data = res['result']
                if res['tb']:
                    prints("Parser error:")
                    prints(res['tb'])
                else:
                    pi.parsed_data = parsed_data

    def add_request(self, name):
        data = get_data(name)
        ldata, hdata = len(data), hash(data)
        pi = self.parse_items.get(name, None)
        if pi is None:
            self.parse_items[name] = pi = ParseItem(name)
        else:
            if pi.length == ldata and pi.fingerprint == hdata:
                return
            pi.parsed_data = None
        pi.length, pi.fingerprint = ldata, hdata
        self.requests.put((self.request_count, pi, data))
        self.request_count += 1

    def shutdown(self):
        self.requests.put(shutdown)

    def get_data(self, name):
        return getattr(self.parse_items.get(name, None), 'parsed_data', None)

    def clear(self):
        self.parse_items.clear()

    def is_alive(self):
        return Thread.is_alive(self) or self.worker.is_alive()

parse_worker = ParseWorker()
# }}}

# Override network access to load data "live" from the editors {{{
class NetworkReply(QNetworkReply):

    def __init__(self, parent, request, mime_type, name):
        QNetworkReply.__init__(self, parent)
        self.setOpenMode(QNetworkReply.ReadOnly | QNetworkReply.Unbuffered)
        self.setRequest(request)
        self.setUrl(request.url())
        self._aborted = False
        if mime_type in OEB_DOCS:
            self.resource_name = name
            QTimer.singleShot(0, self.check_for_parse)
        else:
            data = get_data(name)
            if isinstance(data, type('')):
                data = data.encode('utf-8')
                mime_type += '; charset=utf-8'
            self.__data = data
            self.setHeader(QNetworkRequest.ContentTypeHeader, mime_type)
            self.setHeader(QNetworkRequest.ContentLengthHeader, len(self.__data))
            QTimer.singleShot(0, self.finalize_reply)
            if mime_type in OEB_FONTS:
                font_cache.add_font(data)
                # We prevent the use of the embedded font because of the the
                # bug in Qt WebKit,
                # https://bugs.webkit.org/show_bug.cgi?id=29433
                self.__data = b''

    def check_for_parse(self):
        if self._aborted:
            return
        data = parse_worker.get_data(self.resource_name)
        if data is None:
            return QTimer.singleShot(10, self.check_for_parse)
        self.__data = data
        self.setHeader(QNetworkRequest.ContentTypeHeader, 'text/html; charset=utf-8')
        self.setHeader(QNetworkRequest.ContentLengthHeader, len(self.__data))
        self.finalize_reply()

    def bytesAvailable(self):
        try:
            return len(self.__data)
        except AttributeError:
            return 0

    def isSequential(self):
        return True

    def abort(self):
        self._aborted = True

    def readData(self, maxlen):
        ans, self.__data = self.__data[:maxlen], self.__data[maxlen:]
        return ans
    read = readData

    def finalize_reply(self):
        if self._aborted:
            return
        self.setFinished(True)
        self.setAttribute(QNetworkRequest.HttpStatusCodeAttribute, 200)
        self.setAttribute(QNetworkRequest.HttpReasonPhraseAttribute, "Ok")
        self.metaDataChanged.emit()
        self.downloadProgress.emit(len(self.__data), len(self.__data))
        self.readyRead.emit()
        self.finished.emit()


class NetworkAccessManager(QNetworkAccessManager):

    OPERATION_NAMES = {getattr(QNetworkAccessManager, '%sOperation'%x) :
            x.upper() for x in ('Head', 'Get', 'Put', 'Post', 'Delete',
                'Custom')
    }

    def __init__(self, *args):
        QNetworkAccessManager.__init__(self, *args)
        self.cache = QNetworkDiskCache(self)
        self.setCache(self.cache)
        self.cache.setCacheDirectory(PersistentTemporaryDirectory(prefix='disk_cache_'))
        self.cache.setMaximumCacheSize(0)

    def createRequest(self, operation, request, data):
        url = unicode(request.url().toString())
        if operation == self.GetOperation and url.startswith('file://'):
            path = url[7:]
            if iswindows and path.startswith('/'):
                path = path[1:]
            c = current_container()
            try:
                name = c.abspath_to_name(path)
            except ValueError:  # Happens on windows with absolute paths on different drives
                name = None
            if c.has_name(name):
                try:
                    return NetworkReply(self, request, c.mime_map.get(name, 'application/octet-stream'), name)
                except Exception:
                    import traceback
                    traceback.print_exc()
        return QNetworkAccessManager.createRequest(self, operation, request, data)

# }}}

def uniq(vals):
    ''' Remove all duplicates from vals, while preserving order.  '''
    vals = vals or ()
    seen = set()
    seen_add = seen.add
    return tuple(x for x in vals if x not in seen and not seen_add(x))

def find_le(a, x):
    'Find rightmost value in a less than or equal to x'
    try:
        return a[bisect_right(a, x)]
    except IndexError:
        return a[-1]

class WebPage(QWebPage):

    sync_requested = pyqtSignal(object, object, object)
    split_requested = pyqtSignal(object, object)

    def __init__(self, parent):
        QWebPage.__init__(self, parent)
        settings = self.settings()
        apply_settings(settings, config().parse())
        settings.setMaximumPagesInCache(0)
        settings.setAttribute(settings.JavaEnabled, False)
        settings.setAttribute(settings.PluginsEnabled, False)
        settings.setAttribute(settings.PrivateBrowsingEnabled, True)
        settings.setAttribute(settings.JavascriptCanOpenWindows, False)
        settings.setAttribute(settings.JavascriptCanAccessClipboard, False)
        settings.setAttribute(settings.LinksIncludedInFocusChain, False)
        settings.setAttribute(settings.DeveloperExtrasEnabled, True)
        settings.setDefaultTextEncoding('utf-8')
        data = 'data:text/css;charset=utf-8;base64,'
        css = '[data-in-split-mode="1"] [data-is-block="1"]:hover { cursor: pointer !important; border-top: solid 5px green !important }'
        data += b64encode(css.encode('utf-8'))
        settings.setUserStyleSheetUrl(QUrl(data))

        self.setNetworkAccessManager(NetworkAccessManager(self))
        self.setLinkDelegationPolicy(self.DelegateAllLinks)
        self.mainFrame().javaScriptWindowObjectCleared.connect(self.init_javascript)
        self.init_javascript()

    def javaScriptConsoleMessage(self, msg, lineno, source_id):
        prints('preview js:%s:%s:'%(unicode(source_id), lineno), unicode(msg))

    def init_javascript(self):
        if not hasattr(self, 'js'):
            from calibre.utils.resources import compiled_coffeescript
            self.js = compiled_coffeescript('ebooks.oeb.display.utils', dynamic=False)
            self.js += compiled_coffeescript('ebooks.oeb.polish.preview', dynamic=False)
        self._line_numbers = None
        mf = self.mainFrame()
        mf.addToJavaScriptWindowObject("py_bridge", self)
        mf.evaluateJavaScript(self.js)

    @pyqtSlot(str, str, str)
    def request_sync(self, tag_name, href, lnum):
        try:
            self.sync_requested.emit(unicode(tag_name), unicode(href), int(unicode(lnum)))
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass

    def go_to_anchor(self, anchor, lnum):
        self.mainFrame().evaluateJavaScript('window.calibre_preview_integration.go_to_anchor(%s, %s)' % (
            json.dumps(anchor), json.dumps(str(lnum))))

    @pyqtSlot(str, str)
    def request_split(self, loc, totals):
        actions['split-in-preview'].setChecked(False)
        loc, totals = json.loads(unicode(loc)), json.loads(unicode(totals))
        if not loc or not totals:
            return error_dialog(self.view(), _('Invalid location'),
                                _('Cannot split on the body tag'), show=True)
        self.split_requested.emit(loc, totals)

    @property
    def line_numbers(self):
        if self._line_numbers is None:
            def atoi(x):
                ans, ok = x.toUInt()
                if not ok:
                    ans = None
                return ans
            self._line_numbers = sorted(uniq(filter(lambda x:x is not None, map(atoi, self.mainFrame().evaluateJavaScript(
                'window.calibre_preview_integration.line_numbers()').toStringList()))))
        return self._line_numbers

    def go_to_line(self, lnum):
        try:
            lnum = find_le(self.line_numbers, lnum)
        except IndexError:
            return
        self.mainFrame().evaluateJavaScript(
            'window.calibre_preview_integration.go_to_line(%d)' % lnum)

    def split_mode(self, enabled):
        self.mainFrame().evaluateJavaScript(
            'window.calibre_preview_integration.split_mode(%s)' % (
                'true' if enabled else 'false'))


class WebView(QWebView):

    def __init__(self, parent=None):
        QWebView.__init__(self, parent)
        self.inspector = QWebInspector(self)
        w = QApplication.instance().desktop().availableGeometry(self).width()
        self._size_hint = QSize(int(w/3), int(w/2))
        self._page = WebPage(self)
        self.setPage(self._page)
        self.inspector.setPage(self._page)
        self.clear()

    def sizeHint(self):
        return self._size_hint

    def refresh(self):
        self.pageAction(self.page().Reload).trigger()

    @dynamic_property
    def scroll_pos(self):
        def fget(self):
            mf = self.page().mainFrame()
            return (mf.scrollBarValue(Qt.Horizontal), mf.scrollBarValue(Qt.Vertical))
        def fset(self, val):
            mf = self.page().mainFrame()
            mf.setScrollBarValue(Qt.Horizontal, val[0])
            mf.setScrollBarValue(Qt.Vertical, val[1])
        return property(fget=fget, fset=fset)

    def clear(self):
        self.setHtml(_(
            '''
            <h3>Live preview</h3>

            <p>Here you will see a live preview of the HTML file you are currently editing.
            The preview will update automatically as you make changes.

            <p style="font-size:x-small; color: gray">Note that this is a quick preview
            only, it is not intended to simulate an actual ebook reader. Some
            aspects of your ebook will not work, such as, page breaks,
            page margins and embedded fonts that use font name aliasing.

            '''))

    def inspect(self):
        self.inspector.parent().show()
        self.inspector.parent().raise_()
        self.pageAction(self.page().InspectElement).trigger()

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        ca = self.pageAction(QWebPage.Copy)
        if ca.isEnabled():
            menu.addAction(ca)
        menu.addAction(actions['reload-preview'])
        menu.addAction(QIcon(I('debug.png')), _('Inspect element'), self.inspect)
        menu.exec_(ev.globalPos())

class Preview(QWidget):

    sync_requested = pyqtSignal(object, object)
    split_requested = pyqtSignal(object, object, object)
    split_start_requested = pyqtSignal()
    link_clicked = pyqtSignal(object, object)

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.l = l = QVBoxLayout()
        self.setLayout(l)
        l.setContentsMargins(0, 0, 0, 0)
        self.view = WebView(self)
        self.view.page().sync_requested.connect(self.request_sync)
        self.view.page().split_requested.connect(self.request_split)
        self.view.page().loadFinished.connect(self.load_finished)
        self.inspector = self.view.inspector
        self.inspector.setPage(self.view.page())
        l.addWidget(self.view)
        self.bar = QToolBar(self)
        l.addWidget(self.bar)

        ac = actions['auto-reload-preview']
        ac.setCheckable(True)
        ac.setChecked(True)
        ac.toggled.connect(self.auto_reload_toggled)
        self.auto_reload_toggled(ac.isChecked())
        self.bar.addAction(ac)

        ac = actions['sync-preview-to-editor']
        ac.setCheckable(True)
        ac.setChecked(True)
        ac.toggled.connect(self.sync_toggled)
        self.sync_toggled(ac.isChecked())
        self.bar.addAction(ac)

        self.bar.addSeparator()

        ac = actions['split-in-preview']
        ac.setCheckable(True)
        ac.setChecked(False)
        ac.toggled.connect(self.split_toggled)
        self.split_toggled(ac.isChecked())
        self.bar.addAction(ac)

        ac = actions['reload-preview']
        ac.triggered.connect(self.refresh)
        self.bar.addAction(ac)

        actions['preview-dock'].toggled.connect(self.visibility_changed)

        self.current_name = None
        self.last_sync_request = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        parse_worker.start()
        self.current_sync_request = None

        self.search = HistoryLineEdit2(self)
        self.search.initialize('tweak_book_preview_search')
        self.search.setPlaceholderText(_('Search in preview'))
        self.search.returnPressed.connect(partial(self.find, 'next'))
        self.bar.addSeparator()
        self.bar.addWidget(self.search)
        for d in ('next', 'prev'):
            ac = actions['find-%s-preview' % d]
            ac.triggered.connect(partial(self.find, d))
            self.bar.addAction(ac)

    def find(self, direction):
        text = unicode(self.search.text())
        self.view.findText(text, QWebPage.FindWrapsAroundDocument | (
            QWebPage.FindBackward if direction == 'prev' else QWebPage.FindFlags(0)))

    def request_sync(self, tagname, href, lnum):
        if self.current_name:
            c = current_container()
            if tagname == 'a' and href:
                if href and href.startswith('#'):
                    name = self.current_name
                else:
                    name = c.href_to_name(href, self.current_name) if href else None
                if name == self.current_name:
                    return self.view.page().go_to_anchor(urlparse(href).fragment, lnum)
                if name and c.exists(name) and c.mime_map[name] in OEB_DOCS:
                    return self.link_clicked.emit(name, urlparse(href).fragment or TOP)
            self.sync_requested.emit(self.current_name, lnum)

    def request_split(self, loc, totals):
        if self.current_name:
            self.split_requested.emit(self.current_name, loc, totals)

    def sync_to_editor(self, name, lnum):
        self.current_sync_request = (name, lnum)
        QTimer.singleShot(100, self._sync_to_editor)

    def _sync_to_editor(self):
        if not actions['sync-preview-to-editor'].isChecked():
            return
        try:
            if self.refresh_timer.isActive() or self.current_sync_request[0] != self.current_name:
                return QTimer.singleShot(100, self._sync_to_editor)
        except TypeError:
            return  # Happens if current_sync_request is None
        lnum = self.current_sync_request[1]
        self.current_sync_request = None
        self.view.page().go_to_line(lnum)

    def show(self, name):
        if name != self.current_name:
            self.refresh_timer.stop()
            self.current_name = name
            parse_worker.add_request(name)
            self.view.setUrl(QUrl.fromLocalFile(current_container().name_to_abspath(name)))
            return True

    def refresh(self):
        if self.current_name:
            self.refresh_timer.stop()
            # This will check if the current html has changed in its editor,
            # and re-parse it if so
            parse_worker.add_request(self.current_name)
            # Tell webkit to reload all html and associated resources
            current_url = QUrl.fromLocalFile(current_container().name_to_abspath(self.current_name))
            if current_url != self.view.url():
                # The container was changed
                self.view.setUrl(current_url)
            else:
                self.view.refresh()

    def clear(self):
        self.view.clear()
        self.current_name = None

    @property
    def is_visible(self):
        return actions['preview-dock'].isChecked()

    def start_refresh_timer(self):
        if self.is_visible and actions['auto-reload-preview'].isChecked():
            self.refresh_timer.start(tprefs['preview_refresh_time'] * 1000)

    def stop_refresh_timer(self):
        self.refresh_timer.stop()

    def auto_reload_toggled(self, checked):
        actions['auto-reload-preview'].setToolTip(_(
            'Auto reload preview when text changes in editor') if not checked else _(
                'Disable auto reload of preview'))

    def sync_toggled(self, checked):
        actions['sync-preview-to-editor'].setToolTip(_(
            'Disable syncing of preview position to editor position') if checked else _(
                'Enable syncing of preview position to editor position'))

    def visibility_changed(self, is_visible):
        if is_visible:
            self.refresh()

    def split_toggled(self, checked):
        actions['split-in-preview'].setToolTip(textwrap.fill(_(
            'Abort file split') if checked else _(
                'Split this file at a specified location.\n\nAfter clicking this button, click'
                ' inside the preview panel above at the location you want the file to be split.')))
        if checked:
            self.split_start_requested.emit()
        else:
            self.view.page().split_mode(False)

    def do_start_split(self):
        self.view.page().split_mode(True)

    def stop_split(self):
        actions['split-in-preview'].setChecked(False)

    def load_finished(self, ok):
        if actions['split-in-preview'].isChecked():
            if ok:
                self.do_start_split()
            else:
                self.stop_split()

    def apply_settings(self):
        s = self.view.page().settings()
        s.setFontSize(s.DefaultFontSize, tprefs['preview_base_font_size'])
        s.setFontSize(s.DefaultFixedFontSize, tprefs['preview_mono_font_size'])
        s.setFontSize(s.MinimumLogicalFontSize, tprefs['preview_minimum_font_size'])
        s.setFontSize(s.MinimumFontSize, tprefs['preview_minimum_font_size'])
        sf, ssf, mf = tprefs['preview_serif_family'], tprefs['preview_sans_family'], tprefs['preview_mono_family']
        s.setFontFamily(s.StandardFont, {'serif':sf, 'sans':ssf, 'mono':mf, None:sf}[tprefs['preview_standard_font_family']])
        s.setFontFamily(s.SerifFont, sf)
        s.setFontFamily(s.SansSerifFont, ssf)
        s.setFontFamily(s.FixedFont, mf)

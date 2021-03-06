# Copyright 2013, 2014 Kevin Reid <kpreid@switchb.org>
# 
# This file is part of ShinySDR.
# 
# ShinySDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# ShinySDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

# pylint: disable=no-init, attribute-defined-outside-init
# (no-init: pylint is confused by interfaces)
# (attribute-defined-outside-init: on purpose)

from __future__ import absolute_import, division

import json
import urlparse

from zope.interface import Interface, implements  # available via Twisted

from twisted.trial import unittest
from twisted.internet import reactor
from twisted.web import http

from gnuradio import gr

from shinysdr.db import DatabaseModel
from shinysdr.signals import SignalType
from shinysdr.values import ExportedState, CollectionState, NullExportedState, Poller, exported_block, exported_value, nullExportedState, setter
# TODO: StateStreamInner is an implementation detail; arrange a better interface to test
from shinysdr.web import StateStreamInner, WebService
from shinysdr.test import testutil


class TestWebSite(unittest.TestCase):
    # note: this test has a subclass

    def setUp(self):
        # TODO: arrange so we don't need to pass as many bogus strings
        self._service = WebService(
            reactor=reactor,
            http_endpoint='tcp:0',
            ws_endpoint='tcp:0',
            root_cap='ROOT',
            read_only_dbs={},
            writable_db=DatabaseModel(reactor, []),
            root_object=SiteStateStub(),
            flowgraph_for_debug=gr.top_block(),
            title='test title',
            note_dirty=_noop)
        self._service.startService()
        self.url = self._service.get_url()
    
    def tearDown(self):
        return self._service.stopService()
    
    def test_expected_url(self):
        self.assertEqual('/ROOT/', self._service.get_host_relative_url())
    
    def test_app_redirect(self):
        if 'ROOT' not in self.url:
            return  # test does not apply
            
        url_without_slash = self.url[:-1]
        
        def callback((response, data)):
            self.assertEqual(response.code, http.MOVED_PERMANENTLY)
            self.assertEqual(self.url,
                urlparse.urljoin(url_without_slash,
                    'ONLYONE'.join(response.headers.getRawHeaders('Location'))))
        
        return testutil.http_get(reactor, url_without_slash).addCallback(callback)
    
    def test_index_page(self):
        def callback((response, data)):
            self.assertEqual(response.code, http.OK)
            self.assertEqual(response.headers.getRawHeaders('Content-Type'), ['text/html'])
            self.assertIn('</html>', data)  # complete
            self.assertIn('<title>test title</title>', data)
            # TODO: Probably not here, add an end-to-end test for page title _default_.
        
        return testutil.http_get(reactor, self.url).addCallback(callback)
    
    def test_resource_page(self):
        # TODO: This ought to be a separate test of block-resources
        def callback((response, data)):
            self.assertEqual(response.code, http.OK)
            self.assertEqual(response.headers.getRawHeaders('Content-Type'), ['application/json'])
            description_json = json.loads(data)
            self.assertEqual(description_json, {
                u'kind': u'block',
                u'children': {},
            })
        return testutil.http_get(reactor, self.url + 'radio', accept='application/json').addCallback(callback)
    
    def test_flowgraph_page(self):
        # TODO: This ought to be a separate test of block-resources
        def callback((response, data)):
            self.assertEqual(response.code, http.OK)
            self.assertEqual(response.headers.getRawHeaders('Content-Type'), ['image/png'])
            # TODO ...
        return testutil.http_get(reactor, self.url + 'flow-graph').addCallback(callback)


class TestSiteWithoutRootCap(TestWebSite):
    """Like TestWebSite but with root_cap set to None."""
    def setUp(self):
        # TODO: arrange so we don't need to pass as many bogus strings
        self._service = WebService(
            reactor=reactor,
            http_endpoint='tcp:0',
            ws_endpoint='tcp:0',
            root_cap=None,
            read_only_dbs={},
            writable_db=DatabaseModel(reactor, []),
            root_object=SiteStateStub(),
            flowgraph_for_debug=gr.top_block(),
            title='test title',
            note_dirty=_noop)
        self._service.startService()
        self.url = self._service.get_url()
    
    def test_expected_url(self):
        self.assertEqual('/', self._service.get_host_relative_url())


def _noop():
    pass


class SiteStateStub(ExportedState):
    pass


class StateStreamTestCase(unittest.TestCase):
    object = None  # should be set in subclass setUp
    
    def setUpForObject(self, obj):
        self.object = obj
        self.updates = []
        self.poller = Poller()
        
        def send(value):
            self.updates.extend(json.loads(value))
        
        self.stream = StateStreamInner(
            send,
            self.object,
            'urlroot',
            lambda: None,  # TODO test noteDirty or make it unnecessary
            poller=self.poller)
    
    def getUpdates(self):
        # warning: implementation poking
        self.poller.poll()
        self.stream._flush()
        u = self.updates
        self.updates = []
        return u


class TestStateStream(StateStreamTestCase):
    def test_init_and_mutate(self):
        self.setUpForObject(StateSpecimen())
        self.assertEqual(self.getUpdates(), [
            ['register_block', 1, 'urlroot', ['shinysdr.test.test_web.IFoo']],
            ['register_cell', 2, 'urlroot/rw', self.object.state()['rw'].description()],
            ['value', 1, {'rw': 2}],
            ['value', 0, 1],
        ])
        self.assertEqual(self.getUpdates(), [])
        self.object.set_rw(2.0)
        self.assertEqual(self.getUpdates(), [
            ['value', 2, self.object.get_rw()],
        ])

    def test_two_references(self):
        """Two references are handled correctly, including not deleting until both are gone."""
        self.setUpForObject(DuplicateReferenceSpecimen())
        self.assertEqual(self.getUpdates(), [
            [u'register_block', 1, u'urlroot', []],
            [u'register_cell', 2, u'urlroot/foo', self.object.state()['foo'].description()],
            [u'register_block', 3, u'urlroot/foo', [u'shinysdr.values.INull']],
            [u'value', 3, {}],
            [u'value', 2, 3],
            [u'register_cell', 4, u'urlroot/bar', self.object.state()['bar'].description()],
            [u'value', 4, 3],
            [u'value', 1, {u'bar': 4, u'foo': 2}],
            [u'value', 0, 1],
        ])
        replacement = NullExportedState()
        # becomes distinct
        self.object.bar = replacement
        self.assertEqual(self.getUpdates(), [
            [u'register_block', 5, u'urlroot/bar', [u'shinysdr.values.INull']],
            [u'value', 5, {}],
            [u'value', 4, 5]
        ])
        # old value should be deleted
        self.object.foo = replacement
        self.assertEqual(self.getUpdates(), [
            [u'value', 2, 5],
            [u'delete', 3]
        ])
    
    def test_collection_delete(self):
        d = {'a': ExportedState()}
        self.setUpForObject(CollectionState(d, dynamic=True))
        
        self.assertEqual(self.getUpdates(), [
            ['register_block', 1, 'urlroot', []],
            ['register_cell', 2, 'urlroot/a', self.object.state()['a'].description()],
            ['register_block', 3, 'urlroot/a', []],
            ['value', 3, {}],
            ['value', 2, 3],
            ['value', 1, {'a': 2}],
            ['value', 0, 1],
        ])
        self.assertEqual(self.getUpdates(), [])
        del d['a']
        self.assertEqual(self.getUpdates(), [
            ['value', 1, {}],
            ['delete', 2],
            ['delete', 3],
        ])
    
    def test_send_set_normal(self):
        self.setUpForObject(StateSpecimen())
        self.assertIn(
            ['register_cell', 2, 'urlroot/rw', self.object.state()['rw'].description()],
            self.getUpdates())
        self.stream.dataReceived(json.dumps(['set', 2, 100.0, 1234]))
        self.assertEqual(self.getUpdates(), [
            ['value', 2, 100.0],
            ['done', 1234],
        ])
        self.stream.dataReceived(json.dumps(['set', 2, 100.0, 1234]))
        self.assertEqual(self.getUpdates(), [
            # don't see any value message
            ['done', 1234],
        ])
    
    def test_send_set_wrong_target(self):
        # Raised exception will be logged safely by the wrappper.
        # TODO: Instead of raising, report the error associated with the connection somehow
        self.setUpForObject(StateSpecimen())
        self.assertIn(
            ['register_block', 1, 'urlroot', ['shinysdr.test.test_web.IFoo']],
            self.getUpdates())
        self.assertRaises(Exception, lambda:  # TODO more specific error
            self.stream.dataReceived(json.dumps(['set', 1, 100.0, 1234])))
        self.assertEqual(self.getUpdates(), [])
    
    def test_send_set_unknown_target(self):
        # Raised exception will be logged safely by the wrappper.
        # TODO: Instead of raising, report the error associated with the connection somehow
        self.setUpForObject(StateSpecimen())
        self.getUpdates()
        self.assertRaises(KeyError, lambda:
            self.stream.dataReceived(json.dumps(['set', 99999, 100.0, 1234])))
        self.assertEqual(self.getUpdates(), [])


class IFoo(Interface):
    pass


class StateSpecimen(ExportedState):
    """Helper for TestStateStream"""
    implements(IFoo)

    def __init__(self):
        self.rw = 1.0
    
    @exported_value(type=float)
    def get_rw(self):
        return self.rw
    
    @setter
    def set_rw(self, value):
        self.rw = value


class DuplicateReferenceSpecimen(ExportedState):
    """Helper for TestStateStream"""

    def __init__(self):
        self.foo = self.bar = nullExportedState
    
    @exported_block()
    def get_foo(self):
        return self.foo
    
    @exported_block()
    def get_bar(self):
        return self.bar


class TestSerialization(StateStreamTestCase):
    def test_signal_type(self):
        self.setUpForObject(SerializationSpecimen())
        self.getUpdates()  # ignore initialization
        self.object.st = SignalType(kind='USB', sample_rate=1234.0)
        self.assertEqual(self.getUpdates(), [
            ['value', 2, {
                u'kind': 'USB',
                u'sample_rate': 1234.0,
            }],
        ])


class SerializationSpecimen(ExportedState):
    """Helper for TestStateStream"""
    implements(IFoo)

    def __init__(self):
        self.st = None
    
    @exported_value(type=SignalType)
    def get_st(self):
        return self.st

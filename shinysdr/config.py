#!/usr/bin/env python

# Copyright 2013, 2014, 2015, 2016 Kevin Reid <kpreid@switchb.org>
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


"""
Config interface.

The "public" operations on these objects are used by configuration files to specify configuration. The "private" operations are then used by main to implement the configuration.
"""


from __future__ import absolute_import, division

import base64
import os
import warnings
import __builtin__

from twisted.internet import defer
from twisted.python import log

# Note that gnuradio-dependent modules are loaded lazily, to avoid the startup time if all we're going to do is give a usage message
import shinysdr  # put into config namespace
from shinysdr.db import DatabaseModel, database_from_csv, databases_from_directory


__all__ = []  # appended later


class Config(object):
    def __init__(self, reactor):
        # public config elements
        self.features = _ConfigFeatures(self)
        self.devices = _ConfigDevices(self)
        self.sources = self.devices  # temporary legacy compat -- TODO emit deprecation warnings or something, then remove
        self.databases = _ConfigDbs(self, reactor)

        # provided for the convenience of the config file
        self.reactor = reactor
        
        # these are to be read by main
        self._state_filename = None
        self._service_makers = []
        
        # private: config state
        self.__server_audio = None
        
        # private: meta
        self.__waiting = []
        self.__finished = False
    
    @defer.inlineCallbacks
    def _wait_and_validate(self):
        yield defer.gatherResults(self.__waiting)
        
        self.__finished = True
        if len(self._service_makers) == 0:
            warnings.warn('No network service defined!')
    
    def _create_app(self):
        from shinysdr import session
        return session.AppRoot(
            devices=self.devices._values,
            audio_config=self.__server_audio,
            features=self.features._get_all())
    
    def _not_finished(self):
        if self.__finished:
            raise ConfigTooLateException()
    
    def wait_for(self, deferred):
        """Wait for the provided Deferred before assuming the configuration to be finished."""
        self._not_finished()
        self.__waiting.append(defer.maybeDeferred(lambda: deferred))
    
    def persist_to_file(self, filename):
        self._not_finished()
        if self._state_filename is not None:
            raise ConfigException('config.persist_to_file has already been done once')
        self._state_filename = str(filename)

    def serve_web(self, http_endpoint, ws_endpoint, root_cap=None, title=u'ShinySDR'):
        self._not_finished()
        # TODO: See if we're reinventing bits of Twisted service stuff here
        
        if root_cap is not None:
            root_cap = unicode(root_cap)
            if len(root_cap) <= 0:
                raise ConfigException('config.serve_web: root_cap must be None or a nonempty string')
        
        def make_service(app, note_dirty):
            # TODO: This is, of course, not where session objects should be created. Working on it...
            import shinysdr.web as lazy_web
            return lazy_web.WebService(
                reactor=self.reactor,
                root_object=app.get_session(),
                flowgraph_for_debug=app.get_receive_flowgraph(),  # TODO: Once we have the diagnostics or admin page however that turns out to work, this goes away
                note_dirty=note_dirty,
                read_only_dbs=self.databases._get_read_only_databases(),
                writable_db=self.databases._get_writable_database(),
                http_endpoint=http_endpoint,
                ws_endpoint=ws_endpoint,
                root_cap=root_cap,
                title=title)
        
        self._service_makers.append(make_service)

    def serve_ghpsdr(self):
        self._not_finished()
        # TODO: Alternate services should be provided using getPlugins rather than hardcoded
        
        def make_service(app, note_dirty):
            import shinysdr.plugins.ghpsdr as lazy_ghpsdr
            return lazy_ghpsdr.DspserverService(app.get_receive_flowgraph(), note_dirty, 'tcp:8000')
        
        self._service_makers.append(make_service)
    
    def set_server_audio_allowed(self, allowed, device_name='', sample_rate=44100):
        """
        Set whether clients are allowed to send output to the server audio device.
        """
        self._not_finished()
        
        if allowed:
            self.__server_audio = (str(device_name), int(sample_rate))
        else:
            self.__server_audio = None
    
    def set_stereo(self, value):
        """
        Deprecated alias for self.features.(en|dis)able('stereo').
        """
        if value:
            self.features.enable('stereo')
        else:
            self.features.disable('stereo')


__all__.append('Config')


class _ConfigDict(object):
    def __init__(self, config):
        self._values = {}
        self._config = config

    def add(self, key, value):
        self._config._not_finished()
        if not (isinstance(key, unicode) or isinstance(key, str)):
            # Used to just coerce, but I saw a user error where they did "config.devices.add(device)", so I figured an error is better
            raise ConfigException('Key must be a string, not a %s: %r' % (type(key), key))
        key = unicode(key)
        if key in self._values:
            raise ConfigException('Key %r already present' % (key,))
        self._values[key] = value


class _ConfigDevices(_ConfigDict):
    def add(self, key, *devices):
        if len(devices) <= 0:
            raise ConfigException('config.devices: no device(s) specified')
        from shinysdr.devices import merge_devices
        super(_ConfigDevices, self).add(key, merge_devices(devices))


class _ConfigDbs(object):
    __read_only_databases = None
    __writable_db = None
    
    def __init__(self, config, reactor):
        self._config = config
        self.__reactor = reactor
        
        self.__read_only_databases, diagnostics = databases_from_directory(self.__reactor,
            os.path.join(os.path.dirname(__file__), 'data/dbs/'))
        if len(diagnostics) > 0:
            raise ConfigException(diagnostics)
    
    def add_directory(self, path):
        self._config._not_finished()
        path = str(path)
        dbs, path_diagnostics = databases_from_directory(self.__reactor, path)
        self.__read_only_databases.update(dbs)
        for d in path_diagnostics:
            log.msg('%s: %s' % d)

    def add_writable_database(self, path):
        self._config._not_finished()
        path = str(path)
        if self.__writable_db is not None:
            raise ConfigException('Multiple writable databases are not yet supported.')
        self.__writable_db, diagnostics = database_from_csv(self.__reactor, path, writable=True)
        for d in diagnostics:
            log.msg('%s: %s' % (path, d))
    
    def _get_writable_database(self):
        if self.__writable_db is None:
            # TODO temporary stub till the client takes more configurability -- we should omit the writable db rather than having an unbacked one
            self.__writable_db = DatabaseModel(None, [], writable=True)
        return self.__writable_db
    
    def _get_read_only_databases(self):
        if self.__read_only_databases is None:
            self.__read_only_databases = {}
        return self.__read_only_databases


class _ConfigFeatures(object):
    def __init__(self, config):
        self._state = {
            'reboot': False,
            'stereo': True,
            '_test_disabled_feature': False,
            '_test_enabled_feature': True,
        }
        self.__config = config
    
    def enable(self, name):
        self.__config._not_finished()
        self._state[self.__validate(name)] = True
    
    def disable(self, name):
        self.__config._not_finished()
        self._state[self.__validate(name)] = False
    
    def __validate(self, name):
        name = unicode(name)
        if name not in self._state:
            raise ConfigException(u'Unknown feature name: %s' % name)
        return name
    
    def _get(self, name):
        return self._state[name]
    
    def _get_all(self):
        return dict(self._state)


def execute_config(config_obj, config_file):
    """
    Execute a config file with the special environment.
    Note: does not _wait_and_validate()
    """
    env = dict(__builtin__.__dict__)
    env.update({'shinysdr': shinysdr, 'config': config_obj})
    execfile(config_file, env)


__all__.append('execute_config')


def make_default_config():
    # TODO: support enumerating osmosdr devices and configuring specifically for them
    # TODO: support more than one audio device (moot currently because gnuradio doesn't have a enumeration operation)
    from shinysdr.devices import find_audio_rx_names
    audio_rx_names = find_audio_rx_names()
    if audio_rx_names:
        has_audio = True
        audio_rx_name = audio_rx_names[0]
    else:
        has_audio = False
        audio_rx_name = ''
    
    return '''\
# This is a ShinySDR configuration file. For more information about what can
# be put here, read the manual section on it, available from the running
# ShinySDR server at: http://localhost:8100/manual/configuration

from shinysdr.devices import AudioDevice
from shinysdr.plugins.osmosdr import OsmoSDRDevice
from shinysdr.plugins.simulate import SimulatedDevice

# OsmoSDR generic driver; handles USRP, RTL-SDR, FunCube Dongle, HackRF, etc.
# To select a specific device, replace '' with 'rtl=0' etc.
config.devices.add(u'osmo', OsmoSDRDevice(''))

# For hardware which uses a sound-card as its ADC or appears as an
# audio device.
%(audio_comment)sconfig.devices.add(u'audio', AudioDevice(rx_device='%(audio_rx_name)s'))

# Locally generated RF signals for test purposes.
config.devices.add(u'sim', SimulatedDevice())

config.persist_to_file('state.json')

# You can put CHIRP-style frequency list .csv files in this directory.
# See http://localhost:8100/manual/dbs for more information.
config.databases.add_directory('dbs/')

config.serve_web(
    # These are in Twisted endpoint description syntax:
    # <http://twistedmatrix.com/documents/current/api/twisted.internet.endpoints.html#serverFromString>
    # Note: ws_endpoint must currently be 1 greater than http_endpoint; if one
    # is SSL then both must be. These restrictions will be relaxed later.
    http_endpoint='tcp:8100',
    ws_endpoint='tcp:8101',

    # A secret placed in the URL as simple access control. Does not
    # provide any real security unless using HTTPS. The default value
    # in this file has been automatically generated from 128 random bits.
    # Set to None to not use any secret.
    root_cap='%(root_cap)s',
    
    # Page title / station name
    title='ShinySDR')
''' % {
        'root_cap': base64.urlsafe_b64encode(os.urandom(128 // 8)).replace('=', ''),
        'audio_comment': '' if has_audio else '# ',
        'audio_rx_name': audio_rx_name,
    }


__all__.append('make_default_config')


class ConfigException(Exception):
    """Indicates erroneous configuration of some type."""


__all__.append('ConfigException')


class ConfigTooLateException(ConfigException):
    """Indicates that a config method was called too late for it to take effect."""
    
    def __init__(self):
        super(ConfigTooLateException, self).__init__('Too late to modify configuration')


__all__.append('ConfigTooLateException')

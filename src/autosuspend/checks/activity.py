import copy
from datetime import datetime, timedelta, timezone
import glob
from io import BytesIO
import os
import pwd
import re
import socket
import subprocess
import time

import psutil

from . import (Activity,
               Check,
               ConfigurationError,
               SevereCheckError,
               TemporaryCheckError)
from .util import CommandMixin, NetworkMixin, XPathMixin
from ..util.systemd import list_logind_sessions


class ActiveCalendarEvent(NetworkMixin, Activity):
    """Determines activity by checking against events in an icalendar file."""

    def __init__(self, name, url, timeout):
        NetworkMixin.__init__(self, url, timeout)
        Activity.__init__(self, name)

    def check(self):
        from ..util.ical import list_calendar_events
        response = self.request()
        start = datetime.now(timezone.utc)
        end = start + timedelta(minutes=1)
        events = list_calendar_events(BytesIO(response.content), start, end)
        if events:
            return 'Calendar event {} is active'.format(events[0])
        else:
            return None


class ActiveConnection(Activity):
    """Checks if a client connection exists on specified ports."""

    @classmethod
    def create(cls, name, config):
        try:
            ports = config['ports']
            ports = ports.split(',')
            ports = [p.strip() for p in ports]
            ports = {int(p) for p in ports}
            return cls(name, ports)
        except KeyError:
            raise ConfigurationError('Missing option ports')
        except ValueError:
            raise ConfigurationError('Ports must be integers')

    def __init__(self, name, ports):
        Activity.__init__(self, name)
        self._ports = ports

    def check(self):
        own_addresses = [(item.family, item.address)
                         for sublist in psutil.net_if_addrs().values()
                         for item in sublist]
        connected = [c.laddr[1]
                     for c in psutil.net_connections()
                     if ((c.family, c.laddr[0]) in own_addresses and
                         c.status == 'ESTABLISHED' and
                         c.laddr[1] in self._ports)]
        if connected:
            return 'Ports {} are connected'.format(connected)


class ExternalCommand(CommandMixin, Activity):

    def __init__(self, name, command):
        CommandMixin.__init__(self, command)
        Check.__init__(self, name)

    def check(self):
        try:
            subprocess.check_call(self._command, shell=True)
            return 'Command {} succeeded'.format(self._command)
        except subprocess.CalledProcessError as error:
            return None


class Kodi(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            url = config.get('url', fallback='http://localhost:8080/jsonrpc')
            timeout = config.getint('timeout', fallback=5)
            return cls(name, url, timeout)
        except ValueError as error:
            raise ConfigurationError(
                'Url or timeout configuration wrong: {}'.format(error))

    def __init__(self, name, url, timeout):
        Check.__init__(self, name)
        self._url = url
        self._timeout = timeout

    def check(self):
        import requests
        import requests.exceptions

        try:
            reply = requests.get(self._url +
                                 '?request={"jsonrpc": "2.0", '
                                 '"id": 1, '
                                 '"method": "Player.GetActivePlayers"}',
                                 timeout=self._timeout).json()
            if 'result' not in reply:
                raise TemporaryCheckError('No result array in reply')
            if reply['result']:
                return "Kodi currently playing"
            else:
                return None
        except requests.exceptions.RequestException as error:
            raise TemporaryCheckError(error)


class KodiIdleTime(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            url = config.get('url', fallback='http://localhost:8080/jsonrpc')
            timeout = config.getint('timeout', fallback=5)
            idle_time = config.getint('idle_time', fallback=120)
            return cls(name, url, timeout, idle_time)
        except ValueError as error:
            raise ConfigurationError(
                'Url or timeout configuration wrong: {}'.format(error))

    def __init__(self, name, url, timeout, idle_time):
        Check.__init__(self, name)
        self._url = url
        self._timeout = timeout
        self._idle_time = idle_time

    def check(self):
        import requests
        import requests.exceptions

        try:
            reply = requests.get(
                self._url + '?request={{"jsonrpc": "2.0", '
                '"id": 1, '
                '"method": "XMBC.GetInfoBool"}},'
                '"params": {{"booleans": ["System.IdleTime({})"]}}'.format(
                    self._idle_time),
                timeout=self._timeout).json()
            if reply['result']["System.IdleTime({})".format(self._idle_time)]:
                return 'Someone interacts with Kodi'
            else:
                return None
        except (KeyError, TypeError) as error:
            raise TemporaryCheckError(error)
        except requests.exceptions.RequestException as error:
            raise TemporaryCheckError(error)


class Load(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name,
                       config.getfloat('threshold', fallback=2.5))
        except ValueError as error:
            raise ConfigurationError(
                'Unable to parse threshold as float: {}'.format(error))

    def __init__(self, name, threshold):
        Check.__init__(self, name)
        self._threshold = threshold

    def check(self):
        loadcurrent = os.getloadavg()[1]
        self.logger.debug("Load: %s", loadcurrent)
        if loadcurrent > self._threshold:
            return 'Load {} > threshold {}'.format(loadcurrent,
                                                   self._threshold)
        else:
            return None


class Mpd(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            host = config.get('host', fallback='localhost')
            port = config.getint('port', fallback=6600)
            timeout = config.getint('timeout', fallback=5)
            return cls(name, host, port, timeout)
        except ValueError as error:
            raise ConfigurationError(
                'Host port or timeout configuration wrong: {}'.format(error))

    def __init__(self, name, host, port, timeout):
        Check.__init__(self, name)
        self._host = host
        self._port = port
        self._timeout = timeout

    def _get_state(self):
        from mpd import MPDClient
        client = MPDClient()
        client.timeout = self._timeout
        client.connect(self._host, self._port)
        state = client.status()
        client.close()
        client.disconnect()
        return state

    def check(self):
        try:
            state = self._get_state()
            if state['state'] == 'play':
                return 'MPD currently playing'
            else:
                return None
        except (ConnectionError,
                ConnectionRefusedError,
                socket.timeout,
                socket.gaierror) as error:
            raise TemporaryCheckError(error)


class NetworkBandwidth(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            interfaces = config['interfaces']
            interfaces = interfaces.split(',')
            interfaces = [i.strip() for i in interfaces if i.strip()]
            if not interfaces:
                raise ConfigurationError('No interfaces configured')
            host_interfaces = psutil.net_if_addrs().keys()
            for interface in interfaces:
                if interface not in host_interfaces:
                    raise ConfigurationError(
                        'Network interface {} does not exist'.format(
                            interface))
            threshold_send = config.getfloat('threshold_send',
                                             fallback=100)
            threshold_receive = config.getfloat('threshold_receive',
                                                fallback=100)
            return cls(name, interfaces, threshold_send, threshold_receive)
        except KeyError as error:
            raise ConfigurationError(
                'Missing configuration key: {}'.format(error))
        except ValueError as error:
            raise ConfigurationError(
                'Threshold in wrong format: {}'.format(error))

    def __init__(self, name, interfaces, threshold_send, threshold_receive):
        Check.__init__(self, name)
        self._interfaces = interfaces
        self._threshold_send = threshold_send
        self._threshold_receive = threshold_receive
        self._previous_values = psutil.net_io_counters(pernic=True)
        self._previous_time = time.time()

    def check(self):
        new_values = psutil.net_io_counters(pernic=True)
        new_time = time.time()
        for interface in self._interfaces:
            if interface not in new_values or \
                    interface not in self._previous_values:
                raise TemporaryCheckError(
                    'Interface {} is missing'.format(interface))

            # send direction
            delta_send = new_values[interface].bytes_sent - \
                self._previous_values[interface].bytes_sent
            rate_send = delta_send / (new_time - self._previous_time)
            if rate_send > self._threshold_send:
                return 'Interface {} sending rate {} byte/s '\
                    'higher than threshold {}'.format(
                        interface, rate_send, self._threshold_send)

            delta_receive = new_values[interface].bytes_recv - \
                self._previous_values[interface].bytes_recv
            rate_receive = delta_receive / (new_time - self._previous_time)
            if rate_receive > self._threshold_receive:
                return 'Interface {} receive rate {} byte/s '\
                    'higher than threshold {}'.format(
                        interface, rate_receive, self._threshold_receive)


class Ping(Activity):
    """Check if one or several hosts are reachable via ping."""

    @classmethod
    def create(cls, name, config):
        try:
            hosts = config['hosts'].split(',')
            hosts = [h.strip() for h in hosts]
            return cls(name, hosts)
        except KeyError as error:
            raise ConfigurationError(
                'Unable to determine hosts to ping: {}'.format(error))

    def __init__(self, name, hosts):
        Check.__init__(self, name)
        self._hosts = hosts

    def check(self):
        for host in self._hosts:
            cmd = ['ping', '-q', '-c', '1', host]
            if subprocess.call(cmd,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                self.logger.debug("host " + host + " appears to be up")
                return 'Host {} is up'.format(host)
        return None


class Processes(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            processes = config['processes'].split(',')
            processes = [p.strip() for p in processes]
            return cls(name, processes)
        except KeyError:
            raise ConfigurationError('No processes to check specified')

    def __init__(self, name, processes):
        Check.__init__(self, name)
        self._processes = processes

    def check(self):
        for proc in psutil.process_iter():
            try:
                pinfo = proc.name()
                for name in self._processes:
                    if pinfo == name:
                        return 'Process {} is running'.format(name)
            except psutil.NoSuchProcess:
                pass
        return None


class Smb(Activity):

    @classmethod
    def create(cls, name, config):
        return cls(name)

    def check(self):
        try:
            status_output = subprocess.check_output(
                ['smbstatus', '-b']).decode('utf-8')
        except subprocess.CalledProcessError as error:
            raise SevereCheckError(error)

        self.logger.debug('Received status output:\n%s',
                          status_output)

        connections = []
        start_seen = False
        for line in status_output.splitlines():
            if start_seen:
                connections.append(line)
            else:
                if line.startswith('----'):
                    start_seen = True

        if connections:
            return 'SMB clients are connected:\n{}'.format(
                '\n'.join(connections))
        else:
            return None


class Users(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            user_regex = re.compile(
                config.get('name', fallback='.*'))
            terminal_regex = re.compile(
                config.get('terminal', fallback='.*'))
            host_regex = re.compile(
                config.get('host', fallback='.*'))
            return cls(name, user_regex, terminal_regex, host_regex)
        except re.error as error:
            raise ConfigurationError(
                'Regular expression is invalid: {}'.format(error))

    def __init__(self, name, user_regex, terminal_regex, host_regex):
        Activity.__init__(self, name)
        self._user_regex = user_regex
        self._terminal_regex = terminal_regex
        self._host_regex = host_regex

    def check(self):
        for entry in psutil.users():
            if self._user_regex.fullmatch(entry.name) is not None and \
                    self._terminal_regex.fullmatch(
                        entry.terminal) is not None and \
                    self._host_regex.fullmatch(entry.host) is not None:
                self.logger.debug('User %s on terminal %s from host %s '
                                  'matches criteria.', entry.name,
                                  entry.terminal, entry.host)
                return 'User {user} is logged in on terminal {terminal} ' \
                    'from {host} since {started}'.format(
                        user=entry.name, terminal=entry.terminal,
                        host=entry.host, started=entry.started)
        return None


class XIdleTime(Activity):
    """Check that local X display have been idle long enough."""

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name, config.getint('timeout', fallback=600),
                       config.get('method', fallback='sockets'),
                       re.compile(config.get('ignore_if_process',
                                             fallback=r'a^')),
                       re.compile(config.get('ignore_users',
                                             fallback=r'a^')))
        except re.error as error:
            raise ConfigurationError(
                'Regular expression is invalid: {}'.format(error))
        except ValueError as error:
            raise ConfigurationError(
                'Unable to parse configuration: {}'.format(error))

    def __init__(self, name, timeout, method,
                 ignore_process_re, ignore_users_re):
        Activity.__init__(self, name)
        self._timeout = timeout
        if method == 'sockets':
            self._provide_sessions = self._list_sessions_sockets
        elif method == 'logind':
            self._provide_sessions = self._list_sessions_logind
        else:
            raise ValueError(
                "Unknown session discovery method {}".format(method))
        self._ignore_process_re = ignore_process_re
        self._ignore_users_re = ignore_users_re

    def _list_sessions_sockets(self):
        """List running X sessions by iterating the X sockets.

        This method assumes that X servers are run under the users using the
        server.
        """
        sockets = glob.glob('/tmp/.X11-unix/X*')
        self.logger.debug('Found sockets: %s', sockets)

        results = []
        for sock in sockets:
            # determine the number of the X display
            try:
                display = int(sock[len('/tmp/.X11-unix/X'):])
            except ValueError as error:
                self.logger.warning(
                    'Cannot parse display number from socket %s. Skipping.',
                    sock, exc_info=True)
                continue

            # determine the user of the display
            try:
                user = pwd.getpwuid(os.stat(sock).st_uid).pw_name
            except (FileNotFoundError, KeyError) as error:
                self.logger.warning(
                    'Cannot get the owning user from socket %s. Skipping.',
                    sock, exc_info=True)
                continue

            results.append((display, user))

        return results

    def _list_sessions_logind(self):
        """List running X sessions using logind.

        This method assumes that a ``Display`` variable is set in the logind
        sessions.
        """
        results = []
        for session_id, properties in list_logind_sessions():
            if 'Name' in properties and 'Display' in properties:
                try:
                    results.append(
                        (int(properties['Display'].replace(':', '')),
                         str(properties['Name'])))
                except ValueError as e:
                    self.logger.warn(
                        'Unable to parse display from session properties %s',
                        properties, exc_info=True)
            else:
                self.logger.debug(
                    'Skipping session %s because it does not contain '
                    'a user name and a display', session_id)
        return results

    def _is_skip_process_running(self, user):
        user_processes = []
        for process in psutil.process_iter():
            try:
                if process.username() == user:
                    user_processes.append(process.name())
            except (psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                # ignore processes which have disappeared etc.
                pass

        for process in user_processes:
            if self._ignore_process_re.match(process) is not None:
                self.logger.debug(
                    "Process %s with pid %s matches the ignore regex '%s'."
                    " Skipping idle time check for this user.",
                    process.name(), process.pid, self._ignore_process_re)
                return True

        return False

    def check(self):
        for display, user in self._provide_sessions():
            self.logger.info('Checking display %s of user %s', display, user)

            # check whether this users should be ignored completely
            if self._ignore_users_re.match(user) is not None:
                self.logger.debug("Skipping user '%s' due to request", user)
                continue

            # check whether any of the running processes of this user matches
            # the ignore regular expression. In that case we skip idletime
            # checking because we assume the user has a process running that
            # inevitably tampers with the idle time.
            if self._is_skip_process_running(user):
                continue

            # prepare the environment for the xprintidle call
            env = copy.deepcopy(os.environ)
            env['DISPLAY'] = ':{}'.format(display)
            env['XAUTHORITY'] = os.path.join(os.path.expanduser('~' + user),
                                             '.Xauthority')

            try:
                idle_time = subprocess.check_output(
                    ['sudo', '-u', user, 'xprintidle'], env=env)
                idle_time = float(idle_time.strip()) / 1000.0
            except (subprocess.CalledProcessError, ValueError) as error:
                self.logger.warning(
                    'Unable to determine the idle time for display %s.',
                    display, exc_info=True)
                raise TemporaryCheckError(error)

            self.logger.debug(
                'Idle time for display %s of user %s is %s seconds.',
                display, user, idle_time)

            if idle_time < self._timeout:
                return 'X session {} of user {} ' \
                    'has idle time {} < threshold {}'.format(
                        display, user, idle_time, self._timeout)

        return None


class LogindSessionsIdle(Activity):
    """Prevents suspending in case a logind session is marked not idle.

    The decision is based on the ``IdleHint`` property of logind sessions.
    """

    @classmethod
    def create(cls, name, config):
        types = config.get('types', fallback='tty,x11,wayland')
        types = [t.strip() for t in types.split(',')]
        states = config.get('states', fallback='active,online')
        states = [t.strip() for t in states.split(',')]
        return cls(name, types, states)

    def __init__(self, name, types, states):
        Activity.__init__(self, name)
        self._types = types
        self._states = states

    def check(self):
        for session_id, properties in list_logind_sessions():
            self.logger.debug('Session %s properties: %s',
                              session_id, properties)

            if properties['Type'] not in self._types:
                self.logger.debug('Ignoring session of wrong type %s',
                                  properties['type'])
                continue
            if properties['State'] not in self._states:
                self.logger.debug('Ignoring session because its state is %s',
                                  properties['State'])
                continue

            if properties['IdleHint'] == 'no':
                return 'Login session {} is not idle'.format(
                    session_id)

        return None


class XPath(XPathMixin, Activity):

    def __init__(self, name, url, xpath, timeout):
        Activity.__init__(self, name)
        XPathMixin.__init__(self, url, xpath, timeout)

    def check(self):
        if self.evaluate():
            return "XPath matches for url " + self._url

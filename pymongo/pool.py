# Copyright 2011-2015 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import contextlib
import os
import platform
import socket
import sys
import threading

try:
    import ssl
    from ssl import SSLError
    _HAVE_SNI = getattr(ssl, 'HAS_SNI', False)
except ImportError:
    _HAVE_SNI = False
    class SSLError(socket.error):
        pass


from bson import DEFAULT_CODEC_OPTIONS
from bson.py3compat import imap, itervalues, _unicode
from bson.son import SON
from pymongo import auth, helpers, thread_util, __version__
from pymongo.common import MAX_MESSAGE_SIZE
from pymongo.errors import (AutoReconnect,
                            ConnectionFailure,
                            ConfigurationError,
                            DocumentTooLarge,
                            NetworkTimeout,
                            NotMasterError,
                            OperationFailure)
from pymongo.ismaster import IsMaster
from pymongo.monotonic import time as _time
from pymongo.network import (command,
                             receive_message,
                             socket_closed)
from pymongo.read_concern import DEFAULT_READ_CONCERN
from pymongo.read_preferences import ReadPreference
from pymongo.server_type import SERVER_TYPE
# Always use our backport so we always have support for IP address matching
from pymongo.ssl_match_hostname import match_hostname, CertificateError

# For SNI support. According to RFC6066, section 3, IPv4 and IPv6 literals are
# not permitted for SNI hostname.
try:
    from ipaddress import ip_address
    def is_ip_address(address):
        try:
            ip_address(_unicode(address))
            return True
        except (ValueError, UnicodeError):
            return False
except ImportError:
    if hasattr(socket, 'inet_pton') and socket.has_ipv6:
        # Most *nix, recent Windows
        def is_ip_address(address):
            try:
                # inet_pton rejects IPv4 literals with leading zeros
                # (e.g. 192.168.0.01), inet_aton does not, and we
                # can connect to them without issue. Use inet_aton.
                socket.inet_aton(address)
                return True
            except socket.error:
                try:
                    socket.inet_pton(socket.AF_INET6, address)
                    return True
                except socket.error:
                    return False
    else:
        # No inet_pton
        def is_ip_address(address):
            try:
                socket.inet_aton(address)
                return True
            except socket.error:
                if ':' in address:
                    # ':' is not a valid character for a hostname. If we get
                    # here a few things have to be true:
                    #   - We're on a recent version of python 2.7 (2.7.9+).
                    #     2.6 and older 2.7 versions don't support SNI.
                    #   - We're on Windows XP or some unusual Unix that doesn't
                    #     have inet_pton.
                    #   - The application is using IPv6 literals with TLS, which
                    #     is pretty unusual.
                    return True
                return False

try:
    from fcntl import fcntl, F_GETFD, F_SETFD, FD_CLOEXEC
    def _set_non_inheritable_non_atomic(fd):
        """Set the close-on-exec flag on the given file descriptor."""
        flags = fcntl(fd, F_GETFD)
        fcntl(fd, F_SETFD, flags | FD_CLOEXEC)
except ImportError:
    # Windows, various platforms we don't claim to support
    # (Jython, IronPython, ...), systems that don't provide
    # everything we need from fcntl, etc.
    def _set_non_inheritable_non_atomic(dummy):
        """Dummy function for platforms that don't provide fcntl."""
        pass


_METADATA = SON([
    ('driver', SON([('name', 'PyMongo'), ('version', __version__)])),
])

if sys.platform.startswith('linux'):
    _METADATA['os'] = SON([
        ('type', platform.system()),
        # Distro name and version (e.g. Ubuntu 16.04 xenial)
        ('name', ' '.join([part for part in
                           platform.linux_distribution() if part])),
        ('architecture', platform.machine()),
        # Kernel version (e.g. 4.4.0-17-generic).
        ('version', platform.release())
    ])
elif sys.platform == 'darwin':
    _METADATA['os'] = SON([
        ('type', platform.system()),
        ('name', platform.system()),
        ('architecture', platform.machine()),
        # (mac|i|tv)OS(X) version (e.g. 10.11.6) instead of darwin
        # kernel version.
        ('version', platform.mac_ver()[0])
    ])
elif sys.platform == 'win32':
    _METADATA['os'] = SON([
        ('type', platform.system()),
        # "Windows XP", "Windows 7", "Windows 10", etc.
        ('name', ' '.join((platform.system(), platform.release()))),
        ('architecture', platform.machine()),
        # Windows patch level (e.g. 5.1.2600-SP3)
        ('version', '-'.join(platform.win32_ver()[1:3]))
    ])
elif sys.platform.startswith('java'):
    _name, _ver, _arch = platform.java_ver()[-1]
    _METADATA['os'] = SON([
        # Linux, Windows 7, Mac OS X, etc.
        ('type', _name),
        ('name', _name),
        # x86, x86_64, AMD64, etc.
        ('architecture', _arch),
        # Linux kernel version, OSX version, etc.
        ('version', _ver)
    ])
else:
    # Get potential alias (e.g. SunOS 5.11 becomes Solaris 2.11)
    _aliased = platform.system_alias(
        platform.system(), platform.release(), platform.version())
    _METADATA['os'] = SON([
        ('type', platform.system()),
        ('name', ' '.join([part for part in _aliased[:2] if part])),
        ('architecture', platform.machine()),
        ('version', _aliased[2])
    ])

if platform.python_implementation().startswith('PyPy'):
    _METADATA['platform'] = ' '.join(
        (platform.python_implementation(),
         '.'.join(imap(str, sys.pypy_version_info)),
         '(Python %s)' % '.'.join(imap(str, sys.version_info))))
elif sys.platform.startswith('java'):
    _METADATA['platform'] = ' '.join(
        (platform.python_implementation(),
         '.'.join(imap(str, sys.version_info)),
         '(%s)' % ' '.join((platform.system(), platform.release()))))
else:
    _METADATA['platform'] = ' '.join(
        (platform.python_implementation(),
         '.'.join(imap(str, sys.version_info))))


# If the first getaddrinfo call of this interpreter's life is on a thread,
# while the main thread holds the import lock, getaddrinfo deadlocks trying
# to import the IDNA codec. Import it here, where presumably we're on the
# main thread, to avoid the deadlock. See PYTHON-607.
u'foo'.encode('idna')


def _raise_connection_failure(address, error):
    """Convert a socket.error to ConnectionFailure and raise it."""
    host, port = address
    # If connecting to a Unix socket, port will be None.
    if port is not None:
        msg = '%s:%d: %s' % (host, port, error)
    else:
        msg = '%s: %s' % (host, error)
    if isinstance(error, socket.timeout):
        raise NetworkTimeout(msg)
    elif isinstance(error, SSLError) and 'timed out' in str(error):
        # CPython 2.6, 2.7, PyPy 2.x, and PyPy3 do not distinguish network
        # timeouts from other SSLErrors (https://bugs.python.org/issue10272).
        # Luckily, we can work around this limitation because the phrase
        # 'timed out' appears in all the timeout related SSLErrors raised
        # on the above platforms. CPython >= 3.2 and PyPy3.3 correctly raise
        # socket.timeout.
        raise NetworkTimeout(msg)
    else:
        raise AutoReconnect(msg)


class PoolOptions(object):

    __slots__ = ('__max_pool_size', '__min_pool_size', '__max_idle_time_ms',
                 '__connect_timeout', '__socket_timeout',
                 '__wait_queue_timeout', '__wait_queue_multiple',
                 '__ssl_context', '__ssl_match_hostname', '__socket_keepalive',
                 '__event_listeners', '__appname', '__metadata')

    def __init__(self, max_pool_size=100, min_pool_size=0,
                 max_idle_time_ms=None, connect_timeout=None,
                 socket_timeout=None, wait_queue_timeout=None,
                 wait_queue_multiple=None, ssl_context=None,
                 ssl_match_hostname=True, socket_keepalive=False,
                 event_listeners=None, appname=None):

        self.__max_pool_size = max_pool_size
        self.__min_pool_size = min_pool_size
        self.__max_idle_time_ms = max_idle_time_ms
        self.__connect_timeout = connect_timeout
        self.__socket_timeout = socket_timeout
        self.__wait_queue_timeout = wait_queue_timeout
        self.__wait_queue_multiple = wait_queue_multiple
        self.__ssl_context = ssl_context
        self.__ssl_match_hostname = ssl_match_hostname
        self.__socket_keepalive = socket_keepalive
        self.__event_listeners = event_listeners
        self.__appname = appname
        self.__metadata = _METADATA.copy()
        if appname:
            self.__metadata['application'] = {'name': appname}

    @property
    def max_pool_size(self):
        """The maximum allowable number of concurrent connections to each
        connected server. Requests to a server will block if there are
        `maxPoolSize` outstanding connections to the requested server.
        Defaults to 100. Cannot be 0.

        When a server's pool has reached `max_pool_size`, operations for that
        server block waiting for a socket to be returned to the pool. If
        ``waitQueueTimeoutMS`` is set, a blocked operation will raise
        :exc:`~pymongo.errors.ConnectionFailure` after a timeout.
        By default ``waitQueueTimeoutMS`` is not set.
        """
        return self.__max_pool_size

    @property
    def min_pool_size(self):
        """The minimum required number of concurrent connections that the pool
        will maintain to each connected server. Default is 0.
        """
        return self.__min_pool_size

    @property
    def max_idle_time_ms(self):
        """The maximum number of milliseconds that a connection can remain
        idle in the pool before being removed and replaced. Defaults to
        `None` (no limit).
        """
        return self.__max_idle_time_ms

    @property
    def connect_timeout(self):
        """How long a connection can take to be opened before timing out.
        """
        return self.__connect_timeout

    @property
    def socket_timeout(self):
        """How long a send or receive on a socket can take before timing out.
        """
        return self.__socket_timeout

    @property
    def wait_queue_timeout(self):
        """How long a thread will wait for a socket from the pool if the pool
        has no free sockets.
        """
        return self.__wait_queue_timeout

    @property
    def wait_queue_multiple(self):
        """Multiplied by max_pool_size to give the number of threads allowed
        to wait for a socket at one time.
        """
        return self.__wait_queue_multiple

    @property
    def ssl_context(self):
        """An SSLContext instance or None.
        """
        return self.__ssl_context

    @property
    def ssl_match_hostname(self):
        """Call ssl.match_hostname if cert_reqs is not ssl.CERT_NONE.
        """
        return self.__ssl_match_hostname

    @property
    def socket_keepalive(self):
        """Whether to send periodic messages to determine if a connection
        is closed.
        """
        return self.__socket_keepalive

    @property
    def event_listeners(self):
        """An instance of pymongo.monitoring._EventListeners.
        """
        return self.__event_listeners

    @property
    def appname(self):
        """The application name, for sending with ismaster in server handshake.
        """
        return self.__appname

    @property
    def metadata(self):
        """A dict of metadata about the application, driver, os, and platform.
        """
        return self.__metadata.copy()


class SocketInfo(object):
    """Store a socket with some metadata.

    :Parameters:
      - `sock`: a raw socket object
      - `pool`: a Pool instance
      - `ismaster`: optional IsMaster instance, response to ismaster on `sock`
      - `address`: the server's (host, port)
    """
    def __init__(self, sock, pool, ismaster, address):
        self.sock = sock
        self.address = address
        self.authset = set()
        self.closed = False
        self.last_checkout = _time()
        self.is_writable = ismaster.is_writable if ismaster else None
        self.max_wire_version = ismaster.max_wire_version if ismaster else None
        self.max_bson_size = ismaster.max_bson_size if ismaster else None
        self.max_message_size = (
            ismaster.max_message_size if ismaster else MAX_MESSAGE_SIZE)
        self.max_write_batch_size = (
            ismaster.max_write_batch_size if ismaster else None)

        self.listeners = pool.opts.event_listeners

        if ismaster:
            self.is_mongos = ismaster.server_type == SERVER_TYPE.Mongos
        else:
            self.is_mongos = None

        # The pool's pool_id changes with each reset() so we can close sockets
        # created before the last reset.
        self.pool_id = pool.pool_id

    def command(self, dbname, spec, slave_ok=False,
                read_preference=ReadPreference.PRIMARY,
                codec_options=DEFAULT_CODEC_OPTIONS, check=True,
                allowable_errors=None, check_keys=False,
                read_concern=DEFAULT_READ_CONCERN,
                write_concern=None,
                parse_write_concern_error=False,
                collation=None):
        """Execute a command or raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `dbname`: name of the database on which to run the command
          - `spec`: a command document as a dict, SON, or mapping object
          - `slave_ok`: whether to set the SlaveOkay wire protocol bit
          - `read_preference`: a read preference
          - `codec_options`: a CodecOptions instance
          - `check`: raise OperationFailure if there are errors
          - `allowable_errors`: errors to ignore if `check` is True
          - `check_keys`: if True, check `spec` for invalid keys
          - `read_concern`: The read concern for this command.
          - `write_concern`: The write concern for this command.
          - `parse_write_concern_error`: Whether to parse the
            ``writeConcernError`` field in the command response.
          - `collation`: The collation for this command.
        """
        if self.max_wire_version < 4 and not read_concern.ok_for_legacy:
            raise ConfigurationError(
                'read concern level of %s is not valid '
                'with a max wire version of %d.'
                % (read_concern.level, self.max_wire_version))
        if not (write_concern is None or write_concern.acknowledged or
                collation is None):
            raise ConfigurationError(
                'Collation is unsupported for unacknowledged writes.')
        if self.max_wire_version >= 5 and write_concern:
            spec['writeConcern'] = write_concern.document
        elif self.max_wire_version < 5 and collation is not None:
            raise ConfigurationError(
                'Must be connected to MongoDB 3.4+ to use a collation.')
        try:
            return command(self.sock, dbname, spec, slave_ok,
                           self.is_mongos, read_preference, codec_options,
                           check, allowable_errors, self.address,
                           check_keys, self.listeners, self.max_bson_size,
                           read_concern,
                           parse_write_concern_error=parse_write_concern_error,
                           collation=collation)
        except OperationFailure:
            raise
        # Catch socket.error, KeyboardInterrupt, etc. and close ourselves.
        except BaseException as error:
            self._raise_connection_failure(error)

    def send_message(self, message, max_doc_size):
        """Send a raw BSON message or raise ConnectionFailure.

        If a network exception is raised, the socket is closed.
        """
        if (self.max_bson_size is not None
                and max_doc_size > self.max_bson_size):
            raise DocumentTooLarge(
                "BSON document too large (%d bytes) - the connected server"
                "supports BSON document sizes up to %d bytes." %
                (max_doc_size, self.max_bson_size))

        try:
            self.sock.sendall(message)
        except BaseException as error:
            self._raise_connection_failure(error)

    def receive_message(self, operation, request_id):
        """Receive a raw BSON message or raise ConnectionFailure.

        If any exception is raised, the socket is closed.
        """
        try:
            return receive_message(
                self.sock, operation, request_id, self.max_message_size)
        except BaseException as error:
            self._raise_connection_failure(error)

    def legacy_write(self, request_id, msg, max_doc_size, with_last_error):
        """Send OP_INSERT, etc., optionally returning response as a dict.

        Can raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `request_id`: an int.
          - `msg`: bytes, an OP_INSERT, OP_UPDATE, or OP_DELETE message,
            perhaps with a getlasterror command appended.
          - `max_doc_size`: size in bytes of the largest document in `msg`.
          - `with_last_error`: True if a getlasterror command is appended.
        """
        if not with_last_error and not self.is_writable:
            # Write won't succeed, bail as if we'd done a getlasterror.
            raise NotMasterError("not master")

        self.send_message(msg, max_doc_size)
        if with_last_error:
            response = self.receive_message(1, request_id)
            return helpers._check_gle_response(response)

    def write_command(self, request_id, msg):
        """Send "insert" etc. command, returning response as a dict.

        Can raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `request_id`: an int.
          - `msg`: bytes, the command message.
        """
        self.send_message(msg, 0)
        response = helpers._unpack_response(self.receive_message(1, request_id))
        assert response['number_returned'] == 1
        result = response['data'][0]

        # Raises NotMasterError or OperationFailure.
        helpers._check_command_response(result)
        return result

    def check_auth(self, all_credentials):
        """Update this socket's authentication.

        Log in or out to bring this socket's credentials up to date with
        those provided. Can raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `all_credentials`: dict, maps auth source to MongoCredential.
        """
        if all_credentials or self.authset:
            cached = set(itervalues(all_credentials))
            authset = self.authset.copy()

            # Logout any credentials that no longer exist in the cache.
            for credentials in authset - cached:
                auth.logout(credentials.source, self)
                self.authset.discard(credentials)

            for credentials in cached - authset:
                auth.authenticate(credentials, self)
                self.authset.add(credentials)

    def authenticate(self, credentials):
        """Log in to the server and store these credentials in `authset`.

        Can raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `credentials`: A MongoCredential.
        """
        auth.authenticate(credentials, self)
        self.authset.add(credentials)

    def close(self):
        self.closed = True
        # Avoid exceptions on interpreter shutdown.
        try:
            self.sock.close()
        except:
            pass

    def _raise_connection_failure(self, error):
        # Catch *all* exceptions from socket methods and close the socket. In
        # regular Python, socket operations only raise socket.error, even if
        # the underlying cause was a Ctrl-C: a signal raised during socket.recv
        # is expressed as an EINTR error from poll. See internal_select_ex() in
        # socketmodule.c. All error codes from poll become socket.error at
        # first. Eventually in PyEval_EvalFrameEx the interpreter checks for
        # signals and throws KeyboardInterrupt into the current frame on the
        # main thread.
        #
        # But in Gevent and Eventlet, the polling mechanism (epoll, kqueue,
        # ...) is called in Python code, which experiences the signal as a
        # KeyboardInterrupt from the start, rather than as an initial
        # socket.error, so we catch that, close the socket, and reraise it.
        self.close()
        if isinstance(error, socket.error):
            _raise_connection_failure(self.address, error)
        else:
            raise error

    def __eq__(self, other):
        return self.sock == other.sock

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.sock)

    def __repr__(self):
        return "SocketInfo(%s)%s at %s" % (
            repr(self.sock),
            self.closed and " CLOSED" or "",
            id(self)
        )


def _create_connection(address, options):
    """Given (host, port) and PoolOptions, connect and return a socket object.

    Can raise socket.error.

    This is a modified version of create_connection from CPython >= 2.6.
    """
    host, port = address

    # Check if dealing with a unix domain socket
    if host.endswith('.sock'):
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionFailure("UNIX-sockets are not supported "
                                    "on this system")
        sock = socket.socket(socket.AF_UNIX)
        # SOCK_CLOEXEC not supported for Unix sockets.
        _set_non_inheritable_non_atomic(sock.fileno())
        try:
            sock.connect(host)
            return sock
        except socket.error:
            sock.close()
            raise

    # Don't try IPv6 if we don't support it. Also skip it if host
    # is 'localhost' (::1 is fine). Avoids slow connect issues
    # like PYTHON-356.
    family = socket.AF_INET
    if socket.has_ipv6 and host != 'localhost':
        family = socket.AF_UNSPEC

    err = None
    for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        af, socktype, proto, dummy, sa = res
        # SOCK_CLOEXEC was new in CPython 3.2, and only available on a limited
        # number of platforms (newer Linux and *BSD). Starting with CPython 3.4
        # all file descriptors are created non-inheritable. See PEP 446.
        try:
            sock = socket.socket(
                af, socktype | getattr(socket, 'SOCK_CLOEXEC', 0), proto)
        except socket.error:
            # Can SOCK_CLOEXEC be defined even if the kernel doesn't support
            # it?
            sock = socket.socket(af, socktype, proto)
        # Fallback when SOCK_CLOEXEC isn't available.
        _set_non_inheritable_non_atomic(sock.fileno())
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(options.connect_timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE,
                            options.socket_keepalive)
            sock.connect(sa)
            return sock
        except socket.error as e:
            err = e
            sock.close()

    if err is not None:
        raise err
    else:
        # This likely means we tried to connect to an IPv6 only
        # host with an OS/kernel or Python interpreter that doesn't
        # support IPv6. The test case is Jython2.5.1 which doesn't
        # support IPv6 at all.
        raise socket.error('getaddrinfo failed')


def _configured_socket(address, options):
    """Given (host, port) and PoolOptions, return a configured socket.

    Can raise socket.error, ConnectionFailure, or CertificateError.

    Sets socket's SSL and timeout options.
    """
    sock = _create_connection(address, options)
    ssl_context = options.ssl_context

    if ssl_context is not None:
        host = address[0]
        try:
            # According to RFC6066, section 3, IPv4 and IPv6 literals are
            # not permitted for SNI hostname.
            if _HAVE_SNI and not is_ip_address(host):
                sock = ssl_context.wrap_socket(sock, server_hostname=host)
            else:
                sock = ssl_context.wrap_socket(sock)
        except IOError as exc:
            sock.close()
            raise ConnectionFailure("SSL handshake failed: %s" % (str(exc),))
        if ssl_context.verify_mode and options.ssl_match_hostname:
            try:
                match_hostname(sock.getpeercert(), hostname=host)
            except CertificateError:
                sock.close()
                raise

    sock.settimeout(options.socket_timeout)
    return sock


# Do *not* explicitly inherit from object or Jython won't call __del__
# http://bugs.jython.org/issue1057
class Pool:
    def __init__(self, address, options, handshake=True):
        """
        :Parameters:
          - `address`: a (hostname, port) tuple
          - `options`: a PoolOptions instance
          - `handshake`: whether to call ismaster for each new SocketInfo
        """
        # Check a socket's health with socket_closed() every once in a while.
        # Can override for testing: 0 to always check, None to never check.
        self._check_interval_seconds = 1

        self.sockets = set()
        self.lock = threading.Lock()
        self.active_sockets = 0

        # Keep track of resets, so we notice sockets created before the most
        # recent reset and close them.
        self.pool_id = 0
        self.pid = os.getpid()
        self.address = address
        self.opts = options
        self.handshake = handshake

        if (self.opts.wait_queue_multiple is None or
                self.opts.max_pool_size is None):
            max_waiters = None
        else:
            max_waiters = (
                self.opts.max_pool_size * self.opts.wait_queue_multiple)

        self._socket_semaphore = thread_util.create_semaphore(
            self.opts.max_pool_size, max_waiters)

    def reset(self):
        with self.lock:
            self.pool_id += 1
            self.pid = os.getpid()
            sockets, self.sockets = self.sockets, set()
            self.active_sockets = 0

        for sock_info in sockets:
            sock_info.close()

    def remove_stale_sockets(self):
        with self.lock:
            if self.opts.max_idle_time_ms is not None:
                for sock_info in self.sockets.copy():
                    age = _time() - sock_info.last_checkout
                    if age > self.opts.max_idle_time_ms:
                        self.sockets.remove(sock_info)
                        sock_info.close()

        while len(
                self.sockets) + self.active_sockets < self.opts.min_pool_size:
            sock_info = self.connect()
            with self.lock:
                self.sockets.add(sock_info)

    def connect(self):
        """Connect to Mongo and return a new SocketInfo.

        Can raise ConnectionFailure or CertificateError.

        Note that the pool does not keep a reference to the socket -- you
        must call return_socket() when you're done with it.
        """
        sock = None
        try:
            sock = _configured_socket(self.address, self.opts)
            if self.handshake:
                cmd = SON([
                    ('ismaster', 1),
                    ('client', self.opts.metadata)
                ])
                ismaster = IsMaster(
                    command(sock,
                            'admin',
                            cmd,
                            False,
                            False,
                            ReadPreference.PRIMARY,
                            DEFAULT_CODEC_OPTIONS))
            else:
                ismaster = None
            return SocketInfo(sock, self, ismaster, self.address)
        except socket.error as error:
            if sock is not None:
                sock.close()
            _raise_connection_failure(self.address, error)

    @contextlib.contextmanager
    def get_socket(self, all_credentials, checkout=False):
        """Get a socket from the pool. Use with a "with" statement.

        Returns a :class:`SocketInfo` object wrapping a connected
        :class:`socket.socket`.

        This method should always be used in a with-statement::

            with pool.get_socket(credentials, checkout) as socket_info:
                socket_info.send_message(msg)
                data = socket_info.receive_message(op_code, request_id)

        The socket is logged in or out as needed to match ``all_credentials``
        using the correct authentication mechanism for the server's wire
        protocol version.

        Can raise ConnectionFailure or OperationFailure.

        :Parameters:
          - `all_credentials`: dict, maps auth source to MongoCredential.
          - `checkout` (optional): keep socket checked out.
        """
        # First get a socket, then attempt authentication. Simplifies
        # semaphore management in the face of network errors during auth.
        sock_info = self._get_socket_no_auth()
        try:
            sock_info.check_auth(all_credentials)
            yield sock_info
        except:
            # Exception in caller. Decrement semaphore.
            self.return_socket(sock_info)
            raise
        else:
            if not checkout:
                self.return_socket(sock_info)

    def _get_socket_no_auth(self):
        """Get or create a SocketInfo. Can raise ConnectionFailure."""
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_client:TestClient.test_fork for an example of
        # what could go wrong otherwise
        if self.pid != os.getpid():
            self.reset()

        # Get a free socket or create one.
        if not self._socket_semaphore.acquire(
                True, self.opts.wait_queue_timeout):
            self._raise_wait_queue_timeout()
        with self.lock:
            self.active_sockets += 1

        # We've now acquired the semaphore and must release it on error.
        try:
            try:
                # set.pop() isn't atomic in Jython less than 2.7, see
                # http://bugs.jython.org/issue1854
                with self.lock:
                    sock_info, from_pool = self.sockets.pop(), True
            except KeyError:
                # Can raise ConnectionFailure or CertificateError.
                sock_info, from_pool = self.connect(), False
            # If socket is idle, open a new one.
            if self.opts.max_idle_time_ms is not None:
                age = _time() - sock_info.last_checkout
                if age > self.opts.max_idle_time_ms:
                    sock_info.close()
                    sock_info, from_pool = self.connect(), False

            if from_pool:
                # Can raise ConnectionFailure.
                sock_info = self._check(sock_info)

        except:
            self._socket_semaphore.release()
            with self.lock:
                self.active_sockets -= 1
            raise

        sock_info.last_checkout = _time()
        return sock_info

    def return_socket(self, sock_info):
        """Return the socket to the pool, or if it's closed discard it."""
        if self.pid != os.getpid():
            self.reset()
        else:
            if sock_info.pool_id != self.pool_id:
                sock_info.close()
            elif not sock_info.closed:
                with self.lock:
                    self.sockets.add(sock_info)

        self._socket_semaphore.release()
        with self.lock:
            self.active_sockets -= 1

    def _check(self, sock_info):
        """This side-effecty function checks if this pool has been reset since
        the last time this socket was used, or if the socket has been closed by
        some external network error, and if so, attempts to create a new socket.
        If this connection attempt fails we reset the pool and reraise the
        ConnectionFailure.

        Checking sockets lets us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only do this if it's been > 1 second since
        the last socket checkout, to keep performance reasonable - we
        can't avoid AutoReconnects completely anyway.
        """
        error = False

        # How long since socket was last checked out.
        age = _time() - sock_info.last_checkout
        if (self._check_interval_seconds is not None
                and (
                    0 == self._check_interval_seconds
                    or age > self._check_interval_seconds)):
            if socket_closed(sock_info.sock):
                sock_info.close()
                error = True

        if not error:
            return sock_info
        else:
            return self.connect()

    def _raise_wait_queue_timeout(self):
        raise ConnectionFailure(
            'Timed out waiting for socket from pool with max_size %r and'
            ' wait_queue_timeout %r' % (
                self.opts.max_pool_size, self.opts.wait_queue_timeout))

    def __del__(self):
        # Avoid ResourceWarnings in Python 3
        for sock_info in self.sockets:
            sock_info.close()

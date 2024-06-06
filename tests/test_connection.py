import itertools
import socket
import types
from unittest import TestCase
from unittest import mock
from unittest.mock import patch, MagicMock
import pytest
import redis
from redis import ConnectionPool, Redis
from redis._parsers import _HiredisParser, _RESP2Parser, _RESP3Parser
from redis.backoff import NoBackoff
from redis.connection import (
    Connection,
    SSLConnection,
    UnixDomainSocketConnection,
    parse_url,
    UsernamePasswordCredentialProvider,
    AuthenticationError
)
from redis.exceptions import ConnectionError, InvalidResponse, TimeoutError
from redis.retry import Retry
from redis.utils import HIREDIS_AVAILABLE

from .conftest import skip_if_server_version_lt
from .mocks import MockSocket


@pytest.mark.skipif(HIREDIS_AVAILABLE, reason="PythonParser only")
@pytest.mark.onlynoncluster
def test_invalid_response(r):
    raw = b"x"
    parser = r.connection._parser
    with mock.patch.object(parser._buffer, "readline", return_value=raw):
        with pytest.raises(InvalidResponse) as cm:
            parser.read_response()
    assert str(cm.value) == f"Protocol Error: {raw!r}"


@skip_if_server_version_lt("4.0.0")
@pytest.mark.redismod
def test_loading_external_modules(r):
    def inner():
        pass

    r.load_external_module("myfuncname", inner)
    assert getattr(r, "myfuncname") == inner
    assert isinstance(getattr(r, "myfuncname"), types.FunctionType)

    # and call it
    from redis.commands import RedisModuleCommands

    j = RedisModuleCommands.json
    r.load_external_module("sometestfuncname", j)

    # d = {'hello': 'world!'}
    # mod = j(r)
    # mod.set("fookey", ".", d)
    # assert mod.get('fookey') == d


class TestConnection(TestCase):
    def test_disconnect(self):
        conn = Connection()
        mock_sock = mock.Mock()
        conn._sock = mock_sock
        conn.disconnect()
        mock_sock.shutdown.assert_called_once()
        mock_sock.close.assert_called_once()
        assert conn._sock is None

    def test_disconnect__shutdown_OSError(self):
        """An OSError on socket shutdown will still close the socket."""
        conn = Connection()
        mock_sock = mock.Mock()
        conn._sock = mock_sock
        conn._sock.shutdown.side_effect = OSError
        conn.disconnect()
        mock_sock.shutdown.assert_called_once()
        mock_sock.close.assert_called_once()
        assert conn._sock is None

    def test_disconnect__close_OSError(self):
        """An OSError on socket close will still clear out the socket."""
        conn = Connection()
        mock_sock = mock.Mock()
        conn._sock = mock_sock
        conn._sock.close.side_effect = OSError
        conn.disconnect()
        mock_sock.shutdown.assert_called_once()
        mock_sock.close.assert_called_once()
        assert conn._sock is None

    def clear(self, conn):
        conn.retry_on_error.clear()

    def test_retry_connect_on_timeout_error(self):
        """Test that the _connect function is retried in case of a timeout"""
        conn = Connection(retry_on_timeout=True, retry=Retry(NoBackoff(), 3))
        origin_connect = conn._connect
        conn._connect = mock.Mock()

        def mock_connect():
            # connect only on the last retry
            if conn._connect.call_count <= 2:
                raise socket.timeout
            else:
                return origin_connect()

        conn._connect.side_effect = mock_connect
        conn.connect()
        assert conn._connect.call_count == 3
        self.clear(conn)

    def test_connect_without_retry_on_os_error(self):
        """Test that the _connect function is not being retried in case of a OSError"""
        with patch.object(Connection, "_connect") as _connect:
            _connect.side_effect = OSError("")
            conn = Connection(retry_on_timeout=True, retry=Retry(NoBackoff(), 2))
            with pytest.raises(ConnectionError):
                conn.connect()
            assert _connect.call_count == 1
            self.clear(conn)

    def test_connect_timeout_error_without_retry(self):
        """Test that the _connect function is not being retried if retry_on_timeout is
        set to False"""
        conn = Connection(retry_on_timeout=False)
        conn._connect = mock.Mock()
        conn._connect.side_effect = socket.timeout

        with pytest.raises(TimeoutError) as e:
            conn.connect()
        assert conn._connect.call_count == 1
        assert str(e.value) == "Timeout connecting to server"
        self.clear(conn)

    @patch.object(Connection, 'send_command')
    @patch.object(Connection, 'read_response')
    def test_on_connect(self, mock_read_response, mock_send_command):
        """Test that the on_connect function sends the correct commands"""
        conn = Connection()

        conn._parser = MagicMock()
        conn._parser.on_connect.return_value = None
        conn.credential_provider = None
        conn.username = "myuser"
        conn.password = "password"
        conn.protocol = 3
        conn.client_name = "test-client"
        conn.lib_name = "test"
        conn.lib_version = "1234"
        conn.db = 0
        conn.client_cache = True

        # command response
        mock_read_response.side_effect = itertools.cycle([
            b'QUEUED',  # MULTI
            b'QUEUED',  # HELLO
            b'QUEUED',  # AUTH
            b'QUEUED',  # CLIENT SETNAME
            b'QUEUED',  # CLIENT SETINFO LIB-NAME
            b'QUEUED',  # CLIENT SETINFO LIB-VER
            b'QUEUED',  # SELECT
            b'QUEUED',  # CLIENT TRACKING ON
            [           # EXEC response list
                {"proto": 3, "version": "6"},
                b'OK',
                b'OK',
                b'OK',
                b'OK',
                b'OK',
                b'OK',
                b'OK'
            ]
        ])

        conn.on_connect()

        mock_read_response.side_effect = itertools.repeat("OK")

    @patch.object(Connection, 'send_command')
    @patch.object(Connection, 'read_response')
    def test_on_connect_fail_hello(self, mock_read_response, mock_send_command):
        """Test that on_connect handles connection failure HELLO command"""
        conn = Connection()

        conn._parser = MagicMock()
        conn._parser.on_connect.return_value = None
        conn.credential_provider = None
        conn.username = "myuser"
        conn.password = "password"
        conn.protocol = -1  # invalid protocol
        conn.client_name = "test-client"
        conn.lib_name = "test"
        conn.lib_version = "1234"
        conn.db = 0
        conn.client_cache = True

        # simulate a failure in the HELLO command response
        mock_read_response.side_effect = itertools.cycle([
            Exception("Invalid RESP version"),  # HELLO (fails)
            b'QUEUED',                          # MULTI
        ])

        with self.assertRaises(ConnectionError):
            conn.on_connect()

        mock_send_command.assert_any_call('HELLO', -1, 'AUTH', 'myuser', 'password'),

        mock_send_command.assert_called()
        mock_read_response.assert_called()

    @patch.object(Connection, 'send_command')
    @patch.object(Connection, 'read_response')
    def test_on_connect_fail_auth(self, mock_read_response, mock_send_command):
        """Test that on_connect handles connection failure AUTH command"""
        conn = Connection()

        conn._parser = MagicMock()
        conn._parser.on_connect.return_value = None
        conn.credential_provider = None
        conn.username = "myuser"
        conn.password = "wrong-password"
        conn.protocol = 3
        conn.client_name = "test-client"
        conn.lib_name = "test"
        conn.lib_version = "1234"
        conn.db = 1
        conn.client_cache = True

        # simulate a failure in the HELLO command response
        mock_read_response.side_effect = itertools.cycle([
            {"proto": 3, "version": "6"},   # HELLO
            b'QUEUED',  # MULTI
            b'QUEUED',  # AUTH
            b'QUEUED',  # CLIENT SETNAME
            b'QUEUED',  # CLIENT SETINFO LIB-NAME
            b'QUEUED',  # CLIENT SETINFO LIB-VER
            b'QUEUED',  # SELECT
            b'QUEUED',  # CLIENT TRACKING ON
            [
                {"proto": 3, "version": "6"},  # HELLO response
                b'ERR invalid password',  # AUTH response
                b'OK',  # CLIENT SETNAME response
                b'OK',  # CLIENT SETINFO LIB-NAME response
                b'OK',  # CLIENT SETINFO LIB-VER response
                b'OK',  # SELECT response
                b'OK'   # CLIENT TRACKING ON response
            ]
        ])

        with self.assertRaises(AuthenticationError):
            conn.on_connect()

        mock_send_command.assert_any_call(
            'HELLO', 3, 'AUTH', 'myuser', 'wrong-password'),
        mock_send_command.assert_any_call('CLIENT', 'SETNAME', 'test-client'),
        mock_send_command.assert_any_call('CLIENT', 'SETINFO', 'LIB-NAME', 'test'),
        mock_send_command.assert_any_call('CLIENT', 'SETINFO', 'LIB-VER', '1234'),
        mock_send_command.assert_any_call('SELECT', 1),
        mock_send_command.assert_any_call('CLIENT', 'TRACKING', 'ON'),
        mock_send_command.assert_any_call('EXEC')

        mock_send_command.assert_called()
        mock_read_response.assert_called()

    @patch.object(Connection, 'send_command')
    @patch.object(Connection, 'read_response')
    def test_on_connect_auth_with_password_only(
            self, mock_read_response, mock_send_command):
        """Test on_connect handling of password-only AUTH for Redis versions below 6.0.0 without HELLO command"""
        conn = Connection()

        conn._parser = MagicMock()
        conn._parser.on_connect.return_value = None
        conn.credential_provider = None
        conn.username = None
        conn.password = "password"
        conn.protocol = 1
        conn.client_name = "test-client"
        conn.lib_name = "test"
        conn.lib_version = "1234"
        conn.db = 1
        conn.client_cache = True

        # command response to simulate Redis < 6.0.0 behavior
        mock_read_response.side_effect = itertools.cycle([
            Exception("ERR HELLO"),  # HELLO (fails)
            b'QUEUED',  # MULTI
            b'QUEUED',  # AUTH
            b'QUEUED',  # CLIENT SETNAME
            b'QUEUED',  # CLIENT SETINFO LIB-NAME
            b'QUEUED',  # CLIENT SETINFO LIB-VER
            b'QUEUED',  # SELECT
            b'QUEUED',  # CLIENT TRACKING ON
            [
                b'OK',                           # AUTH response
                b'OK',                           # CLIENT SETNAME response
                b'OK',                           # CLIENT SETINFO LIB-NAME response
                b'OK',                           # CLIENT SETINFO LIB-VER response
                b'OK',                           # SELECT response
                b'OK'                            # CLIENT TRACKING ON response
            ]
        ])

        conn.on_connect()

        mock_send_command.assert_any_call('HELLO', 1, 'AUTH', 'default', 'password'),
        mock_send_command.assert_any_call('MULTI'),
        mock_send_command.assert_any_call(
            'AUTH', 'default', 'password', check_health=False)
        mock_send_command.assert_any_call('CLIENT', 'SETNAME', 'test-client')
        mock_send_command.assert_any_call('CLIENT', 'SETINFO', 'LIB-NAME', 'test')
        mock_send_command.assert_any_call('CLIENT', 'SETINFO', 'LIB-VER', '1234')
        mock_send_command.assert_any_call('SELECT', 1)
        mock_send_command.assert_any_call('CLIENT', 'TRACKING', 'ON')
        mock_send_command.assert_any_call('EXEC')
        mock_read_response.assert_called()


@pytest.mark.onlynoncluster
@pytest.mark.parametrize(
    "parser_class",
    [_RESP2Parser, _RESP3Parser, _HiredisParser],
    ids=["RESP2Parser", "RESP3Parser", "HiredisParser"],
)
def test_connection_parse_response_resume(r: redis.Redis, parser_class):
    """
    This test verifies that the Connection parser,
    be that PythonParser or HiredisParser,
    can be interrupted at IO time and then resume parsing.
    """
    if parser_class is _HiredisParser and not HIREDIS_AVAILABLE:
        pytest.skip("Hiredis not available)")
    args = dict(r.connection_pool.connection_kwargs)
    args["parser_class"] = parser_class
    conn = Connection(**args)
    conn.connect()
    message = (
        b"*3\r\n$7\r\nmessage\r\n$8\r\nchannel1\r\n"
        b"$25\r\nhi\r\nthere\r\n+how\r\nare\r\nyou\r\n"
    )
    mock_socket = MockSocket(message, interrupt_every=2)

    if isinstance(conn._parser, _RESP2Parser) or isinstance(conn._parser, _RESP3Parser):
        conn._parser._buffer._sock = mock_socket
    else:
        conn._parser._sock = mock_socket
    for i in range(100):
        try:
            response = conn.read_response(disconnect_on_error=False)
            break
        except MockSocket.TestError:
            pass

    else:
        pytest.fail("didn't receive a response")
    assert response
    assert i > 0


@pytest.mark.onlynoncluster
@pytest.mark.parametrize(
    "Class",
    [
        Connection,
        SSLConnection,
        UnixDomainSocketConnection,
    ],
)
def test_pack_command(Class):
    """
    This test verifies that the pack_command works
    on all supported connections. #2581
    """
    cmd = (
        "HSET",
        "foo",
        "key",
        "value1",
        b"key_b",
        b"bytes str",
        b"key_i",
        67,
        "key_f",
        3.14159265359,
    )
    expected = (
        b"*10\r\n$4\r\nHSET\r\n$3\r\nfoo\r\n$3\r\nkey\r\n$6\r\nvalue1\r\n"
        b"$5\r\nkey_b\r\n$9\r\nbytes str\r\n$5\r\nkey_i\r\n$2\r\n67\r\n$5"
        b"\r\nkey_f\r\n$13\r\n3.14159265359\r\n"
    )

    actual = Class().pack_command(*cmd)[0]
    assert actual == expected, f"actual = {actual}, expected = {expected}"


@pytest.mark.onlynoncluster
def test_create_single_connection_client_from_url():
    client = redis.Redis.from_url(
        "redis://localhost:6379/0?", single_connection_client=True
    )
    assert client.connection is not None


@pytest.mark.parametrize("from_url", (True, False), ids=("from_url", "from_args"))
def test_pool_auto_close(request, from_url):
    """Verify that basic Redis instances have auto_close_connection_pool set to True"""

    url: str = request.config.getoption("--redis-url")
    url_args = parse_url(url)

    def get_redis_connection():
        if from_url:
            return Redis.from_url(url)
        return Redis(**url_args)

    r1 = get_redis_connection()
    assert r1.auto_close_connection_pool is True
    r1.close()


@pytest.mark.parametrize("from_url", (True, False), ids=("from_url", "from_args"))
def test_redis_connection_pool(request, from_url):
    """Verify that basic Redis instances using `connection_pool`
    have auto_close_connection_pool set to False"""

    url: str = request.config.getoption("--redis-url")
    url_args = parse_url(url)

    pool = None

    def get_redis_connection():
        nonlocal pool
        if from_url:
            pool = ConnectionPool.from_url(url)
        else:
            pool = ConnectionPool(**url_args)
        return Redis(connection_pool=pool)

    called = 0

    def mock_disconnect(_):
        nonlocal called
        called += 1

    with patch.object(ConnectionPool, "disconnect", mock_disconnect):
        with get_redis_connection() as r1:
            assert r1.auto_close_connection_pool is False

    assert called == 0
    pool.disconnect()


@pytest.mark.parametrize("from_url", (True, False), ids=("from_url", "from_args"))
def test_redis_from_pool(request, from_url):
    """Verify that basic Redis instances created using `from_pool()`
    have auto_close_connection_pool set to True"""

    url: str = request.config.getoption("--redis-url")
    url_args = parse_url(url)

    pool = None

    def get_redis_connection():
        nonlocal pool
        if from_url:
            pool = ConnectionPool.from_url(url)
        else:
            pool = ConnectionPool(**url_args)
        return Redis.from_pool(pool)

    called = 0

    def mock_disconnect(_):
        nonlocal called
        called += 1

    with patch.object(ConnectionPool, "disconnect", mock_disconnect):
        with get_redis_connection() as r1:
            assert r1.auto_close_connection_pool is True

    assert called == 1
    pool.disconnect()

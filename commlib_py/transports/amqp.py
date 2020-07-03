from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals
)

import functools
import sys

if sys.version_info[0] >= 3:
    unicode = str

import time
import atexit
import signal
import json
import uuid
import pika
from collections import deque
from threading import Semaphore
#  import ssl

from commlib_py.logger import create_logger, LoggingLevel
from commlib_py.serializer import JSONSerializer, ContentType
from commlib_py.rpc import AbstractRPCServer, AbstractRPCClient
from commlib_py.pubsub import AbstractPublisher, AbstractSubscriber


class MessageProperties(pika.BasicProperties):
    """Message Properties/Attribures used for sending and receiving messages.

    Args:
        content_type (str):
        content_encoding (str):
        timestamp (str):

    """
    def __init__(self, content_type=None, content_encoding=None,
                 timestamp=None, correlation_id=None, reply_to=None,
                 message_id=None, user_id=None, app_id=None):
        """Constructor."""
        if timestamp is None:
            timestamp = (time.time() + 0.5) * 1000
        timestamp = int(timestamp)
        super(MessageProperties, self).__init__(
            content_type=content_type,
            content_encoding=content_encoding,
            timestamp=timestamp,
            correlation_id=correlation_id,
            reply_to=reply_to,
            message_id=str(message_id) if message_id is not None else None,
            user_id=str(user_id) if user_id is not None else None,
            app_id=str(app_id) if app_id is not None else None
        )


class ConnectionParameters(pika.ConnectionParameters):
    """AMQP Connection parameters.

    Args:
        host (str): Hostname of AMQP broker to connect to.
        port (int|str): AMQP broker listening port.
        creds (object): Auth Credentials - Credentials instance.
        secure (bool): Enable SSL/TLS (AMQPS) - Not supported!!
        reconnect_attempts (int): The reconnection attempts to make before
            droping and raising an Exception.
        retry_delay (float): Time delay between reconnect attempts.
        timeout (float): Socket Connection timeout value.
        timeout (float): Blocked Connection timeout value.
            Set the timeout, in seconds, that the connection may remain blocked
            (triggered by Connection.Blocked from broker). If the timeout
            expires before connection becomes unblocked, the connection will
            be torn down.
        heartbeat_timeout (int): Controls AMQP heartbeat
            timeout negotiation during connection tuning. An integer value
            always overrides the value proposed by broker. Use 0 to deactivate
            heartbeats and None to always accept the broker's proposal.
            The value passed for timeout is also used to calculate an interval
            at which a heartbeat frame is sent to the broker. The interval is
            equal to the timeout value divided by two.
        channel_max (int): The max permissible number of channels per
            connection. Defaults to 128.
    """

    __slots__ = [
        'host', 'port', 'secure', 'vhost', 'reconnect_attempts', 'retry_delay',
        'timeout', 'heartbeat_timeout', 'blocked_connection_timeout', 'creds'
    ]

    def __init__(self, host='127.0.0.1', port='5672', creds=None,
                 secure=False, vhost='/', reconnect_attempts=5,
                 retry_delay=2.0, timeout=120, blocked_connection_timeout=None,
                 heartbeat_timeout=60, channel_max=128):
        """Constructor."""
        self.host = host
        self.port = port
        self.secure = secure
        self.vhost = vhost
        self.reconnect_attempts = reconnect_attempts
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.blocked_connection_timeout = blocked_connection_timeout
        self.heartbeat_timeout = heartbeat_timeout
        self.channel_max = channel_max

        if creds is None:
            creds = Credentials()

        super(ConnectionParameters, self).__init__(
            host=host,
            port=str(port),
            credentials=creds,
            connection_attempts=reconnect_attempts,
            retry_delay=retry_delay,
            blocked_connection_timeout=blocked_connection_timeout,
            socket_timeout=timeout,
            virtual_host=vhost,
            heartbeat=heartbeat_timeout,
            channel_max=channel_max)

    def __str__(self):
        _properties = {
            'host': self.host,
            'port': self.port,
            'vhost': self.vhost,
            'reconnect_attempts': self.reconnect_attempts,
            'retry_delay': self.retry_delay,
            'timeout': self.timeout,
            'blocked_connection_timeout': self.blocked_connection_timeout,
            'heartbeat_timeout': self.heartbeat_timeout,
            'channel_max': self.channel_max
        }
        _str = json.dumps(_properties)
        return _str


class AMQPConnection(pika.BlockingConnection):
    """Connection. Thin wrapper around pika.BlockingConnection"""
    def __init__(self, conn_params):
        self._connection_params = conn_params
        self._pika_connection = None
        super(AMQPConnection, self).__init__(
            parameters=self._connection_params)


class ExchangeTypes(object):
    """AMQP Exchange Types."""
    Topic = 'topic'
    Direct = 'direct'
    Fanout = 'fanout'
    Default = ''


class Credentials(pika.PlainCredentials):
    """Connection credentials for authn/authz.

    Args:
        username (str): The username.
        password (str): The password (Basic Authentication).
    """

    __slots__ = ['username', 'password']

    def __init__(self, username='guest', password='guest'):
        """Constructor."""
        super(Credentials, self).__init__(username=username, password=password)


class AMQPTransport(object):
    """AMQPT Transport implementation.
    """

    def __init__(self, connection_params, debug=False, logger=None):
        """Constructor."""
        self._closing = False
        self._connection = None
        self._channel = None

        self._debug = debug

        self._connection_params = ConnectionParameters() if \
            connection_params is None else connection_params

        self._logger = create_logger(self.__class__.__name__) if \
            logger is None else logger

        assert isinstance(self._debug, bool)
        assert isinstance(self._connection_params, ConnectionParameters)

        # So that connections do not go zombie
        atexit.register(self._graceful_shutdown)

    @property
    def logger(self):
        return self._logger

    @property
    def channel(self):
        return self._channel

    @property
    def connection(self):
        return self._connection

    @property
    def debug(self):
        """Debug mode flag."""
        return self._debug

    @debug.setter
    def debug(self, val):
        if not isinstance(val, bool):
            raise TypeError('Value should be boolean')
        self._debug = val
        if self._debug is True:
            self.logger.setLevel(LoggingLevel.DEBUG)
        else:
            self.logger.setLevel(LoggingLevel.INFO)

    def connect(self):
        """Connect to the AMQP broker. Creates a new channel."""
        try:
            # Create a new connection
            self._connection = AMQPConnection(self._connection_params)
            # Create a new communication channel
            self._channel = self._connection.channel()
            self.logger.info(
                    'Connected to AMQP broker @ [{}:{}, vhost={}]'.format(
                        self._connection_params.host,
                        self._connection_params.port,
                        self._connection_params.vhost))
        except pika.exceptions.ConnectionClosed:
            self.logger.debug('Connection timed out. Reconnecting...')
            self.connect()
        except pika.exceptions.AMQPConnectionError:
            self.logger.debug('Connection error. Reconnecting...')
            self.connect()

    def process_amqp_events(self):
        """Force process amqp events, such as heartbeat packages."""
        self.connection.process_data_events()

    def _signal_handler(self, signum, frame):
        """TODO"""
        self.logger.info('Signal received: {}'.format(signum))
        self._graceful_shutdown()

    def _graceful_shutdown(self):
        if not self._connection:
            return
        if self._channel.is_closed:
            # self.logger.warning('Channel is allready closed')
            return
        self.logger.debug('Invoking a graceful shutdown...')
        self._channel.stop_consuming()
        self._channel.close()
        self.logger.debug('Channel closed!')

    def exchange_exists(self, exchange_name):
        resp = self._channel.exchange_declare(
            exchange=exchange_name,
            passive=True,  # Perform a declare or just to see if it exists
        )
        self.logger.debug('Exchange exists result: {}'.format(resp))
        return resp

    def create_exchange(self, exchange_name, exchange_type, internal=None):
        """
        Create a new exchange.

        @param exchange_name: The name of the exchange (e.g. com.logging).
        @type exchange_name: string

        @param exchange_type: The type of the exchange (e.g. 'topic').
        @type exchange_type: string
        """
        self._channel.exchange_declare(
            exchange=exchange_name,
            durable=True,  # Survive reboot
            passive=False,  # Perform a declare or just to see if it exists
            internal=internal,  # Can only be published to by other exchanges
            exchange_type=exchange_type
        )

        self.logger.debug('Created exchange: [name={}, type={}]'.format(
            exchange_name, exchange_type))

    def create_queue(self, queue_name='', exclusive=True, queue_size=10,
                     message_ttl=60000, overflow_behaviour='drop-head',
                     expires=600000):
        """
        Create a new queue.

        @param queue_name: The name of the queue.
        @type queue_name: string

        @param exclusive: Only allow access by the current connection.
        @type exclusive: bool

        @param queue_size: The size of the queue
        @type queue_size: int

        @param message_ttl: Per-queue message time-to-live
            (https://www.rabbitmq.com/ttl.html#per-queue-message-ttl)
        @type message_ttl: int

        @param overflow_behaviour: Overflow behaviour - 'drop-head' ||
            'reject-publish'.
            https://www.rabbitmq.com/maxlength.html#overflow-behaviour
        @type overflow_behaviour: str

        @param expires: Queues will expire after a period of time only
            when they are not used (e.g. do not have consumers).
            This feature can be used together with the auto-delete
            queue property. The value is expressed in milliseconds (ms).
            Default value is 10 minutes.
            https://www.rabbitmq.com/ttl.html#queue-ttl
        """
        args = {
            'x-max-length': queue_size,
            'x-overflow': overflow_behaviour,
            'x-message-ttl': message_ttl,
            'x-expires': expires
        }

        result = self._channel.queue_declare(
            exclusive=exclusive,
            queue=queue_name,
            durable=False,
            auto_delete=True,
            arguments=args)
        queue_name = result.method.queue
        self.logger.debug('Created queue [{}] [size={}, ttl={}]'.format(
            queue_name, queue_size, message_ttl))
        return queue_name

    def delete_queue(self, queue_name):
        self._channel.queue_delete(queue=queue_name)

    def _queue_exists_clb(self, arg):
        print(arg)

    def queue_exists(self, queue_name):
        """Check if a queue exists, given its name.

        Args:
            queue_name (str): The name of the queue.

        Returns:
            int: True if queue exists False otherwise.
        """
        # resp = self._channel.queue_declare(queue_name, passive=True,
        #                                    callback=self._queue_exists_clb)
        try:
            _ = self._channel.queue_declare(queue_name, passive=True)
        except pika.exceptions.ChannelClosedByBroker as exc:
            self.connect()
            if exc.reply_code == 404:  # Not Found
                return False
            else:
                self.logger.warning('Queue exists <{}>'.format(queue_name))
                return True

    def bind_queue(self, exchange_name, queue_name, bind_key):
        """
        Bind a queue to and exchange using a bind-key.

        @param exchange_name: The name of the exchange (e.g. com.logging).
        @type exchange_name: string

        @param queue_name: The name of the queue.
        @type queue_name: string

        @param bind_key: The binding key name.
        @type bind_key: string
        """
        self.logger.info('Subscribed to topic: {}'.format(bind_key))
        try:
            self._channel.queue_bind(
                exchange=exchange_name, queue=queue_name, routing_key=bind_key)
        except Exception as exc:
            raise exc

    def set_channel_qos(self, prefetch_count=1, global_qos=False):
        self._channel.basic_qos(prefetch_count=prefetch_count,
                                global_qos=global_qos)

    def consume_fromm_queue(self, queue_name, callback):
        consumer_tag = self._channel.basic_consume(queue_name, callback)
        return consumer_tag

    def start_consuming(self):
        self.channel.start_consuming()

    def stop_consuming(self):
        self.channel.stop_consuming()

    def close(self):
        self._graceful_shutdown()

    def disconnect(self):
        self._graceful_shutdown()

    def __del__(self):
        self._graceful_shutdown()


class RPCServer(AbstractRPCServer):
    """AMQP RPC Server class.
    Implements an AMQP RPC Server.

    Args:
        rpc_name (str): The name of the RPC.
        exchange (str): The exchange to bind the RPC.
            Defaults to (AMQT default).
        on_request (function): The on-request callback function to register.
    """
    def __init__(self, conn_params=None, exchange='', *args, **kwargs):
        """Constructor. """
        self._exchange = exchange
        super(RPCServer, self).__init__(*args, **kwargs)
        conn_params = ConnectionParameters() if \
            conn_params is None else conn_params

        self._transport = AMQPTransport(conn_params, self.debug, self.logger)

    @property
    def connection(self):
        return self._transport.connection

    @property
    def channel(self):
        return self._transport.channel

    def is_alive(self):
        """Returns True if connection is alive and False otherwise."""
        if self.connection is None:
            return False
        elif self.connection.is_open:
            return True
        else:
            return False

    def run_forever(self, raise_if_exists=True):
        """Run RPC Server in normal mode. Blocking function."""
        self._transport.connect()
        if self._rpc_exists() and raise_if_exists:
            raise ValueError(
                'RPC <{}> allready registered on broker.'.format(
                    self._rpc_name))
        self._rpc_queue = self._transport.create_queue(self._rpc_name)
        self._transport.set_channel_qos()
        self._transport.consume_fromm_queue(self._rpc_queue,
                                            self._on_request_wrapper)
        try:
            self._transport.start_consuming()
        except Exception as exc:
            self.logger.error(exc, exc_info=True)
            raise exc

    def _rpc_exists(self):
        return self._transport.queue_exists(self._rpc_name)

    def _on_request_wrapper(self, ch, method, properties, body):
        _msg = {}
        _ctype = None
        _cencoding = None
        _ts_send = None
        _ts_broker = None
        _dmode = None
        _corr_id = None
        try:
            _corr_id = properties.correlation_id
            _ctype = properties.content_type
            _cencoding = properties.content_encoding
            _ts_broker = properties.headers['timestamp_in_ms']
            _dmode = properties.delivery_mode
            _ts_send = properties.timestamp
            # _ts_broker = properties.timestamp
        except Exception:
            self.logger.error("Could not calculate latency",
                              exc_info=False)

        try:
            _msg = self._deserialize_data(body, _ctype, _cencoding)
        except Exception:
            self.logger.error("Could not deserialize data",
                              exc_info=True)
            # Return data as is. Let callback handle with encoding...
            _msg = body


        if self.on_request is not None:
            _meta = {
                'channel': ch,
                'method': method,
                'properties': {
                    'content_type': _ctype,
                    'content_encoding': _cencoding,
                    'timestamp_broker': _ts_broker,
                    'timestamp_producer': _ts_send,
                    'delivery_mode': _dmode,
                    'correlation_id': _corr_id
                }
            }
            resp = self.on_request(_msg, _meta)
        else:
            resp = {
                'error': 'Not Implemented',
                'status': 501
            }

        try:
            _payload = None
            _encoding = None
            _type = None

            if isinstance(resp, dict):
                _payload = self._serializer.serialize(resp).encode('utf-8')
                _encoding = self._serializer.CONTENT_ENCODING
                _type = self._serializer.CONTENT_TYPE
            elif isinstance(resp, str):
                _type = 'text/plain'
                _encoding = 'utf8'
                _payload = resp
            elif isinstance(resp, bytes):
                _type = 'application/octet-stream'
                _encoding = 'utf8'
                _payload = resp

        except Exception as e:
            self.logger.error("Could not deserialize data",
                              exc_info=True)
            _payload = {
                'status': 501,
                'error': 'Internal server error: {}'.format(str(e))
            }

        _msg_props = MessageProperties(
            content_type=_type,
            content_encoding=_encoding,
            correlation_id=_corr_id
        )

        ch.basic_publish(
            exchange=self._exchange,
            routing_key=properties.reply_to,
            properties=_msg_props,
            body=_payload)
        # Acknowledge receiving the message.
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def _deserialize_data(self, data, content_type, content_encoding):
        """Deserialize wire data.

        Args:
            data (str|dict): Data to deserialize.
            content_encoding (str): The content encoding.
            content_type (str): The content type. Defaults to `utf8`.
        """
        _data = None
        if content_encoding is None:
            content_encoding = 'utf8'
        if content_type == ContentType.json:
            _data = self._serializer.deserialize(data)
        elif content_type == ContentType.text:
            _data = data.decode(content_encoding)
        elif content_type == ContentType.raw_bytes:
            _data = data
        else:
            self.logger.warning(
                    'Content-Type was not set in headers or is invalid!' + \
                            ' Deserializing using default JSON serializer')
            ## TODO: Not the proper way!!!!
            _data = self._serializer.deserialize(data)
        return _data

    def close(self):
        """Stop RPC Server.
        Safely close channel and connection to the broker.
        """
        if not self.channel:
            return
        if self.channel.is_closed:
            self.logger.warning('Channel was already closed!')
            return False
        self._transport.stop_consuming()
        self._transport.delete_queue(self._rpc_queue)
        return True

    def stop(self):
        """Stop RPC Server.
        Safely close channel and connection to the broker.
        """
        return self.close()

    def __del__(self):
        self.close()

    def __exit__(self, exc_type, value, traceback):
        self.close()


class RPCClient(AbstractRPCClient):
    """AMQP RPC Client class.

    Args:
        rpc_name (str): The name of the RPC.
        **kwargs: The Keyword arguments to pass to  the base class
            (AMQPTransportSync).
    """
    def __init__(self, conn_params=None, use_corr_id=False, *args, **kwargs):
        """Constructor."""
        self._use_corr_id = use_corr_id
        self._corr_id = None
        self._response = None
        self._exchange = ExchangeTypes.Default
        self._mean_delay = 0
        self._delay = 0
        self.onresponse = None

        super(RPCClient, self).__init__(*args, *kwargs)

        conn_params = ConnectionParameters() if \
            conn_params is None else conn_params

        self._transport = AMQPTransport(conn_params, self._debug, self._logger)

        self._transport.connect()

        self._consumer_tag = self.channel.basic_consume(
            'amq.rabbitmq.reply-to',
            self._on_response,
            exclusive=False,
            consumer_tag=None,
            auto_ack=True)

    @property
    def logger(self):
        return self._logger

    @property
    def connection(self):
        return self._transport.connection

    @property
    def channel(self):
        return self._transport.channel

    @property
    def mean_delay(self):
        """The mean delay of the communication. Internally calculated."""
        return self._mean_delay

    @property
    def delay(self):
        """The last recorded delay of the communication.
            Internally calculated.
        """
        return self._delay

    def _on_response(self, ch, method, properties, body):
        _ctype = None
        _cencoding = None
        _ts_send = None
        _ts_broker = 0
        _dmode = None
        _msg = None
        _meta = None
        try:
            if self._use_corr_id:
                _corr_id = properties.correlation_id
                if self._corr_id != _corr_id:
                    return
            _ctype = properties.content_type
            _cencoding = properties.content_encoding
            if hasattr(self, 'headers'):
                if 'timestamp_in_ms' in properties.headers:
                    _ts_broker = properties.headers['timestamp_in_ms']

            _dmode = properties.delivery_mode
            _ts_send = properties.timestamp

            _meta = {
                'channel': ch,
                'method': method,
                'properties': {
                    'content_type': _ctype,
                    'content_encoding': _cencoding,
                    'timestamp_broker': _ts_broker,
                    'timestamp_producer': _ts_send,
                    'delivery_mode': _dmode
                }
            }
        except Exception:
            self.logger.error("Error parsing response from rpc server.",
                              exc_info=True)

        try:
            _msg = self._deserialize_data(body, _ctype, _cencoding)
        except Exception:
            self.logger.error("Could not deserialize data",
                              exc_info=True)
            _msg = body


        self._response = _msg
        self._response_meta = _meta

        if self.onresponse is not None and callable(self.onresponse):
            self.onresponse(_msg, _meta)

    def gen_corr_id(self):
        """Generate correlationID."""
        return str(uuid.uuid4())

    def call(self, payload, timeout=5.0):
        """Call RPC.

        Args:
            msg (dict|Message): The message to send.
            timeout (float): Response timeout. Set this value carefully
                based on application criteria.
        """
        self._response = None
        if self._use_corr_id:
            self._corr_id = self.gen_corr_id()
        self._send_data(payload)
        start_t = time.time()
        self._wait_for_response(timeout)
        ## TODO: Validate correlation_id
        elapsed_t = time.time() - start_t
        self._delay = elapsed_t

        if self._response is None:
            resp = {'error': 'RPC Response timeout'}
        else:
            resp = self._response
        return resp

    def _wait_for_response(self, timeout):
        self.logger.debug('Waiting for response from [%s]...', self._rpc_name)
        self.connection.process_data_events(time_limit=timeout)

    def _deserialize_data(self, data, content_type, content_encoding):
        """Deserialize wire data.

        Args:
            data: Data to deserialize.
            content_encoding (str): The content encoding.
            content_type (str): The content type. Defaults to `utf8`
        """
        _data = None
        if content_encoding is None:
            content_encoding = 'utf8'
        if content_type == ContentType.json:
            _data = self._serializer.deserialize(data)
        elif content_type == ContentType.text:
            _data = data.decode(content_encoding)
        elif content_type == ContentType.raw_bytes:
            _data = data
        return _data

    def _send_data(self, data):
        _payload = None
        _encoding = None
        _type = None

        if isinstance(data, dict):
            _payload = self._serializer.serialize(data).encode('utf-8')
            _encoding = self._serializer.CONTENT_ENCODING
            _type = self._serializer.CONTENT_TYPE
        elif isinstance(data, str):
            _type = 'text/plain'
            _encoding = 'utf8'
            _payload = data
        elif isinstance(data, bytes):
            _type = 'application/octet-stream'
            _encoding = 'utf8'
            _payload = data

        # Direct reply-to implementation
        _rpc_props = MessageProperties(
            content_type=_type,
            content_encoding=_encoding,
            correlation_id=self._corr_id,
            # timestamp=(1.0 * (time.time() + 0.5) * 1000),
            message_id=0,
            # user_id="",
            # app_id="",
            reply_to='amq.rabbitmq.reply-to'
        )

        self.channel.basic_publish(
            exchange=self._exchange,
            routing_key=self._rpc_name,
            mandatory=False,
            properties=_rpc_props,
            body=_payload)


class Publisher(AbstractPublisher):
    """Publisher class.

    Args:
        topic (str): The topic uri to publish data.
        exchange (str): The exchange to publish data.
        **kwargs: The keyword arguments to pass to the base class
            (AMQPTransportSync).
    """

    def __init__(self, conn_params=None, exchange='amq.topic', *args, **kwargs):
        """Constructor."""
        self._topic_exchange = exchange

        super(Publisher, self).__init__(*args, **kwargs)

        conn_params = ConnectionParameters() if \
            conn_params is None else conn_params

        self._transport = AMQPTransport(conn_params, self.debug, self.logger)
        self._transport.connect()
        self._transport.create_exchange(self._topic_exchange,
                                        ExchangeTypes.Topic)

    def publish(self, payload):
        """ Publish message once.

        Args:
            msg (dict|Message|str|bytes): Message/Data to publish.
        """
        ## Thread Safe solution
        self._transport.connection.add_callback_threadsafe(
            functools.partial(self._send_data, payload))
        # self._send_data(payload)
        self._transport.process_amqp_events()

    def _send_data(self, data):
        _payload = None
        _encoding = None
        _type = None

        if isinstance(data, dict):
            _payload = self._serializer.serialize(data).encode('utf-8')
            _encoding = self._serializer.CONTENT_ENCODING
            _type = self._serializer.CONTENT_TYPE
        elif isinstance(data, str):
            _type = 'text/plain'
            _encoding = 'utf8'
            _payload = data
        elif isinstance(data, bytes):
            _type = 'application/octet-stream'
            _encoding = 'utf8'
            _payload = data

        msg_props = MessageProperties(
            content_type=_type,
            content_encoding=_encoding,
            message_id=0,
        )

        self._transport._channel.basic_publish(
            exchange=self._topic_exchange,
            routing_key=self._topic,
            properties=msg_props,
            body=_payload)
        self.logger.debug('Sent message to topic <{}>'.format(self._topic))


class Subscriber(AbstractSubscriber):
    """Subscriber class.
    Implements the Subscriber endpoint of the PubSub communication pattern.

    Args:
        topic (str): The topic uri.
        on_message (function): The callback function. This function
            is fired when messages arrive at the registered topic.
        exchange (str): The name of the exchange. Defaults to `amq.topic`
        queue_size (int): The maximum queue size of the topic.
        message_ttl (int): Message Time-to-Live as specified by AMQP.
        overflow (str): queue overflow behavior. Specified by AMQP Protocol.
            Defaults to `drop-head`.
        **kwargs: The keyword arguments to pass to the base class
            (AMQPTransportSync).
    """

    FREQ_CALC_SAMPLES_MAX = 100

    def __init__(self, conn_params=None, exchange='amq.topic', queue_size=10,
                 message_ttl=60000, overflow='drop-head', *args, **kwargs):
        """Constructor."""
        self._topic_exchange = exchange
        self._queue_name = None
        self._queue_size = queue_size
        self._message_ttl = message_ttl
        self._overflow = overflow

        super(Subscriber, self).__init__(*args, **kwargs)

        conn_params = ConnectionParameters() if \
            conn_params is None else conn_params

        self._transport = AMQPTransport(conn_params, self.debug, self.logger)
        self._transport.connect()

        _exch_ex = self._transport.exchange_exists(self._topic_exchange)
        if _exch_ex.method.NAME != 'Exchange.DeclareOk':
            self._transport.create_exchange(self._topic_exchange,
                                            ExchangeTypes.Topic)

        # Create a queue. Set default idle expiration time to 5 mins
        self._queue_name = self._transport.create_queue(
            queue_size=self._queue_size,
            message_ttl=self._message_ttl,
            overflow_behaviour=self._overflow,
            expires=300000)

        # Bind queue to the Topic exchange
        self._transport.bind_queue(self._topic_exchange, self._queue_name, self._topic)
        self._last_msg_ts = None
        self._msg_freq_fifo = deque(maxlen=self.FREQ_CALC_SAMPLES_MAX)
        self._hz = 0
        self._sem = Semaphore()

    @property
    def hz(self):
        """Incoming message frequency."""
        return self._hz

    def run_forever(self):
        """Start Subscriber. Blocking method."""
        self._consume()

    def close(self):
        if self._transport._channel.is_closed:
            self.logger.info('Invoked close() on an already closed channel')
            return False
        self._transport.delete_queue(self._queue_name)
        super(Subscriber, self).stop()

    def _consume(self, reliable=False):
        """Start AMQP consumer."""
        self._transport._channel.basic_consume(
            self._queue_name,
            self._on_msg_callback_wrapper,
            exclusive=False,
            auto_ack=(not reliable))
        try:
            self._transport._channel.start_consuming()
        except KeyboardInterrupt as exc:
            # Log error with traceback
            self.logger.error(exc, exc_info=False)
        except Exception as exc:
            self.logger.error(exc, exc_info=False)
            raise exc

    def _on_msg_callback_wrapper(self, ch, method, properties, body):
        msg = {}
        _ctype = None
        _cencoding = None
        _ts_send = None
        _ts_broker = None
        _dmode = None
        try:
            _ctype = properties.content_type
            _cencoding = properties.content_encoding
            _dmode = properties.delivery_mode
            _ts_broker = properties.headers['timestamp_in_ms']
            _ts_send = properties.timestamp
            # _ts_broker = properties.timestamp
        except Exception:
            self.logger.error("Could not calculate latency",
                              exc_info=False)

        try:
            msg = self._deserialize_data(body, _ctype, _cencoding)
        except Exception:
            self.logger.error("Could not deserialize data",
                              exc_info=True)
            # Return data as is. Let callback handle with encoding...
            msg = body

        try:
            self._sem.acquire()
            self._calc_msg_frequency()
            self._sem.release()
        except Exception:
            self.logger.error("Could not calculate message rate",
                              exc_info=True)

        if self._onmessage is not None:
            meta = {
                'channel': ch,
                'method': method,
                'properties': {
                    'content_type': _ctype,
                    'content_encoding': _cencoding,
                    'timestamp_broker': _ts_broker,
                    'timestamp_producer': _ts_send,
                    'delivery_mode': _dmode
                }
            }
            self._onmessage(msg, meta)

    def _calc_msg_frequency(self):
        ts = time.time()
        if self._last_msg_ts is not None:
            diff = ts - self._last_msg_ts
            if diff < 10e-3:
                self._last_msg_ts = ts
                return
            else:
                hz = 1.0 / float(diff)
                self._msg_freq_fifo.appendleft(hz)
                hz_list = [s for s in self._msg_freq_fifo if s != 0]
                _sum = sum(hz_list)
                self._hz = _sum / len(hz_list)
        self._last_msg_ts = ts

    def _deserialize_data(self, data, content_type, content_encoding):
        """
        Deserialize wire data.

        @param data: Data to deserialize.
        @type data: dict|int|bool
        """
        _data = None
        if content_encoding is None:
            content_encoding = 'utf8'
        if content_type == ContentType.json:
            _data = self._serializer.deserialize(data)
        elif content_type == ContentType.text:
            _data = data.decode(content_encoding)
        elif content_type == ContentType.raw_bytes:
            _data = data
        return _data

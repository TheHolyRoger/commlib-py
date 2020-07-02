# -*- coding: utf-8 -*-
# Copyright (C) 2020  Panayiotou, Konstantinos <klpanagi@gmail.com>
# Author: Panayiotou, Konstantinos <klpanagi@gmail.com>
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals
)

import functools
import time
import atexit
import signal
import json

import pika
#  import ssl

from commlib_py.logger import create_logger, LoggingLevel


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

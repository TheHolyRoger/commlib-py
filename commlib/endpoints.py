from enum import Enum


class EndpointType(Enum):
    RPCService = 1
    RPCClient = 2
    Publisher = 3
    Subscriber = 4
    ActionServer = 5
    ActionClient = 6


class TransportType(Enum):
    AMQP = 1
    REDIS = 2


def endpoint_factory(etype, etransport):
    if etransport == TransportType.AMQP:
        import commlib.transports.amqp as comm
    elif etransport == TransportType.REDIS:
        import commlib.transports.redis as comm
    else:
        raise ValueError()
    if etype == EndpointType.RPCService:
        return comm.RPCService
    elif etype == EndpointType.RPCClient:
        return comm.RPCClient
    elif etype == EndpointType.Publisher:
        return comm.Publisher
    elif etype == EndpointType.Subscriber:
        return comm.Subscriber
    elif etype == EndpointType.ActionServer:
        return comm.ActionServer
    elif etype == EndpointType.ActionClient:
        return comm.ActionClient

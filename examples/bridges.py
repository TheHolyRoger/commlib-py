#!/usr/bin/env python

import commlib.transports.amqp as acomm
import commlib.transports.redis as rcomm
from commlib.bridges import (
    RPCBridge, RPCBridgeType, TopicBridge, TopicBridgeType
)
import time


def on_request(msg, meta):
    print('RPC Service Request')
    return msg


def on_message(nsg, meta):
    print('Data received at topic.')


def redis_to_amqp_rpc_bridge():
    """
    [Broker A] ------------> [Broker B] ---> [Consumer Endpoint]
    """
    bA_params = rcomm.ConnectionParameters()
    bB_params = acomm.ConnectionParameters()
    bA_uri = 'rpc.bridge.testA'
    bB_uri = 'rpc.bridge.testB'
    br = RPCBridge(RPCBridgeType.REDIS_TO_AMQP, bA_uri, bB_uri,
                   bA_params, bB_params, debug=True)
    br.run()


    ## For Testing Bridge ------------------>
    client = rcomm.RPCClient(conn_params=bA_params, rpc_name=bA_uri)

    server = acomm.RPCService(
        conn_params=bB_params,
        rpc_name=bB_uri,
        on_request=on_request
    )
    server.run()

    count = 0
    while count < 5:
        resp = client.call({'a': 1})
        print(f'Response from AMQP RPC Service: {resp}')
        time.sleep(1)
        count += 1
    server.stop()
    ## <-------------------------------------
    br.stop()


if __name__ == '__main__':
    redis_to_amqp_rpc_bridge()

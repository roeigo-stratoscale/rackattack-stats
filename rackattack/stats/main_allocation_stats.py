import time
import pytz
import logging
import datetime
import threading
from functools import partial
from rackattack import clientfactory
from elasticsearch import Elasticsearch
from rackattack.tcp.subscribe import Subscribe


TIMEZONE = 'Asia/Jerusalem'
RABBITMQ_CONNECTNION_URL = r'amqp://guest:guest@rack01-server58:1013/%2F'


def datetime_from_timestamp(timestamp):
    global TIMEZONE
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(TIMEZONE).localize(datetime_now)
    return datetime_now


class AllocationsHandler(object):
    def __init__(self, db, rackattack_client):
        self._hosts_state = dict()
        self._db = db
        self._hosts_state_lock = threading.Lock()
        self._rackattack_client = rackattack_client
        global RABBITMQ_CONNECTNION_URL
        self._subscription_mgr = Subscribe(RABBITMQ_CONNECTNION_URL)
        self._subscription_mgr._readyEvent.wait()
        logging.info('Subscribing to all hosts allocations.')
        self._subscription_mgr.registerForAllHostsAllocations(
            self._all_allocations_handler)
        self._allocation_subscriptions = set()

    def _inauguration_handler(self, msg):
        logging.debug('Inaugurator message: {}'.format(msg))
        with self._hosts_state_lock:
            if 'id' not in msg:
                logging.error('_inauguration_handler: WTF msg={}.'.format(msg))
                return
            host_id = msg['id']
            try:
                host_state = self._hosts_state[host_id]
            except KeyError:
                logging.error('Got an inauguration message for a host without'
                              ' a known allocation: {}'.format(host_id))
                return

            if msg['status'] == 'done':
                host_state['end_timestamp'] = time.time()
                host_state['inauguration_done'] = True
                logging.info('Host "{}" has inaugurated, congratulations.'.format(host_id))
                self._add_allocation_record_to_db(host_id)
            elif msg['status'] == 'progress' and \
                    msg['progress']['state'] == 'fetching':
                chain_count = msg['progress']['chainGetCount']
                self._hosts_state[host_id]['latest_chain_count'] = chain_count

    def _ubsubscribe_allocation(self, allocation_idx):
        logging.info('Unsubscribing from allocation {}.'.format(allocation_idx))
        try:
            self._allocation_subscriptions.remove(allocation_idx)
        except KeyError:
            logging.info('Already ubsubscribed from allocation #{}.'.format(allocation_idx))

        try:
            self._subscription_mgr.unregisterForAllocation(allocation_idx)
        except AssertionError:
            logging.warning('Could not unsubscribe from allocation {}'.
                            format(allocation_idx))

        # Unregister all inaugurators
        hosts_to_remove = set()
        for host_id, host in self._hosts_state.iteritems():
            if host['allocation_idx'] == allocation_idx:
                hosts_to_remove.add(host_id)
                logging.info('Unsubscribing from inauguration events of "{}".'.format(host_id))
                try:
                    self._subscription_mgr.unregisterForInaugurator(host_id)
                except AssertionError:
                    logging.warning('Could not unregister from inauguration of '
                                    ' "{}".'.format(allocation_idx))

        for host_id in list(hosts_to_remove):
            del self._hosts_state[host_id]

    def _is_allocation_dead(self, allocation_idx):
        result = self._rackattack_client.call('allocation__dead', id=allocation_idx)
        if result is None:
            return False
        return result

    def _are_all_inaugurations_done(self, allocation_idx):
        for host in self._hosts_state.itervalues():
            if not host['inauguration_done']:
                return False
        return True

    def _allocation_handler(self, allocation_idx, msg):
        with self._hosts_state_lock:
            if msg.get('event', None) == "changedState":
                is_dead = self._is_allocation_dead(allocation_idx)
                if is_dead:
                    logging.info('Allocation {} has died of reason "{}"'.format(allocation_idx, is_dead))
                    self._ubsubscribe_allocation(allocation_idx)
                elif self._are_all_inaugurations_done(allocation_idx):
                    logging.info("Allocation {} has changed its state and it's still alive, but it does not"
                                 " wait for any more inaugurations, so unsubscribing from it.".
                                 format(allocation_idx))
                    self._ubsubscribe_allocation(allocation_idx)
                else:
                    logging.info("Allocation {} has changed its state and it's still alive and waiting for "
                                 " some inaugurations to complete.".format(allocation_idx))
                    self._ubsubscribe_allocation(allocation_idx)
            elif msg.get('event', None) == "providerMessage":
                logging.info("Rackattack provider says: %(message)s", dict(message=msg['message']))
            elif msg.get('event', None) == "withdrawn":
                logging.info("Rackattack provider widthdrew allocation: '%(message)s",
                             dict(message=msg['message']))
                self._ubsubscribe_allocation(allocation_idx)
            else:
                logging.error('_allocation_handler: WTF allocation_idx={}, msg={}.'.
                              format(allocation_idx, msg))

    def _all_allocations_handler(self, allocation):
        global subscription_mgr, subscribe, state
        with self._hosts_state_lock:
            allocation_idx = allocation[0]['allocationIndex']
            logging.debug('New allocation: {}.'.format(allocation))
            logging.info('Subscribing to new allocation (#{}).'.format(allocation_idx))
            allocation_handler = partial(self._allocation_handler, allocation_idx)
            self._subscription_mgr.registerForAllocation(allocation_idx, allocation_handler)
            self._allocation_subscriptions.add(allocation_idx)
            logging.info("Subscribing to allocation #{}'s hosts inauguration info.".format(allocation_idx))
            for host_idx, allocatedHost in enumerate(allocation):
                host_id = allocatedHost['host']
                # Update hosts state
                allocation_idx = allocatedHost['allocationIndex']
                self._hosts_state[host_id] = dict(start_timestamp=time.time(),
                                                  image_hint=allocatedHost['imageHint'],
                                                  image_label=allocatedHost['imageLabel'],
                                                  name=allocatedHost['name'],
                                                  allocation_idx=allocation_idx,
                                                  inauguration_done=False,
                                                  host_idx=host_idx)
                # Subecribe
                logging.info("Subscribing to inaugurator events of: {}.".
                             format(host_id))
                self._subscription_mgr.registerForInagurator(host_id, self._inauguration_handler)

    def _add_allocation_record_to_db(self, host_id):
        index = 'allocations_'
        doc_type = 'allocation_'

        state = self._hosts_state[host_id]
        record_datetime = datetime_from_timestamp(state['start_timestamp'])

        local_store_count = None
        remote_store_count = None
        try:
            chain_count = state['latest_chain_count']
            local_store_count = chain_count.pop(0)
            remote_store_count = chain_count.pop(0)
        except KeyError:
            # NO info abount Osmosis chain
            pass
        except IndexError:
            pass

        majorioty_chain_type = 'unknown'
        if local_store_count is not None:
            majorioty_chain_type = 'local'
            if remote_store_count is not None and \
                    local_store_count < remote_store_count:
                majorioty_chain_type = 'remote'

        inauguration_period_length = state['end_timestamp'] - \
            state['start_timestamp']
        id = "%d%03d%03d" % (state['start_timestamp'], state['allocation_idx'],
                             state['host_idx'])

        record = dict(timestamp=record_datetime,
                      _timestamp=record_datetime,
                      host_id=host_id,
                      image_label=state['image_label'],
                      image_hint=state['image_hint'],
                      inauguration_period_length=inauguration_period_length,
                      local_store_count=local_store_count,
                      remote_store_count=remote_store_count,
                      majorioty_chain_type=majorioty_chain_type,
                      name=state['name'],
                      allocation_idx=state['allocation_idx'])

        self._db.create(index=index, doc_type=doc_type, body=record, id=id)


def main():
    global subscription_mgr
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

    db = Elasticsearch()
    client = clientfactory.factory()
    handler = AllocationsHandler(db, client)

    a = threading.Event()
    a.wait()

if __name__ == '__main__':
    main()
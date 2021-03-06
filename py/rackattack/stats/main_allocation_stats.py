import os
import sys
import time
import pytz
import Queue
import signal
import pprint
import socket
import smtplib
import logging
import datetime
import threading
import traceback
import elasticsearch
from functools import partial
from email.mime.text import MIMEText
from rackattack.tcp import subscribe
from rackattack.stats import config
from rackattack.stats import logconfig
from rackattack.stats import events_monitor
from rackattack.stats import elasticsearchdbwrapper


MAX_NR_ALLOCATIONS = 150
EMAIL_SUBSCRIBERS = ("eliran@stratoscale.com",)
MAX_NR_SECONDS_WITHOUT_EVENTS_BEFORE_ALERTING = 60 * 60 * 6
SEND_ALERTS_BY_MAIL = True
SENDER_EMAIL = "eliran@stratoscale.com"
SMTP_SERVER = 'localhost'


def datetime_from_timestamp(timestamp):
    datetime_now = datetime.datetime.fromtimestamp(timestamp)
    datetime_now = pytz.timezone(config.TIMEZONE).localize(datetime_now)
    return datetime_now


def send_mail(msg):
    global SEND_ALERTS_BY_MAIL
    if not SEND_ALERTS_BY_MAIL:
        return
    msg = MIMEText(msg)
    msg['Subject'] = 'RackAttack Status Alert {}'.format(time.ctime())
    msg['From'] = SENDER_EMAIL
    msg['To'] = ",".join(EMAIL_SUBSCRIBERS)

    # Send the message via our own SMTP server, but don't include the
    # envelope header.
    try:
        s = smtplib.SMTP(SMTP_SERVER)
    except socket.error:
        msg = 'Cannot send email; Could not connect to an SMTP server at "{}".'.format(SMTP_SERVER)
        logging.exception(msg)
        return
    try:
        s.sendmail(msg['From'], EMAIL_SUBSCRIBERS, msg.as_string())
        s.quit()
    except:
        logging.exception("Could not send mail.")


class AllocationsHandler:
    INAUGURATIONS_INDEX = "inaugurations_4"
    ALLOCATIONS_INDEX = "allocations_3"

    def __init__(self, subscription_mgr, db, events_monitor):
        self._hosts_state = dict()
        self._db = db
        self._subscription_mgr = subscription_mgr
        logging.info('Subscribing to all hosts allocations.')
        subscription_mgr.registerForAllAllocations(self._pika_all_allocations_handler)
        self._allocation_subscriptions = dict()
        self._tasks = Queue.Queue()
        self._latest_allocation_idx = None
        self._host_indices = list()
        self._last_requested_allocation = None
        self._events_monitor = events_monitor

    def run(self):
        self._events_monitor.start()
        while True:
            logging.info('Waiting for a new event (current number of monitored allocations: {})...'
                         .format(len(self._allocation_subscriptions)))
            finishedEvent, callback, message, args = self._tasks.get(block=True)
            if callback is None:
                logging.info('Finished handling events.')
                finishedEvent.set()
                break
            try:
                if args is None:
                    callback(message)
                else:
                    callback(message, **args)
            finally:
                if finishedEvent is not None:
                    finishedEvent.set()
                self._events_monitor.an_event_has_occurred()

    def stop(self, remove_pending_events=False):
        finishedEvent = threading.Event()
        if remove_pending_events:
            while not self._tasks.empty():
                self._tasks.get(block=False)
        self._tasks.put([finishedEvent, None, None, None])

    def finish_all_commands_in_queue(self):
        finishedEvent = threading.Event()
        self._tasks.put([finishedEvent, lambda *a: None, None, None])
        finishedEvent.wait()

    def _pika_inauguration_handler(self, message):
        self._tasks.put([None, self._inauguration_handler, message, None])

    def _inauguration_handler(self, msg):
        host_id = msg['id']
        if host_id not in self._hosts_state:
            logging.error('Got an inauguration message for a host without'
                          ' a known allocation: {}. Ignoring.'.format(host_id))
            return
        host_state = self._hosts_state[host_id]

        if msg['status'] == 'done':
            host_state['end_timestamp'] = time.time()
            logging.info('Host "{}" has finished inauguration. Unsubscribing.'.format(host_id))
            self._hosts_state[host_id]["inauguration_done"] = True
            self._add_inauguration_record_to_db(host_id)
            self._subscription_mgr.unregisterForInaugurator(host_id)
        elif msg['status'] == 'progress' and msg['progress']['state'] == 'fetching':
            logging.info('Progress message for {}'.format(host_id))
            chain_count = msg['progress']['chainGetCount']
            self._hosts_state[host_id]['latest_chain_count'] = chain_count

    def _unsubscribe_allocation(self, allocation_idx):
        """Precondition: allocation is subscribed to."""
        del self._allocation_subscriptions[allocation_idx]
        allocated_hosts = [host_id for host_id, host in self._hosts_state.iteritems() if
                           host['allocation_idx'] == allocation_idx]
        uninaugurated_hosts = [host_id for host_id in allocated_hosts if
                               not self._hosts_state[host_id]["inauguration_done"]]
        if uninaugurated_hosts:
            logging.info("Inauguration stage for allocation {} ended without finishing inauguration "
                         "of the following hosts: {}.".format(allocation_idx,
                                                              ','.join(uninaugurated_hosts)))
            uninaugurated_hosts.sort()
            for host_id in uninaugurated_hosts:
                logging.info('Unsubscribing from inauguration events of "{}".'.format(host_id))
                self._add_inauguration_record_to_db(host_id)
                self._subscription_mgr.unregisterForInaugurator(host_id)
        for host in allocated_hosts:
            del self._hosts_state[host]

    def _pika_all_allocations_handler(self, message):
        self._tasks.put([None, self._all_allocations_handler, message, None], block=True)

    def _store_allocation_request(self, message):
        nr_nodes = len(message["requirements"])
        record = dict(allocationInfo=message['allocationInfo'],
                      nodes=self.get_nodes_list_from_requirements(message['requirements']),
                      nr_nodes=nr_nodes,
                      highest_phase_reached="requested",
                      done=False,
                      reason="Unknown",
                      allocation_duration=0)
        record["date"] = datetime_from_timestamp(time.time())
        record_metadata = self._db.create(index=self.ALLOCATIONS_INDEX,
                                          doc_type='allocation',
                                          body=record)
        return record_metadata["_id"], record

    def _store_allocation_rejection(self, reason):
        assert self._last_requested_allocation is not None
        record_id, record = self._last_requested_allocation
        record["highest_phase_reached"] = "rejected"
        record["reason"] = reason
        self._db.update(index=self.ALLOCATIONS_INDEX,
                        doc_type='allocation',
                        id=record_id,
                        body=dict(doc=record))

    def _store_allocation_creation(self, message):
        assert self._last_requested_allocation is not None
        record_id, record = self._last_requested_allocation
        record["highest_phase_reached"] = "created"
        self.update_nodes_list_with_allocated(record, message["allocated"])
        record.update(dict(nr_nodes=len(record["nodes"]),
                           highest_phase_reached="created",
                           allocation_id=message["allocationID"],
                           creation_time=datetime_from_timestamp(time.time())))
        self._db.update(index=self.ALLOCATIONS_INDEX,
                        doc_type='allocation',
                        id=record_id,
                        body=dict(doc=record))
        self._allocation_subscriptions[message["allocationID"]] = record_id, record

    def _store_allocation_death(self, allocation_id, reason):
        record_id, record = self._allocation_subscriptions[allocation_id]
        record["highest_phase_reached"] = "dead"
        record["reason"] = reason
        record["allocation_duration"] = \
            (datetime_from_timestamp(time.time()) - record["creation_time"]).total_seconds()
        if record["done"]:
            record["test_duration"] = \
                (datetime_from_timestamp(time.time()) - record["creation_time"]).total_seconds()
        self._db.update(index=self.ALLOCATIONS_INDEX,
                        doc_type='allocation',
                        id=record_id,
                        body=dict(doc=record))

    def _store_allocation_done(self, allocation_id):
        record_id, record = self._allocation_subscriptions[allocation_id]
        record["highest_phase_reached"] = "done"
        record["done"] = True
        record["inauguration_duration"] = \
            (datetime_from_timestamp(time.time()) - record["creation_time"]).total_seconds()
        self._db.update(index=self.ALLOCATIONS_INDEX,
                        doc_type='allocation',
                        id=record_id,
                        body=dict(doc=record))

    def _all_allocations_handler(self, message):
        event = message["event"]
        assert event in ("requested", "rejected", "created", "done", "dead"), event
        if len(self._allocation_subscriptions) == MAX_NR_ALLOCATIONS:
            logging.error("Something has gone wrong; Too many open allocations. Quitting")
            self.stop(remove_pending_events=True)
            return
        if event == "requested":
            allocation_data = self._store_allocation_request(message)
            self._last_requested_allocation = allocation_data
        elif event == "rejected":
            if self._last_requested_allocation is None:
                logging.info("Got an allocation rejection message without a requeest message before. "
                             "Skipping.")
                return
            if self._last_requested_allocation[1]["highest_phase_reached"] != "requested":
                logging.error("Got an allocation rejection message in an invalid context (last event was:"
                              " {}".format(self._last_requested_allocation[1]["highest_phase_reached"]))
                return
            self._store_allocation_rejection(reason=message["reason"])
        elif event == "created":
            logging.info('New allocation: {}'.format(message))
            if self._last_requested_allocation is None:
                logging.info("Ignoring allocation creation message since its request message was skipped")
                return
            last_requested_allocation = self._last_requested_allocation[1]
            self._store_allocation_creation(message)
            idx = message['allocationID']
            if self._latest_allocation_idx is None:
                self._latest_allocation_idx = idx
            elif idx < self._latest_allocation_idx:
                logging.error("Got an allocation index {} which is smaller than the previous one ({}) "
                              "(could RackAttack have been restarted?). Quitting."
                              .format(idx, self._latest_allocation_idx))
                self.stop(remove_pending_events=True)
                return
            hosts = message['allocated']
            logging.debug('New allocation: {}.'.format(hosts))
            allocation_unsubscribed_from_due_to_new_allocation = set()
            for name, host_id in hosts.iteritems():
                if host_id in self._hosts_state:
                    existing_allocation = self._hosts_state[host_id]["allocation_idx"]
                    assert existing_allocation not in allocation_unsubscribed_from_due_to_new_allocation
                    logging.warn("Allocation {} was allocated with a host which is already used by "
                                 "another allocation ({}). Unsubscribing from the latter first..."
                                 .format(idx, existing_allocation))
                    self._unsubscribe_allocation(existing_allocation)
                    allocation_unsubscribed_from_due_to_new_allocation.add(existing_allocation)
                    assert host_id not in self._hosts_state
                # Update hosts state
                self._hosts_state[host_id] = dict(start_timestamp=time.time(),
                                                  name=name,
                                                  allocation_idx=idx,
                                                  inauguration_done=False)
                logging.info("Subscribing to inaugurator events of: {}.".format(host_id))
                self._subscription_mgr.registerForInagurator(host_id, self._pika_inauguration_handler)
                nodes = [node for node in last_requested_allocation["nodes"] if node["node_name"] == name]
                if nodes:
                    requirements = nodes[0]["requirements"]
                    self._hosts_state[host_id].update(requirements)
                else:
                    logging.error("Failed to resolve requirmenents for inaugurated host {}".format(name))
                logging.info("Subscribed.")
        elif event == "done":
            allocation_id = message['allocationID']
            if allocation_id not in self._allocation_subscriptions:
                logging.info("Ignoring done message for allocation {} since its request message was "
                             "skipped.".format(allocation_id))
                return
            allocation_id = message['allocationID']
            logging.info('Inauguration stage for allocation {} is over.'.format(allocation_id))
            self._store_allocation_done(allocation_id)
        elif event == "dead":
            allocation_id = message['allocationID']
            if allocation_id not in self._allocation_subscriptions:
                logging.info("Ignoring death message for allocation {} since its request message was "
                             "skipped.".format(allocation_id))
                return
            logging.info('Allocation {} is dead.'.format(allocation_id))
            self._store_allocation_death(allocation_id, reason=message["reason"])
            self._unsubscribe_allocation(allocation_id)

    def _add_inauguration_record_to_db(self, host_id):
        state = self._hosts_state[host_id]
        record_datetime = datetime_from_timestamp(state['start_timestamp'])

        local_store_count = None
        remote_store_count = None
        try:
            chain_count = state['latest_chain_count']
            local_store_count = chain_count.pop(0)
            remote_store_count = chain_count.pop(0)
        except KeyError:
            # NO info about Osmosis chain
            pass
        except IndexError:
            pass

        majority_chain_type = 'unknown'
        if local_store_count is not None:
            majority_chain_type = 'local'
            if remote_store_count is not None and \
                    local_store_count < remote_store_count:
                majority_chain_type = 'remote'
        id = "%d%03d%05d" % (state['start_timestamp'], state['allocation_idx'], self._hostIndex(host_id))

        record = dict(date=record_datetime,
                      host_id=host_id,
                      local_store_count=local_store_count,
                      remote_store_count=remote_store_count,
                      majority_chain_type=majority_chain_type)
        if state["inauguration_done"]:
            record["inauguration_period_length"] = state['end_timestamp'] - state['start_timestamp']
        record.update(state)

        try:
            logging.info("Inserting inauguration to DB (id: {}):\n{}".format(id, pprint.pformat(record)))
            self._db.create(index=self.INAUGURATIONS_INDEX, doc_type='inauguration', body=record, id=id)
        except Exception:
            logging.exception("Inauguration DB record insertion failed. Quitting.")
            self.stop()
            return

    def _hostIndex(self, hostID):
        if hostID not in self._host_indices:
            self._host_indices.append(hostID)
        return self._host_indices.index(hostID)

    @classmethod
    def get_nodes_list_from_requirements(cls, requirements):
        result = list()
        for node_name, node_requirements in requirements.iteritems():
            node = dict(node_name=node_name, requirements=node_requirements)
            result.append(node)
        return result

    def update_nodes_list_with_allocated(self, allocation_record, allocated):
        for node in allocation_record["nodes"]:
            node_name = node["node_name"]
            node["server_name"] = allocated[node_name]


def create_subscription():
    _, amqp_url, _ = os.environ['RACKATTACK_PROVIDER'].split("@@")
    subscription_mgr = subscribe.Subscribe(amqp_url)
    return subscription_mgr


def alert_warn_func(msg):
    logging.warn(msg)
    send_mail(msg)


def alert_info_func(msg):
    logging.info(msg)
    send_mail(msg)


def main():
    logconfig.configure_logger()
    db = elasticsearchdbwrapper.ElasticsearchDBWrapper(alert_func=send_mail)
    subscription_mgr = create_subscription()
    monitor = events_monitor.EventsMonitor(MAX_NR_SECONDS_WITHOUT_EVENTS_BEFORE_ALERTING,
                                           alert_info_func,
                                           alert_warn_func)
    allocation_handler = AllocationsHandler(subscription_mgr, db, monitor)
    while True:
        try:
            allocation_handler.run()
            break
        except elasticsearch.ConnectionTimeout:
            db.handle_disconnection()
        except elasticsearch.ConnectionError:
            db.handle_disconnection()
        except elasticsearch.exceptions.TransportError:
            db.handle_disconnection()
        except KeyboardInterrupt:
            break
        except Exception:
            msg = "Critical error, exiting.\n\n"
            logging.exception(msg)
            msg += traceback.format_exc()
            send_mail(msg)
            sys.exit(1)
    logging.info("Done.")

if __name__ == '__main__':
    main()

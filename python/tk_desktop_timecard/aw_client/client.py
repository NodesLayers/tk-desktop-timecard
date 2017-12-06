import json
import logging
import socket
import os
import threading
import Queue
from datetime import datetime
from collections import namedtuple

import requests as req

from aw_core.models import Event
from aw_core.dirs import get_data_dir
from aw_core.decorators import deprecated

from .config import load_config


# FIXME: This line is probably badly placed
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class ActivityWatchClient:
    """
    A handy wrapper around the aw-server REST API. The recommended way of interacting with the server.

    Can be used with a `with`-statement as an alternative to manually calling connect and disconnect in a try-finally clause.

    :Example:

    .. literalinclude:: examples/client.py
        :lines: 7-
    """

    def __init__(self, client_name="unknown", testing=False):
        self.testing = testing

        # uses of the client_* variables is deprecated
        self.client_name = client_name
        self.client_hostname = socket.gethostname()

        # use these instead
        self.name = self.client_name
        self.hostname = self.client_hostname

        config = load_config()

        # server_config = config["server" if not testing else "server-testing"]
        server_name = "server" if not testing else "server-testing"
        hostname = config.get(server_name, 'hostname')
        port = config.get(server_name, 'port')
        # self.server_host = "{hostname}:{port}".format(**server_config)
        self.server_host = "{}:{}".format(hostname, port)

        self.request_queue = RequestQueue(self)

    #
    #   Get/Post base requests
    #

    def _url(self, endpoint):
        return "http://{host}/api/0/{endpoint}".format(host=self.server_host, endpoint=endpoint)

    def _log_request_exception(self, r, e):
        logger.warning(str(e))
        logger.warning("{} request response had status code {}".format(r.request.method, r.status_code))
        try:
            logger.warning("Message: {}".format(r.status_code, r.json()))
        except json.JSONDecodeError:
            pass

    def _get(self, endpoint, params=None):
        r = req.get(self._url(endpoint), params=params)
        try:
            r.raise_for_status()
        except req.RequestException as e:
            self._log_request_exception(r, e)
            raise e
        return r

    def _post(self, endpoint, data):
        headers = {"Content-type": "application/json", "charset": "utf-8"}
        r = req.post(self._url(endpoint), data=bytes(json.dumps(data), "utf8"), headers=headers)
        try:
            r.raise_for_status()
        except req.RequestException as e:
            self._log_request_exception(r, e)
            raise e
        return r

    def _delete(self, endpoint, data=dict()):
        headers = {"Content-type": "application/json"}
        r = req.delete(self._url(endpoint), data=json.dumps(data), headers=headers)
        try:
            r.raise_for_status()
        except req.RequestException as e:
            self._log_request_exception(r, e)
            raise e
        return r

    def get_info(self):
        """Returns a dict currently containing the keys 'hostname' and 'testing'."""
        endpoint = "info"
        return self._get(endpoint).json()

    #
    #   Event get/post requests
    #

    def get_events(self, bucket_id, limit=100, start=None, end=None):
        endpoint = "buckets/{}/events".format(bucket_id)

        params = dict()  # type: Dict[str, str]
        if limit is not None:
            params["limit"] = str(limit)
        if start is not None:
            params["start"] = start.isoformat()
        if end is not None:
            params["end"] = end.isoformat()

        events = self._get(endpoint, params=params).json()
        return [Event(**event) for event in events]

    # @deprecated  # use insert_event instead
    def send_event(self, bucket_id, event):
        return self.insert_event(bucket_id, event)

    # @deprecated  # use insert_events instead
    def send_events(self, bucket_id, events):
        return self.insert_events(bucket_id, events)

    def insert_event(self, bucket_id, event):
        endpoint = "buckets/{}/events".format(bucket_id)
        data = event.to_json_dict()
        return Event(**self._post(endpoint, data).json())

    def insert_events(self, bucket_id, events):
        endpoint = "buckets/{}/events".format(bucket_id)
        data = [event.to_json_dict() for event in events]
        self._post(endpoint, data)

    def heartbeat(self, bucket_id, event, pulsetime, queued=False):
        """ This endpoint can use the failed requests retry queue.
            This makes the request itself non-blocking and therefore
            the function will in that case always returns None. """

        endpoint = "buckets/{}/heartbeat?pulsetime={}".format(bucket_id, pulsetime)
        data = event.to_json_dict()
        if queued:
            self.request_queue.add_request(endpoint, data)
            return None
        else:
            return Event(**self._post(endpoint, data).json())

    #
    #   Bucket get/post requests
    #

    def get_buckets(self):
        return self._get('buckets/').json()

    def create_bucket(self, bucket_id, event_type, queued=False):
        if queued:
            self.request_queue.register_bucket(bucket_id, event_type)
        else:
            endpoint = "buckets/{}".format(bucket_id)
            data = {
                'client': self.name,
                'hostname': self.hostname,
                'type': event_type,
            }
            self._post(endpoint, data)

    def delete_bucket(self, bucket_id):
        self._delete('buckets/{}'.format(bucket_id))

    @deprecated
    def setup_bucket(self, bucket_id, event_type):
        self.create_bucket(bucket_id, event_type, queued=True)

    def __enter__(self):
        self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def connect(self):
        if not self.request_queue.is_alive():
            self.request_queue.start()

    def disconnect(self):
        self.request_queue.stop()
        self.request_queue.join()

        # Throw away old thread object, create new one since same thread cannot be started twice
        self.request_queue = RequestQueue(self)


QueuedRequest = namedtuple("QueuedRequest", ["endpoint", "data"])
Bucket = namedtuple("Bucket", ["id", "type"])


class RequestQueue(threading.Thread):
    """Used to asynchronously send heartbeats.

    Handles:
        - Cases where the server is temporarily unavailable
        - Saves all queued requests to file in case of a server crash
    """

    VERSION = 1  # update this whenever the queue-file format changes

    def __init__(self, client, dispatch_interval=0):
        # threading.Thread.__init__(self, daemon=True)
        threading.Thread.__init__(self)
        threading.Thread.daemon = True

        self.client = client
        self.dispatch_interval = dispatch_interval  # Time to wait between dispatching events, useful for throttling.

        self.connected = False
        self._stop_event = threading.Event()

        # Buckets that will have events queued to them, will be created if they don't exist
        self._registered_buckets = []  # type: List[Bucket]

        self._queue = Queue.Queue()

        # Setup failed queues file
        data_dir = get_data_dir("aw-client")
        queued_dir = os.path.join(data_dir, "queued")
        if not os.path.exists(queued_dir):
            os.makedirs(queued_dir)
        self.queue_file = os.path.join(queued_dir, self.client.name + ".v{}.json".format(self.VERSION))

    def _create_buckets(self):
        # Check if bucket exists
        buckets = self.client.get_buckets()
        for bucket in self._registered_buckets:
            if bucket.id not in buckets:
                self.client.create_bucket(bucket.id, bucket.type)

    def _queue_to_file(self, endpoint, data):
        entry = QueuedRequest(endpoint=endpoint, data=data)
        with open(self.queue_file, "a+") as queue_fp:
            queue_fp.write(json.dumps(entry) + "\n")

    def _load_queue(self):
        # If crash when lost connection, queue failed requests
        failed_requests = []  # type: List[QueuedRequests]

        # Load failed events from queue into failed_requests
        open(self.queue_file, "a").close()  # Create file if doesn't exist
        with open(self.queue_file, "r") as queue_fp:
            for request in queue_fp:
                logger.debug(request)
                try:
                    failed_requests.append(QueuedRequest(*json.loads(request)))
                except json.decoder.JSONDecodeError as e:
                    logger.error(e, exc_info=True)
                    logger.error("Request that failed: {}".format(request))
                    logger.warning("Skipping request that failed to load")

        # Insert failed_requests into dispatching queue
        # FIXME: We really shouldn't be clearing the file here until the events have been sent to server.
        open(self.queue_file, "w").close()  # Clear file
        if len(failed_requests) > 0:
            for request in failed_requests:
                self._queue.put(request)
            logger.info("Loaded {} failed requests from queuefile".format(len(failed_requests)))

    def _save_queue(self):
        # When lost connection, save queue to file for later sending
        with open(self.queue_file, "w") as queue_fp:
            while not self._queue.empty():
                request = self._queue.get()

                if request is not None:
                    queue_fp.write(json.dumps(request) + "\n")

    def _try_connect(self):
        try:  # Try to connect
            self._create_buckets()
            self.connected = True
            logger.info("Connection to aw-server established")
        except req.RequestException:
            self.connected = False

        return self.connected

    def wait(self, seconds):
        return self._stop_event.wait(seconds)

    def should_stop(self):
        return self._stop_event.is_set()

    def _dispatch_request(self):
        try:
            request = self._queue.get(block=False)
        except Queue.Empty:
            self.wait(0.1)
            return

        try:
            self.client._post(request.endpoint, request.data)
        except req.RequestException as e:
            self._queue.queue.appendleft(request)
            self.connected = False
            logger.warning("Failed to send request to aw-server, will queue requests until connection is available.")

    def run(self):
        self._stop_event.clear()
        while not self.should_stop():
            # Connect
            while not self._try_connect():
                if self.wait(10):
                    break

            # Load requests from queuefile
            self._load_queue()

            # Dispatch requests until connection is lost or thread should stop
            while self.connected and not self.should_stop():
                self._dispatch_request()

            # Disconnected or should stop, save remaining to queuefile
            self._save_queue()

    def stop(self):
        self._stop_event.set()

    def add_request(self, endpoint, data):
        if self.connected:
            self._queue.put(QueuedRequest(endpoint=endpoint, data=data))
        else:
            self._queue_to_file(endpoint, data)

    def register_bucket(self, bucket_id, event_type):
        self._registered_buckets.append(Bucket(bucket_id, event_type))

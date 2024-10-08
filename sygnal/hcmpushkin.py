# -*- coding: utf-8 -*-
# Copyright 2014 Leon Handreke
# Copyright 2017 New Vector Ltd
# Copyright 2019-2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import time
from io import BytesIO
from typing import TYPE_CHECKING, Any, AnyStr, Dict, List, Optional, Tuple

from opentracing import Span, logs, tags
from prometheus_client import Counter, Gauge, Histogram
from twisted.internet.defer import DeferredSemaphore
from twisted.web.client import FileBodyProducer, HTTPConnectionPool, readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IResponse

from sygnal.exceptions import (
    NotificationDispatchException,
    PushkinSetupException,
    TemporaryNotificationDispatchException,
)
from sygnal.helper.context_factory import ClientTLSOptionsFactory
from sygnal.helper.proxy.proxyagent_twisted import ProxyAgent
from sygnal.notifications import (
    ConcurrencyLimitedPushkin,
    Device,
    Notification,
    NotificationContext,
)
from sygnal.utils import NotificationLoggerAdapter, json_decoder, twisted_sleep

if TYPE_CHECKING:
    from sygnal.sygnal import Sygnal

QUEUE_TIME_HISTOGRAM = Histogram(
    "sygnal_hcm_queue_time", "Time taken waiting for a connection to HCM"
)

SEND_TIME_HISTOGRAM = Histogram(
    "sygnal_hcm_request_time", "Time taken to send HTTP request to HCM"
)

PENDING_REQUESTS_GAUGE = Gauge(
    "sygnal_pending_hcm_requests", "Number of HCM requests waiting for a connection"
)

ACTIVE_REQUESTS_GAUGE = Gauge(
    "sygnal_active_hcm_requests", "Number of HCM requests in flight"
)

RESPONSE_STATUS_CODES_COUNTER = Counter(
    "sygnal_hcm_status_codes",
    "Number of HTTP response status codes received from HCM",
    labelnames=["pushkin", "code"],
)

logger = logging.getLogger(__name__)

HCM_URL = b"https://push-api.cloud.huawei.com/v2/" #
HCM_URL_PATH = b"/messages:send"
MAX_TRIES = 3
RETRY_DELAY_BASE = 10
MAX_BYTES_PER_FIELD = 1024

# The error codes that mean a registration ID will never
# succeed and we should reject it upstream.
# We include NotRegistered here too for good measure, even
# though hcm-client 'helpfully' extracts these into a separate
# list.
BAD_PUSHKEY_FAILURE_CODES = [
    "80100000",
]

# Failure codes that mean the message in question will never
# succeed, so don't retry, but the registration ID is fine
# so we should not reject it upstream.
BAD_MESSAGE_FAILURE_CODES = ["80100001", "80100003", "80100004", "80300008", "80300010"]

BAD_AUTH_FAILURE_CODES = ["80200003", "80200001", "80300002", "80300007", "80600003"]

DEFAULT_MAX_CONNECTIONS = 20


class HcmPushkin(ConcurrencyLimitedPushkin):
    """
    Pushkin that relays notifications to Huawei.
    """

    UNDERSTOOD_CONFIG_FIELDS = {
        "type",
        "app_id",
        "project_id",
        "client_secret",
        "max_connections",
    } | ConcurrencyLimitedPushkin.UNDERSTOOD_CONFIG_FIELDS

    def __init__(self, name: str, sygnal: "Sygnal", config: Dict[str, Any]) -> None:
        super().__init__(name, sygnal, config)

        nonunderstood = set(self.cfg.keys()).difference(self.UNDERSTOOD_CONFIG_FIELDS)
        if len(nonunderstood) > 0:
            logger.warning(
                "The following configuration fields are not understood: %s",
                nonunderstood,
            )

        self.http_pool = HTTPConnectionPool(reactor=sygnal.reactor)
        self.max_connections = self.get_config(
            "max_connections", int, DEFAULT_MAX_CONNECTIONS
        )

        self.connection_semaphore = DeferredSemaphore(self.max_connections)
        self.http_pool.maxPersistentPerHost = self.max_connections

        tls_client_options_factory = ClientTLSOptionsFactory()
        # use the Sygnal global proxy configuration
        proxy_url = sygnal.config.get("proxy")

        self.http_agent = ProxyAgent(
            reactor=sygnal.reactor,
            pool=self.http_pool,
            contextFactory=tls_client_options_factory,
            proxy_url_str=proxy_url,
        )

        self.app_id = self.get_config("app_id", int)
        if not self.app_id:
            raise PushkinSetupException("No App ID set in config, please find in AppGallery Connect page")

        self.project_id = self.get_config("project_id", int)
        if not self.project_id:
            raise PushkinSetupException("No Project ID set in config, please find in AppGallery Connect page")

        self.client_secret = self.get_config("client_secret", str)
        if not self.project_id:
            raise PushkinSetupException("No Client Secret set in config, please find in AppGallery Connect page")

        # Use the hcm_options config dictionary as a foundation for the body;
        # this lets the Sygnal admin choose custom hcm options
        # (e.g. content_available).
        self.hcm_app_url = HCM_URL + bytes(str(self.project_id), 'utf-8') + HCM_URL_PATH
        self.access_token = None
        
        self.base_request_body = self.get_config("hcm_options", dict, {})
        if not isinstance(self.base_request_body, dict):
            raise PushkinSetupException(
                "Config field hcm_options, if set, must be a dictionary of options"
            )
        
    

    @classmethod
    async def create(
        cls, name: str, sygnal: "Sygnal", config: Dict[str, Any]
    ) -> "HcmPushkin":
        """
        Override this if your pushkin needs to call async code in order to
        be constructed. Otherwise, it defaults to just invoking the Python-standard
        __init__ constructor.

        Returns:
            an instance of this Pushkin
        """
        return cls(name, sygnal, config)

    async def _fetch_access_token(self) -> str:
        """
        Generate an access token to access Huawei public app-level APIs.
        Args:

        Returns:
            an access token of type string
        """

        body = '&grant_type=client_credentials&client_id=106945189&client_secret=2a68ffecdbb5826c71ca144a7df1d3f3992c446950249aad6915b2e7c1ab72e2&'
        headers = {
            'Accept':'*/*',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        response, response_text = await self._perform_access_request(body=body, headers=headers,)


        logger.debug("Access Token: " + json.dumps(response_text))
        response_object = json.loads(response_text)
        access_token = response_object["access_token"]
        return access_token

    async def _perform_access_request(
        self, body: Dict, headers: Dict[AnyStr, List[AnyStr]]
    ) -> Tuple[IResponse, str]:
        """
        Perform an HTTP request to the HCM server with the body and headers
        specified.
        Args:
            body: Body. Will be JSON-encoded.
            headers: HTTP Headers.

        Returns:

        """
        body_producer = FileBodyProducer(BytesIO(json.dumps(body).encode()))
        hcm_app_url = b"https://oauth-login.cloud.huawei.com/oauth2/v3/token"


        # we use the semaphore to actually limit the number of concurrent
        # requests, since the HTTPConnectionPool will actually just lead to more
        # requests being created but not pooled – it does not perform limiting.
        with QUEUE_TIME_HISTOGRAM.time():
            with PENDING_REQUESTS_GAUGE.track_inprogress():
                await self.connection_semaphore.acquire()

        try:
            with SEND_TIME_HISTOGRAM.time():
                with ACTIVE_REQUESTS_GAUGE.track_inprogress():
                    response = await self.http_agent.request(
                        b"POST",
                        hcm_app_url,
                        headers=Headers(headers),
                        bodyProducer=body_producer,
                    )
                    response_text = (await readBody(response)).decode()
        except Exception as exception:
            raise TemporaryNotificationDispatchException(
                "HCM request failure"
            ) from exception
        finally:
            self.connection_semaphore.release()
        return response, response_text


    async def _perform_http_request(
        self, body: Dict, headers: Dict[AnyStr, List[AnyStr]]
    ) -> Tuple[IResponse, str]:
        """
        Perform an HTTP request to the HCM server with the body and headers
        specified.
        Args:
            body: Body. Will be JSON-encoded.
            headers: HTTP Headers.

        Returns:

        """
        body_producer = FileBodyProducer(BytesIO(json.dumps(body).encode()))

        # we use the semaphore to actually limit the number of concurrent
        # requests, since the HTTPConnectionPool will actually just lead to more
        # requests being created but not pooled – it does not perform limiting.
        with QUEUE_TIME_HISTOGRAM.time():
            with PENDING_REQUESTS_GAUGE.track_inprogress():
                await self.connection_semaphore.acquire()

        try:
            with SEND_TIME_HISTOGRAM.time():
                with ACTIVE_REQUESTS_GAUGE.track_inprogress():
                    response = await self.http_agent.request(
                        b"POST",
                        self.hcm_app_url,
                        headers=Headers(headers),
                        bodyProducer=body_producer,
                    )
                    response_text = (await readBody(response)).decode()
        except Exception as exception:
            raise TemporaryNotificationDispatchException(
                "HCM request failure"
            ) from exception
        finally:
            self.connection_semaphore.release()
        return response, response_text

    async def _request_dispatch(
        self,
        n: Notification,
        log: NotificationLoggerAdapter,
        body: dict,
        headers: Dict[AnyStr, List[AnyStr]],
        pushkeys: List[str],
        span: Span,
    ) -> Tuple[List[str], List[str]]:
        poke_start_time = time.time()

        failed = []
        response, response_text = await self._perform_http_request(body, headers)
        RESPONSE_STATUS_CODES_COUNTER.labels(pushkin=self.name, code=response.code).inc()
        
        log.debug("HCM request took %f seconds", time.time() - poke_start_time)

        span.set_tag(tags.HTTP_STATUS_CODE, response.code)
        retry_after = None
        
        if response.code == 500:
            log.debug("%d from server, waiting to try again. Internal service error.", response.code)

            retry_after = None

            for header_value in response.headers.getRawHeaders(
                b"retry-after", default=[]
            ):
                retry_after = int(header_value)
                span.log_kv({"event": "hcm_retry_after", "retry_after": retry_after})

            raise TemporaryNotificationDispatchException(
                "HCM server error, hopefully temporary.", custom_retry_delay=retry_after
            )
        elif response.code == 502:
            log.debug("%d from server, waiting to try again. The connection request is abnormal, generally because the network is unstable.", response.code)

            retry_after = None

            for header_value in response.headers.getRawHeaders(
                b"retry-after", default=[]
            ):
                retry_after = int(header_value)
                span.log_kv({"event": "hcm_retry_after", "retry_after": retry_after})

            raise TemporaryNotificationDispatchException(
                "HCM server error, hopefully temporary.", custom_retry_delay=retry_after
            )

        elif response.code == 503:
            log.debug("%d from server, this is due to too much traffic.", response.code)
            log.debug("Set the average push speed to a value smaller than the QPS quota provided by Huawei. For details about the QPS quota, please refer to FAQs. Set the average push interval. Do not push messages too frequently in a period of time.")
            raise TemporaryNotificationDispatchException(
                "Traffic control error.", custom_retry_delay=retry_after
            )

        elif response.code == 400:
            log.error(
                "%d from server, we have sent something invalid! Error: %r",
                response.code,
                response_text,
            )
            # permanent failure: give up
            raise NotificationDispatchException("Invalid request")
        elif response.code == 401:
            log.error(
                "401 from server! Our Access Token is invalid or Expaired? Error: %r", response_text
            )
            # permanent failure: give up
            raise NotificationDispatchException("Not authorised to push")

        elif response.code == 404:
            # assume they're all failed
            log.error("Service not found. Verify that the request URI is correct.")
            raise NotificationDispatchException("Service not found")

        elif 200 <= response.code < 300:
            try:
                resp_object = json_decoder.decode(response_text)
            except ValueError:
                raise NotificationDispatchException("Invalid JSON response from hcm.")
            if "code" not in resp_object:
                log.error(
                    "%d from server but response contained no 'code' key: %r",
                    response.code,
                    response_text,
                )
            
            new_pushkeys = []
            if  resp_object["code"] == "80200003":
                log.error("%d from server but OAuth token expired. The access token in the Authorization parameter in the request HTTP header has expired. Obtain a new access token: %r - %r",
                    response.code,
                    resp_object["code"],
                    resp_object["msg"],
                    )
                log.error(
                    "Push Tokens %r has temporarily failed with code %r",
                    pushkeys,
                    resp_object["code"],
                )
                new_pushkeys.append(pushkeys)

            # determine which pushkeys to retry or forget about
            
            if  resp_object["code"] == "80100000":
                log.error("%d from server but some push tokens were invalid and not delievered: %r - %r",
                    response.code,
                    resp_object["code"],
                    resp_object["msg"],
                    )
                
                for i, result in enumerate(json_decoder.decode(resp_object["msg"])["illegal_tokens"]):
                    log.warning(
                        "Error for pushkey %s", pushkeys[i],
                    )
                    span.set_tag("hcm_error", resp_object["code"])
                    if resp_object["code"] in BAD_PUSHKEY_FAILURE_CODES:
                        log.error(
                            "Push Token %r has permanently failed with code %r: "
                            "rejecting upstream",
                            pushkeys[i],
                            resp_object["code"],
                        )
                        failed.append(pushkeys[i])
                    elif resp_object["code"] in BAD_MESSAGE_FAILURE_CODES:
                        log.error(
                            "Message for push token %r has permanently failed with code %r",
                            pushkeys[i],
                            resp_object["code"],
                        )
                    else:
                        log.info(
                            "Push Token %r has temporarily failed with code %r",
                            pushkeys[i],
                            resp_object["code"],
                        )
                        new_pushkeys.append(pushkeys[i])
            return failed, new_pushkeys
            
        else:
            raise NotificationDispatchException(
                f"Unknown HCM response code: {response.code}, {response.phrase}"
            )

    async def _dispatch_notification_unlimited(
        self, n: Notification, device: Device, context: NotificationContext
    ) -> List[str]:
        log = NotificationLoggerAdapter(logger, {"request_id": context.request_id})

        # `_dispatch_notification_unlimited` gets called once for each device in the
        # `Notification` with a matching app ID. We do something a little dirty and
        # perform all of our dispatches the first time we get called for a
        # `Notification` and do nothing for the rest of the times we get called.
        pushkeys = [
            device.pushkey for device in n.devices if self.handles_appid(device.app_id)
        ]
        # `pushkeys` ought to never be empty here. At the very least it should contain
        # `device`'s pushkey.

        if pushkeys[0] != device.pushkey:
            # We've already been asked to dispatch for this `Notification` and have
            # previously sent out the notification to all devices.
            return []

        # The pushkey is kind of secret because you can use it to send push
        # to someone.   
        # span_tags = {"pushkeys": pushkeys}
        span_tags = {"hcm_num_devices": len(pushkeys)}
        
        with self.sygnal.tracer.start_span(
            "hcm_dispatch", tags=span_tags, child_of=context.opentracing_span
        ) as span_parent:
            # TODO: Implement collapse_key to queue only one message per room.
            failed: List[str] = []

            data = HcmPushkin._build_data(n, device)

            # Reject pushkey if default_payload is misconfigured
            if data is None:
                failed.append(device.pushkey)

            self.access_token = await self._fetch_access_token()
            headers = {
                "User-Agent": ["sygnal"],
                'Content-Type':['application/json'],
                'Authorization':['Bearer %s' % (self.access_token)]
            }
            body = self.base_request_body.copy()
            body["message"] = {}
            body["message"]["data"] = json.dumps(data)
            body["validate_only"] = False
            

            for retry_number in range(0, MAX_TRIES):
                body["message"]["token"] = pushkeys

                log.info("Sending (attempt %i) => %r", retry_number, pushkeys)

                try:
                    span_tags = {"retry_num": retry_number}

                    with self.sygnal.tracer.start_span(
                        "hcm_dispatch_try", tags=span_tags, child_of=span_parent
                    ) as span:
                        new_failed, new_pushkeys = await self._request_dispatch(
                            n, log, body, headers, pushkeys, span
                        )
                    pushkeys = new_pushkeys
                    failed += new_failed

                    if len(pushkeys) == 0:
                        break
                except TemporaryNotificationDispatchException as exc:
                    retry_delay = RETRY_DELAY_BASE * (2 ** retry_number)
                    if exc.custom_retry_delay is not None:
                        retry_delay = exc.custom_retry_delay

                    log.warning(
                        "Temporary failure, will retry in %d seconds",
                        retry_delay,
                        exc_info=True,
                    )

                    span_parent.log_kv(
                        {"event": "temporary_fail", "retrying_in": retry_delay}
                    )

                    await twisted_sleep(
                        retry_delay, twisted_reactor=self.sygnal.reactor
                    )

            if len(pushkeys) > 0:
                log.error("Gave up retrying reg IDs: %r", pushkeys)
            # Count the number of failed devices.
            span_parent.set_tag("hcm_num_failed", len(failed))
            return failed

    @staticmethod
    def _build_data(n: Notification, device: Device) -> Optional[Dict[str, Any]]:
        """
        Build the payload data to be sent.
        Args:
            n: Notification to build the payload for.
            device: Device information to which the constructed payload
            will be sent.

        Returns:
            JSON-compatible dict or None if the default_payload is misconfigured
        """

        data = {}

        if device.data:
            default_payload = device.data.get("default_payload", {})
            if isinstance(default_payload, dict):
                data.update(default_payload)
            else:
                logger.error(
                    "default_payload was misconfigured, this value must be a dict."
                )
                return None

        for attr in [
            "event_id",
            "type",
            "sender",
            "room_name",
            "room_alias",
            "membership",
            "sender_display_name",
            "content",
            "room_id",
        ]:
            if hasattr(n, attr):
                data[attr] = getattr(n, attr)
                # Truncate fields to a sensible maximum length. If the whole
                # body is too long, HCM will reject it.
                if data[attr] is not None and len(data[attr]) > MAX_BYTES_PER_FIELD:
                    data[attr] = data[attr][0:MAX_BYTES_PER_FIELD]

        data["prio"] = "high"
        if n.prio == "low":
            data["prio"] = "normal"

        if getattr(n, "counts", None):
            data["unread"] = n.counts.unread
            data["missed_calls"] = n.counts.missed_calls

        return data


# 
import json
import asyncio
import logging
import ipaddress
import traceback
from typing import Optional
from datetime import datetime
from contextlib import suppress
from pydantic import BaseModel, validator

from .helpers import *
from bbot.core.errors import *
from bbot.core.helpers import (
    extract_words,
    split_host_port,
    host_in_host,
    is_domain,
    is_subdomain,
    is_ip,
    is_ptr,
    domain_stem,
    make_netloc,
    make_ip_type,
    smart_decode,
    get_file_extension,
    validators,
    smart_decode_punycode,
    tagify,
)


log = logging.getLogger("bbot.core.event")


class BaseEvent:
    # Always emit this event type even if it's not in scope
    _always_emit = False
    # Always emit events with these tags even if they're not in scope
    _always_emit_tags = ["affiliate"]
    # Exclude from output modules
    _omit = False
    # Disables certain data validations
    _dummy = False
    # Data validation, if data is a dictionary
    _data_validator = None

    def __init__(
        self,
        data,
        event_type=None,
        source=None,
        module=None,
        scan=None,
        scans=None,
        tags=None,
        confidence=5,
        timestamp=None,
        _dummy=False,
        _internal=None,
    ):
        self._id = None
        self._hash = None
        self.__host = None
        self._port = None
        self.__words = None
        self._priority = None
        self._module_priority = None
        self._resolved_hosts = set()

        self._made_internal = False
        # whether to force-send to output modules
        self._force_output = False
        # keep track of whether this event has been recorded by the scan
        self._stats_recorded = False

        self.timestamp = datetime.utcnow()

        self._tags = set()
        if tags is not None:
            self._tags = set(tagify(s) for s in tags)

        self._data = None
        self._type = event_type
        self.confidence = int(confidence)

        # for creating one-off events without enforcing source requirement
        self._dummy = _dummy
        self._internal = False

        self.module = module
        # self.scan holds the instantiated scan object (for helpers, etc.)
        self.scan = scan
        if (not self.scan) and (not self._dummy):
            raise ValidationError(f"Must specify scan")
        # self.scans holds a list of scan IDs from scans that encountered this event
        self.scans = []
        if scans is not None:
            self.scans = scans
        if self.scan:
            self.scans = list(set([self.scan.id] + self.scans))

        # check type blacklist
        self._check_omit()

        self._scope_distance = -1

        try:
            self.data = self._sanitize_data(data)
        except Exception as e:
            log.trace(traceback.format_exc())
            raise ValidationError(f'Error sanitizing event data "{data}" for type "{self.type}": {e}')

        if not self.data:
            raise ValidationError(f'Invalid event data "{data}" for type "{self.type}"')

        self._source = None
        self._source_id = None
        self.source = source
        if (not self.source) and (not self._dummy):
            raise ValidationError(f"Must specify event source")

        # internal events are not ingested by output modules
        if not self._dummy:
            # removed this second part because it was making certain sslcert events internal
            if _internal:  # or source._internal:
                self.make_internal()

        # an event indicating whether the event has undergone DNS resolution
        self._resolved = asyncio.Event()

    @property
    def data(self):
        return self._data

    @property
    def resolved_hosts(self):
        if is_ip(self.host):
            return {
                self.host,
            }
        return self._resolved_hosts

    @data.setter
    def data(self, data):
        self._hash = None
        self._id = None
        self.__host = None
        self._port = None
        self._data = data

    @property
    def host(self):
        """
        An abbreviated representation of the data that allows comparison with other events.
        For host types, this is a hostname.
        This allows comparison of an email or a URL with a domain, and vice versa
            bob@evilcorp.com        --> evilcorp.com
            https://evilcorp.com    --> evilcorp.com
            evilcorp.com:80         --> evilcorp.com

        For IP_* types, this is an instantiated object representing the event's data
        E.g. for IP_ADDRESS, it could be an ipaddress.IPv4Address() or IPv6Address() object
        """
        if self.__host is None:
            self.__host = self._host()
        return self.__host

    @property
    def port(self):
        self.host
        if getattr(self, "parsed", None):
            if self.parsed.port is not None:
                return self.parsed.port
            elif self.parsed.scheme == "https":
                return 443
            elif self.parsed.scheme == "http":
                return 80
        return self._port

    @property
    def host_stem(self):
        """
        An abbreviated representation of hostname that removes the TLD
            E.g. www.evilcorp.com --> www.evilcorp
        """
        if self.host and type(self.host) == str:
            return domain_stem(self.host)
        else:
            return f"{self.host}"

    @property
    def words(self):
        if self.__words is None:
            self.__words = set(self._words())
        return self.__words

    def _words(self):
        return set()

    @property
    def tags(self):
        return self._tags

    @tags.setter
    def tags(self, tags):
        if isinstance(tags, str):
            tags = (tags,)
        self._tags = set(tagify(s) for s in tags)

    def add_tag(self, tag):
        self._tags.add(tagify(tag))

    def remove_tag(self, tag):
        with suppress(KeyError):
            self._tags.remove(tagify(tag))

    @property
    def always_emit(self):
        return self._always_emit or any(t in self.tags for t in self._always_emit_tags)

    @property
    def id(self):
        if self._id is None:
            self._id = make_event_id(self.data_id, self.type)
        return self._id

    @property
    def scope_distance(self):
        return self._scope_distance

    @scope_distance.setter
    def scope_distance(self, scope_distance):
        if scope_distance >= 0:
            new_scope_distance = None
            # ensure scope distance does not increase (only allow setting to smaller values)
            if self.scope_distance == -1:
                new_scope_distance = scope_distance
            else:
                new_scope_distance = min(self.scope_distance, scope_distance)
            if self._scope_distance != new_scope_distance:
                self._scope_distance = new_scope_distance
                for t in list(self.tags):
                    if t.startswith("distance-"):
                        self.remove_tag(t)
                self.add_tag(f"distance-{new_scope_distance}")

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, source):
        if is_event(source):
            self._source = source
            if source.scope_distance >= 0:
                new_scope_distance = int(source.scope_distance)
                # only increment the scope distance if the host changes
                if self.host != source.host:
                    new_scope_distance += 1
                self.scope_distance = new_scope_distance
        elif not self._dummy:
            log.warning(f"Tried to set invalid source on {self}: (got: {source})")

    @property
    def source_id(self):
        source_id = getattr(self.get_source(), "id", None)
        if source_id is not None:
            return source_id
        return self._source_id

    def get_source(self):
        """
        Takes into account events with the _omit flag
        """
        if getattr(self.source, "_omit", False):
            return self.source.get_source()
        return self.source

    def get_sources(self, omit=False):
        sources = []
        e = self
        while 1:
            if omit:
                source = e.get_source()
            else:
                source = e.source
            if e == source:
                break
            sources.append(source)
            e = source
        return sources

    def make_internal(self):
        if not self._made_internal:
            self._internal = True
            self.add_tag("internal")
            self._made_internal = True

    def unmake_internal(self, set_scope_distance=None, force_output=False):
        source_trail = []
        self.remove_tag("internal")
        if self._made_internal:
            if set_scope_distance is not None:
                self.scope_distance = set_scope_distance
            self._internal = False
            self._made_internal = False
        if force_output is True:
            self._force_output = True
        if force_output == "trail_only":
            force_output = True

        # if our source event is internal, unmake it too
        if getattr(self.source, "_internal", False):
            source_scope_distance = None
            if set_scope_distance is not None:
                source_scope_distance = set_scope_distance + 1
            source_trail += self.source.unmake_internal(
                set_scope_distance=source_scope_distance, force_output=force_output
            )
            source_trail.append(self.source)

        return source_trail

    def set_scope_distance(self, d=0):
        """
        Set the scope of an event and its parents
        """
        source_trail = []
        # keep the event internal if the module requests so, unless it's a DNS_NAME
        if getattr(self.module, "_scope_shepherding", True) or self.type in ("DNS_NAME",):
            source_trail = self.unmake_internal(set_scope_distance=d, force_output="trail_only")
        self.scope_distance = d
        if d == 0:
            self.add_tag("in-scope")
        return source_trail

    def _host(self):
        return ""

    def _sanitize_data(self, data):
        data = self._data_load(data)
        if self._data_validator is not None:
            if not isinstance(data, dict):
                raise ValidationError(f"data is not of type dict: {data}")
            data = self._data_validator(**data).dict()
            data = {k: v for k, v in data.items() if v is not None}
        return self.sanitize_data(data)

    def sanitize_data(self, data):
        return data

    @property
    def data_human(self):
        """
        Human representation of event.data
        """
        return self._data_human()

    def _data_human(self):
        return str(self.data)

    def _data_load(self, data):
        """
        How to load the event data (JSON-decode it, etc.)
        """
        return data

    @property
    def data_id(self):
        """
        Representation of the event.data used to calculate the event's ID
        """
        return self._data_id()

    def _data_id(self):
        return self.data

    @property
    def pretty_string(self):
        """
        Graph representation of event.data
        """
        return self._pretty_string()

    def _pretty_string(self):
        if isinstance(self.data, dict):
            with suppress(Exception):
                return json.dumps(self.data, sort_keys=True)
        return smart_decode(self.data)

    @property
    def data_graph(self):
        """
        Representation of event.data for neo4j graph nodes
        """
        return self.pretty_string

    @property
    def data_json(self):
        """
        JSON representation of event.data
        """
        return self.data

    def __contains__(self, other):
        """
        Allows events to be compared using the "in" operator:
        E.g.:
            if some_event in other_event:
                ...
        """
        try:
            other = make_event(other, dummy=True)
        except ValidationError:
            return False
        # if hashes match
        if other == self:
            return True
        # if hosts match
        if self.host and other.host:
            if self.host == other.host:
                return True
            # hostnames and IPs
            return host_in_host(other.host, self.host)
        return False

    def json(self, mode="json"):
        j = dict()
        for i in ("type", "id"):
            v = getattr(self, i, "")
            if v:
                j.update({i: v})
        data_attr = getattr(self, f"data_{mode}", None)
        if data_attr is not None:
            j["data"] = data_attr
        else:
            j["data"] = smart_decode(self.data)
        web_spider_distance = getattr(self, "web_spider_distance", None)
        if web_spider_distance is not None:
            j["web_spider_distance"] = web_spider_distance
        j["scope_distance"] = self.scope_distance
        if self.scan:
            j["scan"] = self.scan.id
        j["timestamp"] = self.timestamp.timestamp()
        if self.host:
            j["resolved_hosts"] = [str(h) for h in self.resolved_hosts]
        source_id = self.source_id
        if source_id:
            j["source"] = source_id
        if self.tags:
            j.update({"tags": list(self.tags)})
        if self.module:
            j.update({"module": str(self.module)})
        if self.module_sequence:
            j.update({"module_sequence": str(self.module_sequence)})

        # normalize non-primitive python objects
        for k, v in list(j.items()):
            if k == "data":
                continue
            if type(v) not in (str, int, float, bool, list, type(None)):
                try:
                    j[k] = json.dumps(v, sort_keys=True)
                except Exception:
                    j[k] = smart_decode(v)
        return j

    @staticmethod
    def from_json(j):
        return event_from_json(j)

    @property
    def module_sequence(self):
        """
        A human-friendly representation of the module name that includes modules from omitted source events

        Helpful in identifying where a URL came from
        """
        module_name = getattr(self.module, "name", "")
        if getattr(self.source, "_omit", False):
            module_name = f"{self.source.module_sequence}->{module_name}"
        return module_name

    @property
    def module_priority(self):
        if self._module_priority is None:
            module = getattr(self, "module", None)
            self._module_priority = int(max(1, min(5, getattr(module, "priority", 3))))
        return self._module_priority

    @module_priority.setter
    def module_priority(self, priority):
        self._module_priority = int(max(1, min(5, priority)))

    @property
    def priority(self):
        if self._priority is None:
            timestamp = self.timestamp.timestamp()
            if self.source.timestamp == self.timestamp:
                self._priority = (timestamp,)
            else:
                self._priority = getattr(self.source, "priority", ()) + (timestamp,)

        return self._priority

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, val):
        self._type = val
        self._hash = None
        self._id = None
        self._check_omit()

    def _check_omit(self):
        if self.scan is not None:
            omit_event_types = self.scan.config.get("omit_event_types", [])
            if omit_event_types and self.type in omit_event_types:
                self._omit = True

    def __iter__(self):
        """
        For dict(event)
        """
        yield from self.json().items()

    def __lt__(self, other):
        """
        For queue sorting
        """
        return self.priority < getattr(other, "priority", (0,))

    def __gt__(self, other):
        """
        For queue sorting
        """
        return self.priority > getattr(other, "priority", (0,))

    def __eq__(self, other):
        try:
            other = make_event(other, dummy=True)
        except ValidationError:
            return False
        return hash(self) == hash(other)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(self.id)
        return self._hash

    def __str__(self):
        max_event_len = 80
        d = str(self.data)
        return f'{self.type}("{d[:max_event_len]}{("..." if len(d) > max_event_len else "")}", module={self.module}, tags={self.tags})'

    def __repr__(self):
        return str(self)


class FINISHED(BaseEvent):
    """
    Special signal event to indicate end of scan
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._priority = (999999999999999999999,)


class DefaultEvent(BaseEvent):
    def sanitize_data(self, data):
        return data


class DictEvent(BaseEvent):
    def sanitize_data(self, data):
        url = data.get("url", "")
        if url:
            self.parsed = validators.validate_url_parsed(url)
        return data

    def _data_human(self):
        return json.dumps(self.data, sort_keys=True)

    def _data_load(self, data):
        if isinstance(data, str):
            return json.loads(data)
        return data


class DictHostEvent(DictEvent):
    def _host(self):
        if isinstance(self.data, dict) and "host" in self.data:
            return make_ip_type(self.data["host"])
        else:
            parsed = getattr(self, "parsed")
            if parsed is not None:
                return make_ip_type(parsed.hostname)


class ASN(DictEvent):
    _always_emit = True


class CODE_REPOSITORY(DictHostEvent):
    class _data_validator(BaseModel):
        url: str
        _validate_url = validator("url", allow_reuse=True)(validators.validate_url)

    def _pretty_string(self):
        return self.data["url"]


class IP_ADDRESS(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ip = ipaddress.ip_address(self.data)
        self.add_tag(f"ipv{ip.version}")
        if ip.is_private:
            self.add_tag("private")
        self.dns_resolve_distance = getattr(self.source, "dns_resolve_distance", 0)

    def sanitize_data(self, data):
        return validators.validate_host(data)

    def _host(self):
        return ipaddress.ip_address(self.data)


class DnsEvent(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # prevent runaway DNS entries
        self.dns_resolve_distance = 0
        source = getattr(self, "source", None)
        module = getattr(self, "module", None)
        module_type = getattr(module, "_type", "")
        source_module = getattr(source, "module", None)
        source_module_type = getattr(source_module, "_type", "")
        if module_type == "DNS":
            self.dns_resolve_distance = getattr(source, "dns_resolve_distance", 0)
            if source_module_type == "DNS":
                self.dns_resolve_distance += 1
        # self.add_tag(f"resolve-distance-{self.dns_resolve_distance}")


class IP_RANGE(DnsEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        net = ipaddress.ip_network(self.data, strict=False)
        self.add_tag(f"ipv{net.version}")

    def sanitize_data(self, data):
        return str(ipaddress.ip_network(str(data), strict=False))

    def _host(self):
        return ipaddress.ip_network(self.data)


class DNS_NAME(DnsEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if is_subdomain(self.data):
            self.add_tag("subdomain")
        elif is_domain(self.data):
            self.add_tag("domain")

    def sanitize_data(self, data):
        return validators.validate_host(data)

    def _host(self):
        return self.data

    def _words(self):
        stem = self.host_stem
        if not is_ptr(stem):
            split_stem = stem.split(".")
            if split_stem:
                leftmost_segment = split_stem[0]
                if leftmost_segment == "_wildcard":
                    stem = ".".join(split_stem[1:])
            if stem:
                return extract_words(stem)
        return set()


class OPEN_TCP_PORT(BaseEvent):
    def sanitize_data(self, data):
        return validators.validate_open_port(data)

    def _host(self):
        host, self._port = split_host_port(self.data)
        return host

    def _words(self):
        if not is_ip(self.host) and not is_ptr(self.host):
            return extract_words(self.host_stem)
        return set()


class URL_UNVERIFIED(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.web_spider_distance = getattr(self.source, "web_spider_distance", 0)
        # increment the web spider distance
        if self.type == "URL_UNVERIFIED" and getattr(self.module, "name", "") != "TARGET":
            self.web_spider_distance += 1
        self.num_redirects = getattr(self.source, "num_redirects", 0)

    def sanitize_data(self, data):
        self.parsed = validators.validate_url_parsed(data)

        # tag as dir or endpoint
        if str(self.parsed.path).endswith("/"):
            self.add_tag("dir")
        else:
            self.add_tag("endpoint")

        parsed_path_lower = str(self.parsed.path).lower()

        url_extension_blacklist = []
        url_extension_httpx_only = []
        scan = getattr(self, "scan", None)
        if scan is not None:
            _url_extension_blacklist = scan.config.get("url_extension_blacklist", [])
            _url_extension_httpx_only = scan.config.get("url_extension_httpx_only", [])
            if _url_extension_blacklist:
                url_extension_blacklist = [e.lower() for e in _url_extension_blacklist]
            if _url_extension_httpx_only:
                url_extension_httpx_only = [e.lower() for e in _url_extension_httpx_only]

        extension = get_file_extension(parsed_path_lower)
        if extension:
            self.add_tag(f"extension-{extension}")
            if extension in url_extension_blacklist:
                self.add_tag("blacklisted")
            if extension in url_extension_httpx_only:
                self.add_tag("httpx-only")
                self._omit = True

        data = self.parsed.geturl()
        return data

    def with_port(self):
        netloc_with_port = make_netloc(self.host, self.port)
        return self.parsed._replace(netloc=netloc_with_port)

    def _words(self):
        first_elem = self.parsed.path.lstrip("/").split("/")[0]
        if not "." in first_elem:
            return extract_words(first_elem)
        return set()

    def _host(self):
        return make_ip_type(self.parsed.hostname)

    def _data_id(self):
        # consider spider-danger tag when deduping
        data = super()._data_id()
        if "spider-danger" in self.tags:
            data = "spider-danger" + data
        return data


class URL(URL_UNVERIFIED):
    def sanitize_data(self, data):
        if not self._dummy and not any(t.startswith("status-") for t in self.tags):
            raise ValidationError(
                'Must specify HTTP status tag for URL event, e.g. "status-200". Use URL_UNVERIFIED if the URL is unvisited.'
            )
        return super().sanitize_data(data)

    @property
    def resolved_hosts(self):
        return [".".join(i.split("-")[1:]) for i in self.tags if i.startswith("ip-")]

    @property
    def pretty_string(self):
        return self.data


class STORAGE_BUCKET(DictEvent, URL_UNVERIFIED):
    _always_emit = True

    class _data_validator(BaseModel):
        name: str
        url: str

    def _words(self):
        return self.data["name"]


class URL_HINT(URL_UNVERIFIED):
    pass


class EMAIL_ADDRESS(BaseEvent):
    def sanitize_data(self, data):
        return validators.validate_email(data)

    def _host(self):
        data = str(self.data).split("@")[-1]
        host, self._port = split_host_port(data)
        return host

    def _words(self):
        return extract_words(self.host_stem)


class HTTP_RESPONSE(URL_UNVERIFIED, DictEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # count number of consecutive redirects
        self.num_redirects = getattr(self.source, "num_redirects", 0)
        if str(self.data.get("status_code", 0)).startswith("3"):
            self.num_redirects += 1

    def sanitize_data(self, data):
        url = data.get("url", "")
        self.parsed = validators.validate_url_parsed(url)

        header_dict = {}
        for i in data.get("raw_header", "").splitlines():
            if len(i) > 0 and ":" in i:
                k, v = i.split(":", 1)
                k = k.strip().lower()
                v = v.lstrip()
                header_dict[k] = v
        data["header-dict"] = header_dict
        # move URL to the front of the dictionary for visibility
        data = dict(data)
        new_data = {"url": data.pop("url")}
        new_data.update(data)

        return new_data

    def _words(self):
        return set()

    def _pretty_string(self):
        return f'{self.data["hash"]["header_mmh3"]}:{self.data["hash"]["body_mmh3"]}'


class VULNERABILITY(DictHostEvent):
    _always_emit = True

    def sanitize_data(self, data):
        self.add_tag(data["severity"].lower())
        return data

    class _data_validator(BaseModel):
        host: str
        severity: str
        description: str
        url: Optional[str]
        _validate_host = validator("host", allow_reuse=True)(validators.validate_host)
        _validate_severity = validator("severity", allow_reuse=True)(validators.validate_severity)

    def _pretty_string(self):
        return f'[{self.data["severity"]}] {self.data["description"]}'


class FINDING(DictHostEvent):
    _always_emit = True

    class _data_validator(BaseModel):
        host: str
        description: str
        url: Optional[str]
        _validate_host = validator("host", allow_reuse=True)(validators.validate_host)

    def _pretty_string(self):
        return self.data["description"]


class TECHNOLOGY(DictHostEvent):
    class _data_validator(BaseModel):
        host: str
        technology: str
        url: Optional[str]
        _validate_host = validator("host", allow_reuse=True)(validators.validate_host)

    def _data_id(self):
        # dedupe by host+port+tech
        tech = self.data.get("technology", "")
        return f"{self.host}:{self.port}:{tech}"

    def _pretty_string(self):
        return self.data["technology"]


class VHOST(DictHostEvent):
    class _data_validator(BaseModel):
        host: str
        vhost: str
        url: Optional[str]
        _validate_host = validator("host", allow_reuse=True)(validators.validate_host)

    def _pretty_string(self):
        return self.data["vhost"]


class PROTOCOL(DictHostEvent):
    class _data_validator(BaseModel):
        host: str
        protocol: str
        port: Optional[int]
        banner: Optional[str]
        _validate_host = validator("host", allow_reuse=True)(validators.validate_host)
        _validate_port = validator("port", allow_reuse=True)(validators.validate_port)

    def sanitize_data(self, data):
        new_data = dict(data)
        new_data["protocol"] = data.get("protocol", "").upper()
        return new_data

    @property
    def port(self):
        return self.data.get("port", None)

    def _pretty_string(self):
        return self.data["protocol"]


class GEOLOCATION(BaseEvent):
    _always_emit = True


class SOCIAL(DictEvent):
    _always_emit = True


class WEBSCREENSHOT(DictHostEvent):
    _always_emit = True


def make_event(
    data,
    event_type=None,
    source=None,
    module=None,
    scan=None,
    scans=None,
    tags=None,
    confidence=5,
    dummy=False,
    internal=None,
):
    """
    If data is already an event, simply return it
    """

    # allow tags to be either a string or an array
    if isinstance(tags, str):
        tags = [tags]

    if is_event(data):
        if scan is not None and not data.scan:
            data.scan = scan
        if scans is not None and not data.scans:
            data.scans = scans
        if module is not None:
            data.module = module
        if source is not None:
            data.source = source
        if internal == True and not data._made_internal:
            data.make_internal()
        event_type = data.type
        return data
    else:
        if event_type is None:
            if isinstance(data, str):
                data = smart_decode_punycode(data)
            event_type = get_event_type(data)
            if not dummy:
                log.debug(f'Autodetected event type "{event_type}" based on data: "{data}"')

        event_type = str(event_type).strip().upper()

        # Catch these common whoopsies
        if event_type in ("DNS_NAME", "IP_ADDRESS"):
            # DNS_NAME <--> EMAIL_ADDRESS confusion
            if validators.soft_validate(data, "email"):
                event_type = "EMAIL_ADDRESS"
            else:
                # DNS_NAME <--> IP_ADDRESS confusion
                try:
                    data = validators.validate_host(data)
                except Exception as e:
                    log.trace(traceback.format_exc())
                    raise ValidationError(f'Error sanitizing event data "{data}" for type "{event_type}": {e}')
                data_is_ip = is_ip(data)
                if event_type == "DNS_NAME" and data_is_ip:
                    event_type = "IP_ADDRESS"
                elif event_type == "IP_ADDRESS" and not data_is_ip:
                    event_type = "DNS_NAME"

        event_class = globals().get(event_type, DefaultEvent)

        return event_class(
            data,
            event_type=event_type,
            source=source,
            module=module,
            scan=scan,
            scans=scans,
            tags=tags,
            confidence=confidence,
            _dummy=dummy,
            _internal=internal,
        )


def event_from_json(j):
    try:
        kwargs = {
            "data": j["data"],
            "event_type": j["type"],
            "scans": j.get("scans", []),
            "tags": j.get("tags", []),
            "confidence": j.get("confidence", 5),
            "dummy": True,
        }
        event = make_event(**kwargs)
        event.timestamp = datetime.fromtimestamp(j["timestamp"])
        event.scope_distance = j["scope_distance"]
        source_id = j.get("source", None)
        if source_id is not None:
            event._source_id = source_id
        return event
    except KeyError as e:
        raise ValidationError(f"Event missing required field: {e}")


def is_event(e):
    return BaseEvent in e.__class__.__mro__

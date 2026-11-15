import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

_DATASET_ID = re.compile(r'^[A-Za-z0-9][\w.-]{0,95}(?:/[\w.-]{1,96})?$')
_ARXIV_ID = re.compile(r'^\d{4}\.\d{4,5}(?:v\d+)?$')
_MAX_RESPONSE_BYTES = 1_000_000
_NEGATIVE_TTL = 60.0


class FakeHubClient:
    def __init__(self, source):
        self.data = json.loads(Path(source).read_text()) if not isinstance(source, dict) else source
        self.calls = []

    def get_dataset(self, dataset_id):
        self.calls.append(dataset_id)
        return self.data.get(dataset_id)


class FakePaperClient:
    def __init__(self, source):
        self.data = json.loads(Path(source).read_text()) if not isinstance(source, dict) else source
        self.calls = []

    def get_abstract(self, arxiv_id):
        self.calls.append(arxiv_id)
        return self.data.get(arxiv_id)


class _Cached:
    def __init__(self, cache_dir, ttl=604800):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ttl = ttl
        self._cache_cap = 50 * 1024 * 1024
        self._cache_lock = threading.RLock()
        self._cache_serial = 0
        self._cache_order = {}
        metadata = self.cache.lstat()
        uid = getattr(os, 'geteuid', lambda: None)()
        if not stat.S_ISDIR(metadata.st_mode) or self.cache.is_symlink() or (uid is not None and metadata.st_uid != uid):
            raise PermissionError('cache path must be a real directory owned by this user')
        self.cache.chmod(0o700)
        self._prune()

    @property
    def cache_cap(self):
        return self._cache_cap

    @cache_cap.setter
    def cache_cap(self, value):
        value = int(value)
        if value < 0:
            raise ValueError('cache capacity must be non-negative')
        with self._cache_lock:
            self._cache_cap = value
            # Enforce a changed capacity immediately, removing expired entries
            # first and then evicting by current filesystem modification time.
            self._prune()

    def _path(self, key):
        return self.cache / (urllib.parse.quote(key, safe='') + '.json')

    def _validate_cached(self, key, value):
        return False

    def _forget(self, path):
        self._cache_order.pop(path, None)

    def _prune(self, incoming=0, replacing=None):
        """Remove expired files, then evict by actual filesystem mtime.

        Write sequence only breaks equal-mtime ties. Never advance timestamps
        artificially: doing so changes both eviction order and TTL behaviour.
        """
        with self._cache_lock:
            incoming = max(0, int(incoming))
            replacing = Path(replacing) if replacing is not None else None
            current_time = time.time()
            entries = []
            total = 0
            for path in list(self.cache.iterdir()):
                try:
                    metadata = path.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    if path.name.endswith('.tmp') or current_time - metadata.st_mtime >= self.ttl:
                        path.unlink(missing_ok=True)
                        self._forget(path)
                        continue
                    if replacing is not None and path == replacing:
                        continue
                    entries.append((metadata.st_mtime_ns, self._cache_order.get(path, 0), path.name, path, metadata.st_size))
                    total += metadata.st_size
                except FileNotFoundError:
                    self._forget(path)

            for _, _, _, path, amount in sorted(entries):
                if total + incoming <= self._cache_cap:
                    break
                path.unlink(missing_ok=True)
                total -= amount
                self._forget(path)

    def _get(self, key):
        with self._cache_lock:
            self._prune()
            path = self._path(key)
            try:
                metadata = path.lstat()
                uid = getattr(os, 'geteuid', lambda: None)()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or path.is_symlink()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or (uid is not None and metadata.st_uid != uid)
                    or time.time() - metadata.st_mtime >= self.ttl
                    or metadata.st_size > self._cache_cap
                ):
                    raise ValueError
                value = json.loads(path.read_text())
                if not self._validate_cached(key, value) or (
                    value.get('_negative_404')
                    and time.time() - value['_cached_at'] >= _NEGATIVE_TTL
                ):
                    raise ValueError
                return value
            except Exception:
                path.unlink(missing_ok=True)
                self._forget(path)
                return None

    def _put(self, key, value):
        if not self._validate_cached(key, value):
            raise ValueError
        raw = json.dumps(value).encode()
        with self._cache_lock:
            path = self._path(key)
            if len(raw) > self._cache_cap:
                path.unlink(missing_ok=True)
                self._forget(path)
                self._prune()
                return
            self._prune(incoming=len(raw), replacing=path)
            temporary = path.with_name(path.name + '-' + uuid.uuid4().hex + '.tmp')
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                os.replace(temporary, path)
                self._cache_serial += 1
                self._cache_order[path] = self._cache_serial
            finally:
                temporary.unlink(missing_ok=True)
            self._prune()


class _Throttled:
    _global_lock = threading.Lock()
    _global_last = 0.0
    _global_stop = 0.0

    def _wait_for_slot(self, deadline):
        with self._global_lock:
            current = time.monotonic()
            if self.cooldown and current < self.__class__._global_stop:
                return False
            previous = self.__class__._global_last
            wait = max(0, self.minimum_interval - (current - previous)) if previous else 0
            if current + wait >= deadline:
                return False
            self.__class__._global_last = current + wait
        if wait:
            time.sleep(wait)
        return time.monotonic() < deadline


class _HubSession:
    def __init__(self, hub, deadline, max_fetches):
        self.hub = hub
        self.deadline = deadline
        self.remaining = max_fetches

    def get_dataset(self, dataset_id):
        return self.hub.get_dataset(dataset_id, self)


class RealHubClient(_Cached, _Throttled):
    def __init__(self, cache_dir, request_budget=15.0, minimum_interval=1.0, ttl=604800, opener=None, allowed_host='huggingface.co', cooldown=0):
        super().__init__(Path(cache_dir) / 'hub', ttl)
        self.request_budget = max(1, float(request_budget))
        self.minimum_interval = max(0, float(minimum_interval))
        self.opener = opener
        self.allowed_host = allowed_host
        self.cooldown = max(0, float(cooldown))

    def session(self, deadline=None, max_fetches=40):
        return _HubSession(self, deadline if deadline is not None else time.monotonic() + self.request_budget, max_fetches)

    def _validate_cached(self, key, value):
        required = {'id', 'card_text', 'card_data', 'tags', 'partial', '_limits_hit'}
        if not (
            isinstance(value, dict)
            and required <= set(value)
            and not set(value) - required - {'_negative_404', '_cached_at', '_parts'}
            and value['id'] == key
            and isinstance(value['card_text'], str)
            and isinstance(value['card_data'], dict)
            and isinstance(value['tags'], list)
            and type(value['partial']) is bool
            and isinstance(value['_limits_hit'], list)
            and (not value.get('_negative_404') or isinstance(value.get('_cached_at'), (int, float)))
        ):
            return False
        parts = value.get('_parts', {})
        return (
            isinstance(parts, dict)
            and not set(parts) - {'metadata', 'card'}
            and all(
                isinstance(response, dict)
                and response.get('status') in (200, 404)
                and isinstance(response.get('text'), (str, type(None)))
                and not response.get('truncated')
                for response in parts.values()
            )
        )

    def _allowed(self, url):
        try:
            parsed = urllib.parse.urlsplit(url)
            return (
                parsed.scheme == 'https'
                and parsed.hostname == self.allowed_host
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    @staticmethod
    def _failure(limit):
        return {'status': 0, 'text': None, 'truncated': False, 'local_limit': False, 'limit': limit}

    def _read(self, url, deadline, session=None, redirects=0):
        if not self._allowed(url):
            return self._failure('HUB_REQUEST_FAILED')
        if time.monotonic() >= deadline:
            return self._failure('TIME_LIMIT')
        if session is not None and session.remaining <= 0:
            return self._failure('HUB_REQUEST_BUDGET')
        if self.cooldown and time.monotonic() < self.__class__._global_stop:
            return self._failure('HUB_RATE_LIMITED')
        if not self._wait_for_slot(deadline):
            limit = 'HUB_RATE_LIMITED' if self.cooldown and time.monotonic() < self.__class__._global_stop else 'TIME_LIMIT'
            return self._failure(limit)
        if self.cooldown and time.monotonic() < self.__class__._global_stop:
            return self._failure('HUB_RATE_LIMITED')
        if session is not None:
            session.remaining -= 1
        response = None
        timer = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._failure('TIME_LIMIT')
            request = urllib.request.Request(url, headers={'User-Agent': 'DataLineage/0.1'})
            opened = self.opener.open if self.opener else urllib.request.urlopen
            with opened(request, timeout=min(10, remaining)) as response:
                if not self._allowed(response.geturl() if hasattr(response, 'geturl') else url):
                    raise OSError('redirect refused')

                def abort():
                    import socket
                    try:
                        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                    except (AttributeError, OSError):
                        pass

                timer = threading.Timer(max(0, deadline - time.monotonic()), abort)
                timer.daemon = True
                timer.start()
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if time.monotonic() >= deadline:
                    return self._failure('TIME_LIMIT')
                return {
                    'status': int(getattr(response, 'status', 200)),
                    'text': raw[:_MAX_RESPONSE_BYTES].decode('utf-8', 'replace'),
                    'truncated': len(raw) > _MAX_RESPONSE_BYTES,
                    'local_limit': False,
                }
        except urllib.error.HTTPError as error:
            try:
                if error.code in (301, 302, 303, 307, 308):
                    target = urllib.parse.urljoin(url, error.headers.get('Location', ''))
                    if redirects >= 3 or not self._allowed(target):
                        return self._failure('HUB_REQUEST_FAILED')
                    return self._read(target, deadline, session, redirects + 1)
                if error.code == 429 and self.cooldown:
                    with self._global_lock:
                        self.__class__._global_stop = max(
                            self.__class__._global_stop,
                            time.monotonic() + self.cooldown,
                        )
                return {'status': error.code, 'text': None, 'truncated': False, 'local_limit': False}
            finally:
                error.close()
        except (OSError, TimeoutError, ValueError):
            return self._failure('TIME_LIMIT' if time.monotonic() >= deadline else 'HUB_REQUEST_FAILED')
        finally:
            if timer is not None:
                timer.cancel()

    @staticmethod
    def _limits(*responses):
        output = []
        for response in responses:
            if response.get('limit'):
                output.append(response['limit'])
                continue
            name = {
                401: 'HUB_UNAUTHORIZED',
                403: 'HUB_FORBIDDEN',
                404: 'HUB_NOT_FOUND',
                429: 'HUB_RATE_LIMITED',
            }.get(response['status'])
            if response.get('local_limit'):
                name = 'HUB_REQUEST_BUDGET'
            if response['status'] not in (200, 401, 403, 404, 429):
                output.append('HUB_REQUEST_FAILED')
            if name:
                output.append(name)
            if response.get('truncated'):
                output.append('HUB_RESPONSE_TRUNCATED')
        return list(dict.fromkeys(output))

    def get_dataset(self, dataset_id, session=None):
        if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
            return None
        cached = self._get(dataset_id)
        if cached is not None and (not cached['partial'] or cached.get('_negative_404')):
            return cached
        session = session or self.session()
        deadline = session.deadline
        parts = dict((cached or {}).get('_parts', {}))
        escaped = urllib.parse.quote(dataset_id, safe='/')
        metadata_response = parts.get('metadata') or self._read(
            'https://huggingface.co/api/datasets/' + escaped,
            deadline,
            session,
        )
        card_response = parts.get('card') or self._read(
            'https://huggingface.co/datasets/' + escaped + '/raw/main/README.md',
            deadline,
            session,
        )
        limits = self._limits(metadata_response, card_response)
        try:
            metadata = json.loads(metadata_response['text']) if metadata_response.get('text') else {}
            if not isinstance(metadata, dict):
                raise ValueError
        except (ValueError, TypeError):
            metadata = {}
            limits.append('HUB_INVALID_RESPONSE')
            metadata_response = {**metadata_response, 'status': 0}
        metadata_only = metadata_response['status'] == 200 and card_response['status'] == 404
        if metadata_only:
            limits = [item for item in limits if item != 'HUB_NOT_FOUND']
        from .extract import parse_front_matter
        card = card_response.get('text') or ''
        data = dict(metadata.get('cardData') or {}) if isinstance(metadata.get('cardData'), dict) else {}
        data.update(parse_front_matter(card.removeprefix('\ufeff')))
        tags = metadata.get('tags', [])
        tags = [item for item in tags if isinstance(item, str)] if isinstance(tags, list) else []
        complete = metadata_response['status'] == 200 and card_response['status'] in (200, 404) and not limits
        result = {
            'id': dataset_id,
            'card_text': card,
            'card_data': data,
            'tags': tags,
            'partial': not complete,
            '_limits_hit': list(dict.fromkeys(limits)),
        }
        if metadata_response['status'] == card_response['status'] == 404:
            result.update(_negative_404=True, _cached_at=time.time())
            self._put(dataset_id, result)
        elif complete:
            self._put(dataset_id, result)
        else:
            successful = {
                key: response
                for key, response in [('metadata', metadata_response), ('card', card_response)]
                if response['status'] in (200, 404) and not response.get('truncated')
            }
            if successful:
                result['_parts'] = successful
                self._put(dataset_id, result)
        return result


class RealPaperClient(_Cached, _Throttled):
    def __init__(self, cache_dir, request_budget=12, minimum_interval=1, ttl=604800):
        super().__init__(Path(cache_dir) / 'papers', ttl)
        self.request_budget = float(request_budget)
        self.minimum_interval = float(minimum_interval)
        self.cooldown = 0

    def _validate_cached(self, key, value):
        return isinstance(value, dict) and set(value) == {'id', 'abstract'} and value['id'] == key

    def get_abstract(self, arxiv_id):
        return None

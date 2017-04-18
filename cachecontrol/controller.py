"""
The httplib2 algorithms ported for use with requests.
"""
import hashlib
import logging
import re
import calendar
import time
from email.utils import parsedate_tz

from requests.structures import CaseInsensitiveDict

from .cache import DictCache, keymaker
from .serialize import Serializer


logger = logging.getLogger(__name__)

URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")


def value_as_int(di, key, default=0):
    try:
        value = int(di.get(key, default))
    except ValueError:
        value = default
    return value


def parse_uri(uri):
    """Parses a URI using the regex given in Appendix B of RFC 3986.

        (scheme, authority, path, query, fragment) = parse_uri(uri)
    """
    groups = URI.match(uri).groups()
    return (groups[1], groups[3], groups[4], groups[6], groups[8])


class CacheController(object):
    """An interface to see if request should cached or not.
    """
    def __init__(self, cache=None, cache_etags=True, serializer=None,
                 status_codes=None):
        self.cache = cache or DictCache()
        self.cache_etags = cache_etags
        self.serializer = serializer or Serializer()
        self.cacheable_status_codes = status_codes or (200, 203, 300, 301)

    @classmethod
    def _urlnorm(cls, uri):
        """Normalize the URL to create a safe key for the cache"""
        (scheme, authority, path, query, fragment) = parse_uri(uri)
        if not scheme or not authority:
            raise Exception("Only absolute URIs are allowed. uri = %s" % uri)

        scheme = scheme.lower()
        authority = authority.lower()

        if not path:
            path = "/"

        # Could do syntax based normalization of the URI before
        # computing the digest. See Section 6.2.2 of Std 66.
        request_uri = query and "?".join([path, query]) or path
        defrag_uri = scheme + "://" + authority + request_uri

        return defrag_uri

    @classmethod
    def _authorization_digest(cls, authorization_header):
        """Creates a hash digest of a request's authorization header.
        """
        if not authorization_header:
            return ''
        return hashlib.sha224(authorization_header.encode('utf-8')).hexdigest()

    @classmethod
    def cache_key(cls, request):
        """Create a cache_key from a url and request headers.

        Requests are generally stored by their URL.
        For requests that have an Authorization header, we add a hash of that header to the cache key.
        The hash is only used to make sure we allow caching for each user - it's not a security feature.
        """
        normalized_url = cls._urlnorm(request.url)
        authorization_hash = cls._authorization_digest(request.headers.get('Authorization'))
        return keymaker(normalized_url, authorization_hash)

    def parse_cache_control(self, headers):
        """
        Parse the cache control headers returning a dictionary with values
        for the different directives.
        """
        retval = {}

        cc_header = 'cache-control'
        if 'Cache-Control' in headers:
            cc_header = 'Cache-Control'

        if cc_header in headers:
            parts = headers[cc_header].split(',')
            parts_with_args = [
                tuple([x.strip().lower() for x in part.split("=", 1)])
                for part in parts if -1 != part.find("=")
            ]
            parts_wo_args = [
                (name.strip().lower(), 1)
                for name in parts if -1 == name.find("=")
            ]
            retval = dict(parts_with_args + parts_wo_args)
        return retval

    def cached_request(self, request):
        """
        Return a cached response if it exists in the cache, otherwise
        return False.
        """
        cc = self.parse_cache_control(request.headers)

        # Bail out if the request insists on fresh data
        if 'no-cache' in cc:
            logger.debug('Request header has "no-cache", cache bypassed')
            return False

        if value_as_int(cc, 'max-age', default=-1) == 0:
            logger.debug('Request header has "max_age" as 0, cache bypassed')
            return False

        # Request allows serving from the cache, let's see if we find something
        cache_key = self.cache_key(request)
        logger.debug('Looking up "%s" in the cache', cache_key)
        cache_data = self.cache.get(cache_key)
        if cache_data is None:
            logger.debug('No cache entry available')
            return False

        # Check whether it can be deserialized
        resp = self.serializer.loads(request, cache_data)
        if not resp:
            logger.warning('Cache entry deserialization failed, entry ignored')
            return False

        # If we have a cached 301, return it immediately. We don't
        # need to test our response for other headers b/c it is
        # intrinsically "cacheable" as it is Permanent.
        # See:
        #   https://tools.ietf.org/html/rfc7231#section-6.4.2
        #
        # Client can try to refresh the value by repeating the request
        # with cache busting headers as usual (ie no-cache).
        if resp.status == 301:
            msg = ('Returning cached "301 Moved Permanently" response '
                   '(ignoring date and etag information)')
            logger.debug(msg)
            return resp

        headers = CaseInsensitiveDict(resp.headers)
        if not headers or 'date' not in headers:
            if 'etag' not in headers:
                # Without date or etag, the cached response can never be used
                # and should be deleted.
                logger.debug('Purging cached response: no date or etag')
                self.cache.delete(cache_key)
            logger.debug('Ignoring cached response: no date')
            return False

        now = time.time()
        date = calendar.timegm(
            parsedate_tz(headers['date'])
        )
        current_age = max(0, now - date)
        logger.debug('Current age based on date: %i', current_age)

        # TODO: There is an assumption that the result will be a
        #       urllib3 response object. This may not be best since we
        #       could probably avoid instantiating or constructing the
        #       response until we know we need it.
        resp_cc = self.parse_cache_control(headers)

        # determine freshness
        freshness_lifetime = 0

        # Check the max-age pragma in the cache control header
        if 'max-age' in resp_cc:
            freshness_lifetime = value_as_int(resp_cc, 'max-age')
            logger.debug('Freshness lifetime from max-age: %i',
                         freshness_lifetime)

        # If there isn't a max-age, check for an expires header
        elif 'expires' in headers:
            expires = parsedate_tz(headers['expires'])
            if expires is not None:
                expire_time = calendar.timegm(expires) - date
                freshness_lifetime = max(0, expire_time)
                logger.debug("Freshness lifetime from expires: %i",
                             freshness_lifetime)

        # Determine if we are setting freshness limit in the
        # request. Note, this overrides what was in the response.
        if 'max-age' in cc:
            freshness_lifetime = value_as_int(cc, 'max-age')
            logger.debug('Freshness lifetime from request max-age: %i',
                         freshness_lifetime)

        if 'min-fresh' in cc:
            # adjust our current age by our min fresh
            current_age += value_as_int(cc, 'min-fresh')
            logger.debug('Adjusted current age from min-fresh: %i',
                         current_age)

        # Return entry if it is fresh enough
        if freshness_lifetime > current_age:
            logger.debug('The response is "fresh", returning cached response')
            logger.debug('%i > %i', freshness_lifetime, current_age)
            return resp

        # we're not fresh. If we don't have an Etag, clear it out
        if 'etag' not in headers:
            logger.debug(
                'The cached response is "stale" with no etag, purging'
            )
            self.cache.delete(cache_key)

        # return the original handler
        return False

    def conditional_headers(self, request):
        cache_key = self.cache_key(request)
        resp = self.serializer.loads(request, self.cache.get(cache_key))
        new_headers = {}

        if resp:
            headers = CaseInsensitiveDict(resp.headers)

            if 'etag' in headers:
                new_headers['If-None-Match'] = headers['ETag']

            if 'last-modified' in headers:
                new_headers['If-Modified-Since'] = headers['Last-Modified']

        return new_headers

    def cache_response(self, request, response, body=None,
                       status_codes=None):
        """
        Algorithm for caching requests.

        This assumes a requests Response object.
        """
        # From httplib2: Don't cache 206's since we aren't going to
        #                handle byte range requests
        cacheable_status_codes = status_codes or self.cacheable_status_codes
        if response.status not in cacheable_status_codes:
            logger.debug(
                'Status code %s not in %s',
                response.status,
                cacheable_status_codes
            )
            return

        response_headers = CaseInsensitiveDict(response.headers)

        # If we've been given a body, our response has a Content-Length, that
        # Content-Length is valid then we can check to see if the body we've
        # been given matches the expected size, and if it doesn't we'll just
        # skip trying to cache it.
        if (body is not None and
                "content-length" in response_headers and
                response_headers["content-length"].isdigit() and
                int(response_headers["content-length"]) != len(body)):
            return

        cc_req = self.parse_cache_control(request.headers)
        cc = self.parse_cache_control(response_headers)
        cache_key = self.cache_key(request)
        logger.debug('Updating cache with response from "%s"', cache_key)

        # Delete it from the cache if we happen to have it stored there
        no_store = False
        if cc.get('no-store'):
            no_store = True
            logger.debug('Response header has "no-store"')
        if cc_req.get('no-store'):
            no_store = True
            logger.debug('Request header has "no-store"')
        if no_store and self.cache.get(cache_key):
            logger.debug('Purging existing cache entry to honor "no-store"')
            self.cache.delete(cache_key)

        # If we've been given an etag, then keep the response
        if self.cache_etags and 'etag' in response_headers:
            logger.debug('Caching due to etag')
            self.cache.set(
                cache_key,
                self.serializer.dumps(request, response, body=body),
            )

        # Add to the cache any 301s. We do this before looking that
        # the Date headers.
        elif response.status == 301:
            logger.debug('Caching permanant redirect')
            self.cache.set(
                cache_key,
                self.serializer.dumps(request, response)
            )

        # Add to the cache if the response headers demand it. If there
        # is no date header then we can't do anything about expiring
        # the cache.
        elif 'date' in response_headers:
            # cache when there is a max-age > 0
            max_age = value_as_int(cc, 'max-age')
            if max_age > 0:
                logger.debug('Caching b/c date exists and max-age > 0')
                self.cache.set(
                    cache_key,
                    self.serializer.dumps(request, response, body=body),
                )

            # If the request can expire, it means we should cache it
            # in the meantime.
            elif 'expires' in response_headers:
                if response_headers['expires']:
                    logger.debug('Caching b/c of expires header')
                    self.cache.set(
                        cache_key,
                        self.serializer.dumps(request, response, body=body),
                    )

    def update_cached_response(self, request, response):
        """On a 304 we will get a new set of headers that we want to
        update our cached value with, assuming we have one.

        This should only ever be called when we've sent an ETag and
        gotten a 304 as the response.
        """
        cache_key = self.cache_key(request)
        cache_data = self.cache.get(cache_key)
        cached_response = self.serializer.loads(
            request,
            cache_data
        )

        if not cached_response:
            # we didn't have a cached response
            return response

        # Lets update our headers with the headers from the new request:
        # http://tools.ietf.org/html/draft-ietf-httpbis-p4-conditional-26#section-4.1
        #
        # The server isn't supposed to send headers that would make
        # the cached body invalid. But... just in case, we'll be sure
        # to strip out ones we know that might be problmatic due to
        # typical assumptions.
        excluded_headers = [
            "content-length",
        ]

        cached_response.headers.update(
            dict((k, v) for k, v in response.headers.items()
                 if k.lower() not in excluded_headers)
        )

        # we want a 200 b/c we have content via the cache
        cached_response.status = 200

        # update our cache
        self.cache.set(
            cache_key,
            self.serializer.dumps(request, cached_response),
        )

        return cached_response

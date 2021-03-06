import json
import logging
import bottle
import functools

from collections import OrderedDict
from datetime import datetime
from urlparse import urlparse, urlunparse, parse_qsl
from urllib import urlencode

from paste import httpserver
from paste.translogger import TransLogger

log = logging.getLogger(__name__)

DEFAULT_FIRST_NAME = 'Friendly'
DEFAULT_LAST_NAME = 'Human'

class APILogger(TransLogger):
    def _trim_uri(self, req_uri):
        """ Hide the API key in the query string
        """
        parts = urlparse(req_uri)
        # Keep query args in original order
        query = OrderedDict(parse_qsl(parts.query))
        if 'key' in query:
            query['key'] = query['key'][-5:]
        query = urlencode(query)
        parts = parts._replace(query=query)
        trimmed_uri = urlunparse(parts)
        return trimmed_uri

    def write_log(
        self,
        environ,
        method,
        req_uri,
        start,
        status,
        bytes_,
        ):
        remote_addr = environ['REMOTE_ADDR']
        protocol = environ['SERVER_PROTOCOL']
        referer = environ.get('HTTP_REFERER', '-')
        user_agent = environ.get('HTTP_USER_AGENT', '-')
        trimmed_uri = self._trim_uri(req_uri)
        msg = ('{remote_addr} {method} {trimmed_uri} {protocol} {status} '
               '{bytes_} {referer} {user_agent}'
               ).format(
            remote_addr=remote_addr,
            method=method,
            trimmed_uri=trimmed_uri,
            protocol=protocol,
            status=status,
            bytes_=bytes_,
            referer=referer,
            user_agent=user_agent,
            )
        log.info(msg)

class APIServer(bottle.ServerAdapter):
    def run(self, handler):
        handler = APILogger(handler)
        httpserver.serve(
            handler,
            host=self.host,
            port=str(self.port),
            **self.options
            )

def set_content(type_, charset='charset=UTF-8'):
    bottle.response.content_type = '{type_}; {charset}'.format(
        type_=type_,
        charset=charset,
        )

def plain_content(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        set_content('text/plain')
        return fn(*args, **kwargs)
    return wrapper

def json_content(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        set_content('application/json')
        return fn(*args, **kwargs)
    return wrapper

def api_response(message, code=200):
    status = dict([
            ('message', message),
            ('code', code),
            ])
    status = dict([
            ('status', status),
            ])

    return json.dumps(status)

@bottle.error(400)
@bottle.error(403)
@bottle.error(404)
@bottle.error(500)
@json_content
def api_error(error):
    res = api_response(
        error.body,
        error.status_code,
    )
    return res

def key_context(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = bottle.request.query.key
        # request.query returns empty if attr doesn't exist
        if not key:
            raise bottle.HTTPError(
                status=403,
                body='You must specify an API key',
            )
        db_key = kwargs['self']._keys_coll.find_one(
            dict([
                ('_id', key),
                ]),
        )
        if (
                db_key is None or
                db_key['meta']['disabled'] or
                key != db_key['_id']
        ):
            raise bottle.HTTPError(
                status=403,
                body='Invalid API key',
            )
        if kwargs['self']._restrict_hosts:
            host = bottle.request.environ.get('REMOTE_ADDR')
            if host not in db_key['meta']['hosts']:
                log.error(
                    'Could not find host {host} for the specified '
                    'API key'.format(
                        host=host,
                    )
                )
                raise bottle.HTTPError(
                    status=403,
                    body='Invalid API key',
                )
        kwargs['request_key'] = key
        res = fn(*args, **kwargs)
        try:
            now = datetime.utcnow()
            kwargs['self']._keys_coll.update(
                dict([
                        ('meta.hosts', host),
                        ]),
                dict([
                        ('$set', dict([
                                    ('meta.last_used', now),
                                    ])
                         ),
                        ('$inc', dict([
                                    ('meta.times_used', 1),
                                    ])
                         ),
                        ]),
                )
        except Exception, e:
            log.error(
                'Could not update key for {host}: {e}'.format(
                    host=host,
                    e=str(e),
                )
            )
        finally:
            return res
    return wrapper

class EventAPI01(object):
    def __init__(self, colls, **kwargs):
        self._keys_coll = colls['gumjabi-keys']
        self._queue_coll = colls['kajabi-queue']
        self._restrict_hosts = kwargs.get('restrict_hosts', False)

    def apply(self, callback, context):
        """
        Similar to a bottle.JSONPlugin's apply
        method. This one also ensures that self
        is available to methods with bottle
        decorators.
        """
        @functools.wraps(callback)
        @json_content
        def wrapper(*args, **kwargs):
            kwargs['self'] = self
            return callback(*args, **kwargs)
        return wrapper

    def _log_gumroad_param_error(self, param, form):
        tmpl = (
            'Gumroad did not provide {param} but it did '
            'provide: {form}.'
        )
        msg = tmpl.format(
            param=param,
            form=json.dumps(dict(form)),
        )
        log.error(msg)

    @bottle.post('/gumroad/ping')
    @bottle.post('/gunroad/ping/')
    @key_context
    @json_content
    def gumroad_ping(self, **kwargs):
        form = bottle.request.forms
        email = form.pop('email', None)
        first_name = form.pop('First Name', None)
        last_name = form.pop('Last Name', None)
        test = form.pop('test', None)
        gumroad_link = form.pop('permalink', None)
        gumjabi_key = kwargs['request_key']
        dbkey = self._keys_coll.find_one({'_id': gumjabi_key})
        dblink = dbkey['gumroad_links'].get(gumroad_link)

        # None and empty are both bad
        if not gumroad_link:
            self._log_gumroad_param_error(
                param='a permalink',
                form=form,
            )
            raise bottle.HTTPError(
                status=400,
                body='Parameter missing: permalink',
            )
        if not dblink:
            msg = (
                'Could not find Gumroad link '
                '{gumroad_link}'.format(
                    gumroad_link=gumroad_link,
                )
            )
            log.error(msg)
            body = 'Invalid permalink: {gumroad_link}'.format(
                    gumroad_link=gumroad_link,
            )
            raise bottle.HTTPError(
                status=400,
                body=body,
            )
        if not email:
            self._log_gumroad_param_error(
                param='an email',
                form=form,
            )
            raise bottle.HTTPError(
                status=400,
                body='Parameter missing: email',
            )
        if not first_name:
            first_name = DEFAULT_FIRST_NAME
        if not last_name:
            last_name = DEFAULT_LAST_NAME

        # Simpler than loading JSON for just this variable
        if test == 'true':
            log.debug('Test request successful')
            return api_response('Test successful')
        log.debug(
            'Queueing email {email} and Gumroad link '
            '{gumroad_link} for Kajabi account creation'.format(
                email=email,
                gumroad_link=gumroad_link,
            )
        )
        item = dict([
            ('gumjabi_key', gumjabi_key),
            ('email', email),
            ('first_name', first_name),
            ('last_name', last_name),
            ('gumroad_link', gumroad_link),
            ('meta', dict([
                ('requested_on', datetime.utcnow()),
            ]),
         ),
        ])
        item.update(**form)
        self._queue_coll.insert(item)
        return api_response('Success')

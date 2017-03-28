# -*- coding: utf-8 -*-
# (c) 2016-2017 Andreas Motl <andreas.motl@elmyra.de>
import re
import json
import types
import mimetypes
import pymongo
from six import BytesIO
from copy import deepcopy
from urlparse import urlparse
from bunch import bunchify, Bunch
from collections import OrderedDict
from twisted.application.service import Service
from twisted.internet import reactor, threads
from twisted.logger import Logger
from twisted.web import http, server
from twisted.web.http import parse_qs
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.web.error import Error
from twisted.python.compat import nativeString
from kotori.io.router.path import PathRoutingEngine
from kotori.io.export.tabular import UniversalTabularExporter
from kotori.io.export.plot import UniversalPlotter
from kotori.io.protocol.util import convert_floats, slugify_datettime, flatten_request_args, handleFailure
from kotori.errors import last_error_and_traceback

log = Logger()

class LocalSite(Site):

    def log(self, request):
        """
        Redirect logging of HTTPFactory.

        @param request: The request object about which to log.
        @type request: L{Request}
        """
        line = u'HTTP access: ' + self._logFormatter(self._logDateTime, request)
        if self._nativeize:
            line = nativeString(line)
        else:
            line = line.encode("utf-8")
        log.debug(line)

class HttpServerService(Service):
    """
    Singleton instance of a Twisted service wrapping
    the Twisted TCP/HTTP server object "Site", in turn
    obtaining a ``HttpChannelContainer`` as root resource.
    """

    _instance = None

    def __init__(self, settings):

        # Propagate global settings
        self.settings = settings

        # Unique name of this service
        self.name = 'http-server-default'

        # Root resource object representing a channel
        # Contains routing machinery
        self.root = HttpChannelContainer()

        # Forward route registration method to channel object
        self.registerEndpoint = self.root.registerEndpoint

    def startService(self):
        """
        Start TCP listener on designated HTTP port,
        serving ``HttpChannelContainer`` as root resource.
        """

        # Don't start service twice
        if self.running == 1:
            return

        self.running = 1

        # Prepare startup
        http_listen = self.settings.kotori.http_listen
        http_port   = int(self.settings.kotori.http_port)
        log.info('Starting HTTP service on {http_listen}:{http_port}', http_listen=http_listen, http_port=http_port)

        # Configure root Site object and start listening to requests.
        # This must take place only once - can't bind to the same port multiple times!
        factory = LocalSite(self.root)
        reactor.listenTCP(http_port, factory, interface=http_listen)

    @classmethod
    def create(cls, settings):
        """
        Singleton factory
        """
        if not cls._instance:
            cls._instance = HttpServerService(settings)
            cls._instance.startService()
        return cls._instance


class HttpChannelContainer(Resource):
    """
    Twisted Site HTTP root resource driven by a
    routing engine based on the Pyramid request router.
    """

    def __init__(self):
        Resource.__init__(self)

        log.info('HttpChannelContainer init')

        self.database_connect()

        self.router    = PathRoutingEngine()
        self.callbacks = {}

    def database_connect(self):
        """
        Connect to Metadata storage
        """
        log.info('Connecting to Metadata storage database (MongoDB)')
        mongodb_uri = "mongodb://localhost:27017"

        # TODO: Make MongoDB address configurable
        self.metastore = pymongo.MongoClient(host='localhost', port=27017, socketTimeoutMS=5000, connectTimeoutMS=5000)

    def registerEndpoint(self, methods=None, path=None, callback=None):
        """
        Register path/callback with routing engine.
        """
        methods = methods or []
        log.info("Registering endpoint at path '{path}' for methods {methods}", path=path, methods=methods)
        if not callable(callback):
            log.error('Reference to endpoint {path} specified via "callback" '
                      'argument is not callable: {callback}', path=path, callback=callback)
            return

        # TODO: Add sanity checks for protecting against collisions on "name" and "path"
        name = path
        self.router.add_route(name, path, methods=methods)
        self.callbacks[name] = callback

    def getChild(self, name, request):
        """
        Twisted Resource path traversal method using
        the Pyramid request router for matching
        the request to registered endpoints.

        Returns ``HttpChannelEndpoint`` instance on match.
        """

        #log.info('getChild: {name}', name=name)
        uri = urlparse(str(request.URLPath()))

        # router v1
        """
        for endpoint in self.endpoints:
            if uri.path.startswith(endpoint.path):
                return HttpChannelEndpoint(options=endpoint)
        """

        # router v2
        #print 'Matching method=', request.method, 'uri=', uri.path
        result = self.router.match(request.method, uri.path)
        if result:
            #print 'route match result:'; pprint(result); pprint(result['route'].__dict__)

            # Obtain matched route name
            route_name = result['route'].name

            # Obtain appropriate callback function
            # TODO: Beware of collisions on "route_name", see above
            callback = self.callbacks[route_name]

            # Wrap endpoint description into container object
            endpoint = bunchify({'path': route_name, 'callback': callback, 'match': result['match'], 'request': request})
            #print 'endpoint:'; pprint(endpoint)

            # Create leaf resource instance
            return HttpChannelEndpoint(options=endpoint, metastore=self.metastore)

        # If nothing matched, continue traversal
        return self


class HttpChannelEndpoint(Resource):
    """
    Upper layer of data forwarding workhorse for HTTP.

    Twisted Site HTTP leaf resource containing the main dispatcher
    logic for forwarding inbound requests to the routing target.
    """

    isLeaf = True

    def __init__(self, options, metastore=None):
        self.options = options
        self.metastore = metastore
        Resource.__init__(self)

    def render(self, request):
        """
        Main Twisted Resource rendering method,
        overridden to provide custom logic.
        """

        # Pluck ``error_response`` method to request object
        request.error_response = self.error_response

        # Pluck ``channel_identifier`` attribute to request object
        request.channel_identifier = re.sub('/data.*$', '', request.path.replace('/api', ''))

        # Pluck response messages object to request object
        request.messages = []

        # Add informational headers
        request.setHeader('Channel-Id', request.channel_identifier)

        # Main bucket data container object serving the whole downstream processing chain
        bucket = Bunch(path=request.path, request=request)

        # Request processing chain, worker-threaded
        deferred = threads.deferToThread(self.dispatch, request, bucket)
        deferred.addErrback(handleFailure, request)
        deferred.addBoth(self.render_messages, request)
        deferred.addBoth(request.write)
        deferred.addBoth(lambda _: request.finish())
        return server.NOT_DONE_YET

    def dispatch(self, request, bucket):
        """
        Dispatch request
        """

        # Read and decode/parse ingress data
        data = self.read_request(bucket)

        # Process ingress data
        response = self.process_data(data, bucket)

        # Send response
        return response

    def render_messages(self, passthrough, request):
        if request.messages:
            request.setHeader('Content-Type', 'application/json')
            return json.dumps(request.messages, indent=4)
        else:
            #request.setHeader('Content-Type', 'text/plain; charset=utf-8')
            return passthrough

    def read_request(self, bucket):
        """
        Forward inbound requests to the routing target by performing these steps:

        - Build the transformation data container ``tdata``
          by feeding it information from the HTTP request:

            - Obtain HTTP request
            - Decode data from request body
            - Merge data from request arguments
            - Merge data from url matches

        - Build the main data container object ``bucket``
          serving the whole downstream processing chain.

        - Call designated registered callback method with ``bucket``.

        """

        request = bucket.request

        content_type = request.getHeader('Content-Type')
        log.debug('Received HTTP request on uri {uri}, '
                  'content type is "{content_type}"', uri=request.path, content_type=content_type)

        # Data acquisition uses HTTP POST
        if request.method == 'POST':
            return self.data_acquisition(bucket)

    def data_acquisition(self, bucket):

        request = bucket.request

        content_type = request.getHeader('Content-Type')

        # Read and decode request body
        body = request.content.read()
        bucket.body = body

        # Decode data from request body
        if body:

            # Decode data from JSON format
            if content_type.startswith('application/json'):
                return json.loads(body)

            # Decode data from x-www-form-urlencoded format
            elif content_type.startswith('application/x-www-form-urlencoded'):
                # TODO: Honor charset when receiving "application/x-www-form-urlencoded; charset=utf-8"
                payload = parse_qs(body, 1)
                # TODO: Decapsulate multiple values of same reading into "{name}-{N}", where N=1...
                for key, value in payload.iteritems():
                    if type(value) is types.ListType:
                        payload[key] = value[0]
                return payload

            # Decode data from CSV format
            elif content_type.startswith('text/csv'):

                if not self.metastore:
                    log.error('Generic decoding of CSV format requires metastore')

                # Prepare alias for metastore table
                csv_header_store = self.metastore.kotori['channel-csv-headers']
                #print 'csv_header_store:', csv_header_store

                # 1. Decode CSV header like '## weight,temperature, humidity' and remember for upcoming data readings
                def parse_header(channel_info, data_lines):

                    first_line = data_lines[0]
                    header_line = None
                    options = {}

                    # Regular header announcement
                    if first_line.startswith('## '):
                        header_line = first_line[3:].strip()
                        data_lines.pop(0)

                    # Convenience hack to support Open Hive CSV import
                    elif first_line.startswith('Date/Time') or first_line.startswith('Datum/Zeit'):
                        header_line = first_line
                        data_lines.pop(0)

                    # Convenience hack to support Beelogger CSV import
                    elif first_line.startswith('Datum,Uhrzeit'):
                        header_line = first_line
                        data_lines.pop(0)
                        options['rules'] = [{'type': 'fuse', 'source': ['Datum', 'Uhrzeit'], 'target': 'time', 'join': 'T', 'suffix': 'Z'}]

                    # Convenience hack to support import from http://archive.luftdaten.info/
                    elif first_line.startswith('sensor_id'):
                        header_line = first_line
                        data_lines.pop(0)

                    if header_line:
                        # Streamline various differences for even more convenience
                        header_line = header_line.replace(';', ',').replace('Date/Time', 'time').replace('Datum/Zeit', 'time').replace('timestamp', 'time')
                        header_fields = map(str.strip, header_line.split(','))
                        msg = u'CSV Header: fields={fields}, key={key}'.format(fields=header_fields, key=request.channel_identifier)
                        log.info(msg)

                        csv_header_store.update_one(
                            {"channel": request.channel_identifier},
                            {"$set": {"header_fields": header_fields, "options": options}}, upsert=True)

                        message = u'Received header fields {}'.format(header_fields)
                        request.messages.append({'type': 'info', 'message': message})

                        channel_info['header_fields'] = header_fields
                        channel_info['options'] = options

                    #print 'header_fields, data_lines:', header_fields, data_lines
                    #return header_fields, data_lines

                # 2. Decode data, map to full-qualified payload container
                def parse_data(channel_info):
                    channel_info = channel_info or {}
                    data_raw = body.strip()
                    data_lines = map(str.strip, data_raw.split('\n'))
                    parse_header(channel_info, data_lines)
                    header_fields = channel_info.get('header_fields')
                    if not header_fields:
                        raise Error(http.BAD_REQUEST,
                            response='Could not process data, please supply field names via CSV header before sending readings')

                    #print 'data_lines:', data_lines; pprint(data_lines)

                    data_list = []
                    for data_line in data_lines:
                        data_fields = map(str.strip, data_line.replace(';', ',').split(','))
                        #print 'header_fields, data_fields:', header_fields, data_fields
                        data = OrderedDict(zip(header_fields, data_fields))
                        self.manipulate_data(data, channel_info)
                        data_list.append(data)

                    return data_list

                try:
                    channel_info = csv_header_store.find_one(filter={"channel": request.channel_identifier})
                except Exception as ex:
                    log.failure('Could not process CSV data, unknown database error: {0}'.format(ex))
                    raise Error(http.INTERNAL_SERVER_ERROR,
                        response='Could not process CSV data, unknown database error: {0}'.format(ex))

                return parse_data(channel_info)

            else:
                msg = u"Unable to handle Content-Type '{content_type}'".format(content_type=content_type)
                log.warn(msg)
                raise Error(http.UNSUPPORTED_MEDIA_TYPE, response=msg)

        else:
            msg = u'Empty request body'
            log.warn(msg)
            raise Error(http.BAD_REQUEST, response=msg)

    def manipulate_data(self, data, channel_info):
        """
        Data fusion on CSV data lines.
        Convenience hack to support Beelogger CSV import.
        """
        if 'options' in channel_info:
            rules = channel_info['options'].get('rules', [])
            for rule in rules:
                if rule['type'] == 'fuse':
                    extracted = []
                    for source_field in rule['source']:
                        if source_field in data:
                            extracted.append(data.get(source_field, ''))
                            del data[source_field]

                    separator = rule.get('join', '')
                    fused = separator.join(extracted)
                    fused += rule.get('suffix', '')

                    target = rule['target']
                    data[target] = fused


    def process_data(self, data, bucket):

        # Main transformation data container
        bucket.tdata = Bunch()

        # Merge request parameters (GET and POST) and url matches, in this order
        bucket.tdata.update(flatten_request_args(bucket.request.args))
        bucket.tdata.update(self.options.match)

        if data is None:

            # Run forwarding callback
            return self.options.callback(bucket)


        if type(data) is not types.ListType:
            data = [data]

        for item in data:
            # TODO: Apply this to telemetry values only!
            # FIXME: This is a hack
            if not bucket.request.path.endswith('event') and not bucket.request.path.endswith('firmware'):
                convert_floats(item, integers=['time'])
            self.propagate_single(item, bucket)

        message = 'Received #{number} readings'.format(number=len(data))
        bucket.request.messages.append({'type': 'info', 'message': message})


    def propagate_single(self, item, bucket):

        # Serialize as json for convenience
        # TODO: Really?
        item_json = json.dumps(item)

        # Update main bucket container
        bucket.data = item
        bucket.json = item_json

        # Run forwarding callback
        return self.options.callback(bucket)

    @staticmethod
    def error_response(bucket, error_message='', code=http.BAD_REQUEST, with_traceback=False):
        """
        Error handling method logging and returning appropriate stacktrace.
        """
        # FIXME: Check for privacy. Do something more sane with the stacktrace
        #        or enable only when sending appropriate request arguments.
        if with_traceback:
            error_message += '\n' + last_error_and_traceback()
            log.error(error_message)
        bucket.request.setResponseCode(code)
        #bucket.request.setHeader('Content-Type', 'text/plain; charset=utf-8')
        return error_message.encode('utf-8')


class HttpDataFrameResponse(object):
    """
    Bottom layer of data forwarding workhorse for HTTP.

    Generate appropriate output content based on
    information in transformation data ``bucket.tdata``.

    Render pandas DataFrame to various tabular and hierarchical
    data formats and different timeseries plots.

    Tabular data:

        - CSV
        - JSON
        - HTML
        - Excel (XLSX)
        - DataTables HTML widget

    Hierarchical data:

        - HDF5
        - NetCDF

    Timeseries plots:

        - [PNG]  matplotlib
        - [PNG]  ggplot
        - [HTML] dygraphs
        - [HTML] Bokeh
        - [HTML] Vega/Vincent

    """

    def __init__(self, bucket, dataframe):
        self.bucket = bucket
        self.request = bucket.request
        self.dataframe = dataframe

    def render(self):
        """
        Evaluate ``bucket`` information and enrich further before
        executing the designated output format rendering handler.
        """

        # Variable aliases
        bucket = self.bucket
        df = self.dataframe

        # Read designated suffix from transformation data
        suffix = bucket.tdata.suffix.lower()

        # Update "time_begin" and "time_end" fields to be in ISO 8601 format
        tdata = deepcopy(bucket.tdata)
        tdata.update({
            'time_begin': slugify_datettime(bucket.tdata.time_begin),
            'time_end':   slugify_datettime(bucket.tdata.time_end),
        })

        # Compute some names and titles and pluck into ``bucket``
        bucket.title = Bunch(
            compact = u'{gateway}_{node}'.format(**dict(tdata)).replace('-', '_'),
            short = u'{network}_{gateway}_{node}'.format(**dict(tdata)).replace('-', '_'),
            full  = u'{network}_{gateway}_{node}_{time_begin}-{time_end}'.format(**dict(tdata)).replace('-', '_'),
            human = u'Address: {network} » {gateway} » {node}'.format(**dict(tdata)),
        )


        # Buffer object most output handlers write their content to
        buffer = BytesIO()


        # Dispatch to appropriate output handler
        # TODO: XML, SQL, GBQ (Google BigQuery table), MsgPack?, Thrift?
        # TODO: jsonline using Odo, see http://odo.pydata.org/en/latest/json.html
        # TODO: Refactor "if response: return response" cruft
        # TODO: Refactor dispatching logic to improve suffix comparison redundancy with UniversalTabularExporter

        if suffix in ['csv', 'txt']:
            # http://pandas.pydata.org/pandas-docs/stable/io.html#io-store-in-csv
            df.to_csv(buffer, header=True, index=False, date_format='%Y-%m-%dT%H:%M:%S.%fZ')

        elif suffix == 'tsv':
            df.to_csv(buffer, header=True, index=False, date_format='%Y-%m-%dT%H:%M:%S.%fZ', sep='\t')

        elif suffix == 'json':
            # http://pandas.pydata.org/pandas-docs/stable/io.html#io-json-writer
            df.to_json(buffer, orient='records', date_format='iso')

        elif suffix == 'html':
            # http://pandas.pydata.org/pandas-docs/stable/io.html#io-html
            df.to_html(buffer, index=False, justify='center')

        elif suffix == 'xlsx':
            exporter = UniversalTabularExporter(bucket, dataframe=df)
            response = exporter.render(suffix, buffer=buffer)
            if response:
                return response

        elif suffix in ['hdf', 'hdf5', 'h5']:
            exporter = UniversalTabularExporter(bucket, dataframe=df)
            response = exporter.render(suffix, buffer=buffer)
            if response:
                return response

        elif suffix in ['nc', 'cdf']:
            exporter = UniversalTabularExporter(bucket, dataframe=df)
            response = exporter.render(suffix, buffer=buffer)
            if response:
                return response

        elif suffix in ['dy', 'dygraphs']:
            plotter = UniversalPlotter(bucket, dataframe=df)
            response = plotter.render('html', kind='dygraphs')
            if response:
                return response

        elif suffix in ['dt', 'datatables']:
            exporter = UniversalTabularExporter(bucket, dataframe=df)
            response = exporter.render(suffix, buffer=buffer)
            if response:
                return response

        elif suffix in ['bk', 'bokeh']:
            plotter = UniversalPlotter(bucket, dataframe=df)
            response = plotter.render('html', kind='bokeh')
            if response:
                return response

        elif suffix == 'vega.json':
            plotter = UniversalPlotter(bucket, dataframe=df)
            response = plotter.render('json', kind='vega')
            if response:
                return response

        elif suffix == 'vega':
            plotter = UniversalPlotter(bucket, dataframe=df)
            response = plotter.render('html', kind='vega')
            if response:
                return response

        elif suffix in ['png']:
            plotter = UniversalPlotter(bucket, dataframe=df)
            response = plotter.render('png', buffer=buffer)
            if response:
                return response

        else:
            error_message = u'# Unknown data format "{suffix}"'.format(suffix=suffix)
            bucket.request.setResponseCode(http.BAD_REQUEST)
            bucket.request.setHeader('Content-Type', 'text/plain; charset=utf-8')
            return error_message.encode('utf-8')


        # Get hold of buffer content
        payload = buffer.getvalue()


        # Compute filename offered to browser
        filename = '{name}.{suffix}'.format(name=bucket.title.full, suffix=suffix)
        mimetype, encoding = mimetypes.guess_type(filename, strict=False)
        log.info(u'Fetching data succeeded, filename: {filename}, Format: {mimetype}', filename=filename, mimetype=mimetype)

        # Set "Content-Type" header
        if mimetype:
            bucket.request.setHeader('Content-Type', mimetype)

        # Set "Content-Disposition" header
        disposition = 'attachment'
        if mimetype in ['text/plain', 'text/csv', 'text/html', 'application/json', 'image/png']:
            disposition = 'inline'
        bucket.request.setHeader('Content-Disposition', '{disposition}; filename={filename}'.format(
            disposition=disposition, filename=filename))

        # Optionally encode to UTF-8 when serving HTML
        if mimetype == 'text/html':
            payload = payload.encode('utf-8')

        return payload


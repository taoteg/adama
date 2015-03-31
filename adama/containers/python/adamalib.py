import requests


REGISTER_TIMEOUT = 30  # seconds


class Adama(object):

    def __init__(self, token, url=None):
        """
        :type token: str
        :type url: str
        :rtype: None
        """
        self.token = token
        self.url = url

    @property
    def utils(self):
        return Utils(self)

    def error(self, message, obj=None):
        """
        :type message: str
        :type obj: object
        :rtype: None
        """

    def get(self, url, **kwargs):
        """
        :type url: str
        :type kwargs: dict[str, object]
        :rtype: requests.Response
        """

    def get_json(self, url, **kwargs):
        """
        :type url: str
        :type kwargs: dict[str, object]
        :rtype: dict
        """

    def post(self, url, **kwargs):
        """
        :type url: str
        :type kwargs: dict
        :rtype: requests.Response
        """

    def delete(self, url):
        """
        :type url: str
        :rtype: None
        """

    @property
    def status(self):
        return self.get_json('/status')

    @property
    def namespaces(self):
        nss = self.get_json('/namespaces')['result']
        return Namespaces(self, [Namespace(self, ns['name']) for ns in nss])

    def __getattr__(self, item):
        """
        :type item: str
        :rtype: Namespace
        """
        return Namespace(self, item)


class APIException(Exception):

    def __init__(self, msg, obj=None):
        super(APIException, self).__init__(msg)
        self.obj = obj


class Namespaces(list):

    def __init__(self, adama, *args, **kwargs):
        super(Namespaces, self).__init__(*args, **kwargs)
        self.adama = adama

    def add(self, **kwargs):
        """
        :type kwargs: dict
        :rtype: Namespace
        """


class Namespace(object):

    def __init__(self, adama, namespace):
        """
        :type adama: Adama
        :type namespace: str
        :rtype: None
        """
        self.adama = adama
        self.namespace = namespace

    def __repr__(self):
        return 'Namespace({})'.format(self.namespace)

    @property
    def services(self):
        """
        :rtype: Services
        """
        srvs = self.adama.get_json(
            '/{}/services'.format(self.namespace))['result']
        return Services(self.adama, self.namespace,
                        [Service(self, srv['name']) for srv in srvs])

    def __getattr__(self, item):
        """
        :type item: str
        :rtype: Service
        """
        return Service(self, item)


class Services(list):

    def __init__(self, adama, namespace, *args, **kwargs):
        """
        :type adama: Adama
        :type namespace: str
        :type args: list
        :type kwargs: dict
        :rtype: None
        """
        super(Services, self).__init__(*args, **kwargs)
        self.adama = adama
        self.namespace = namespace

    def add(self, mod, async=False, timeout=REGISTER_TIMEOUT):
        """
        :type mod: module
        :rtype: Service
        """


class Service(object):

    def __init__(self, namespace, service):
        """
        :type namespace: Namespace
        :type service: str
        :rtype: None
        """
        self._namespace = namespace
        self.service = service
        self._version = '0.1'

    @property
    def _full_name(self):
        return '/{}/{}_v{}'.format(
            self._namespace.namespace, self.service, self._version)

    def __repr__(self):
        return 'Service({})'.format(self._full_name)

    def __getitem__(self, item):
        self._version = item
        return self

    def __getattr__(self, item):
        """
        :type item: str
        :rtype:
        """
        return Endpoint(self, item)

    def delete(self):
        pass


# noinspection PyProtectedMember
class Endpoint(object):

    def __init__(self, service, endpoint):
        """
        :type service: Service
        :type endpoint: str
        :rtype: None
        """
        self.service = service
        self.endpoint = endpoint
        self.namespace = self.service._namespace
        self.adama = self.service._namespace.adama

    def __call__(self, **kwargs):
        """
        :type kwargs: dict
        :rtype: object
        """


class Utils(object):

    def __init__(self, adama):
        """
        :type adama: Adama
        :rtype: None
        """
        self.adama = adama

    def request(self, url, **kwargs):
        """
        :type url: str
        :type kwargs: dict[str, object]
        :rtype: requests.Response
        """
        resp = requests.get(url, params=kwargs)
        if not resp.ok:
            self.adama.error(resp.text, resp)
        return resp
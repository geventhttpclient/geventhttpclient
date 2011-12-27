from geventhttpclient import httplib
httplib.patch()

from urllib2 import urlopen
from gauss.common.debug import debugger


# equivalent to a gevent monkey patch and:
# httplib.HTTPConnection.response_class = httplib.HTTPResponse


with debugger():
    print urlopen('https://www.google.fr/').read()



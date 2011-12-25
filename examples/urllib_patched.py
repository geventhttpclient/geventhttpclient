from geventhttpclient import httplibcompat
httplibcompat.patch()

from urllib2 import urlopen
from gauss.common.debug import debugger


# equivalent to a gevent monkey patch and:
# httplib.HTTPConnection.response_class = httplibcompat.HTTPResponse


with debugger():
    print urlopen('https://www.google.fr/').read()



import gevent.monkey

gevent.monkey.patch_all()

from geventhttpclient import httplib

httplib.patch()

from urllib.request import urlopen

print(urlopen("http://gevent.org").read()[:1000])
print(urlopen("https://google.com").read()[:1000])

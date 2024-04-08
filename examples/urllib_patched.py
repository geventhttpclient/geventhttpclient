from geventhttpclient import httplib

httplib.patch()

from urllib2 import urlopen


print(urlopen("https://www.google.fr/").read())

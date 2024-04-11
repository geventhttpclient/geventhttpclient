import gevent.monkey

gevent.monkey.patch_all()

from geventhttpclient import httplib

httplib.patch()

from geventhttpclient import httplib2

http = httplib2.Http()
response, content = http.request("http://github.com")
assert response.status == 200
assert content
print(response)
print(content[:1000])

response, content = http.request("https://github.com/", method="HEAD")
assert response.status == 200
assert not content
print(response)

response, content = http.request("https://google.com/", method="HEAD")
assert response.status == 200
assert not content
print(response)

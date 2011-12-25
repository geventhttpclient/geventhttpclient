from geventhttpclient import httplibcompat
httplibcompat.patch()

from httplib2 import Http
from gauss.common.debug import debugger


with debugger():
    http = Http()
    response, content = http.request('http://google.fr/')
    assert response.status == 200
    assert content
    print response
    print content

    response, content = http.request('http://google.fr/', method='HEAD')
    assert response.status == 200
    assert content == ''
    print response

    response, content = http.request('https://www.google.com/', method='HEAD')
    assert response.status == 200
    assert content == ''
    print response



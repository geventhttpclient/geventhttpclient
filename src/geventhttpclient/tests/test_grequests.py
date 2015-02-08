import gevent.monkey
gevent.monkey.patch_all()

from geventhttpclient import grequests

def test_connect():
    s = grequests.Session()
    r = s.get('http://heise.de')
    print r.status_code
    print r.text

if __name__ == '__main__':
    test_connect()
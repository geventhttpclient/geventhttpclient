'''
Created on 05.11.2012

@author: nimrod
'''
import gevent
import gevent.monkey
gevent.monkey.patch_all()

import os

from geventhttpclient.useragent import UserAgent, CompatResponse, CompatRequest, \
        RetriesExceeded, BadStatusCode, ConnectionError


def test_open_multiple_domains():
    ua = UserAgent(max_retries=1)
    for domain in ('google.com', 'facebook.com'):
        try:
            r = ua.urlopen('http://' + domain + '/')
        except RetriesExceeded:
            print "Redirect failed"
        else:
            print r.headers

def test_open_multiple_domains_parallel():
    ua = UserAgent(max_retries=1)
    domains = 'google.com', 'facebook.com', 'microsoft.com', 'spiegel.de', 'heise.de'
    get_domain_headers = lambda d: (d, ua.urlopen('http://' + d).headers)
    gp = gevent.pool.Group()
    for domain, hdr in gp.imap_unordered(get_domain_headers, domains):
        print domain
        print hdr 
        print

def test_download():
    url = 'http://de.archive.ubuntu.com/ubuntu/pool/universe/v/vlc/vlc_2.0.4-0ubuntu1_i386.deb'
    fpath = '/tmp/_test_download'
    ua = UserAgent(max_retries=3)
    try:
        r = ua.download(url, fpath)
    except RetriesExceeded:
        print "Redirect failed"
    except ConnectionError as e:
        print "Not found: %s %s" % (type(e).__name__, e)
    else:
        fl = os.path.getsize(fpath)
        cl = r.headers.get('Content-Length')
        cl = int(cl) if cl else None
        assert cl == fl
        len_str = 'OK' if cl == fl else 'CL: %s / FL: %s' % (cl, fl)
        print "Download finished:", len_str
    os.remove(fpath)
        
if __name__ == '__main__':
    test_download()
#    test_open_multiple_domains_parallel()

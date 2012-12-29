'''
Created on 05.11.2012

@author: nimrod
'''
import gevent
import gevent.monkey
gevent.monkey.patch_all()

import os
import sys
import filecmp

from geventhttpclient.useragent import UserAgent, CompatResponse, CompatRequest, \
        RetriesExceeded, BadStatusCode, ConnectionError


USER_AGENT = 'Mozilla/5.0 (X11; U; Linux i686; de; rv:1.9.2.17) Gecko/20110422 Ubuntu/10.04 (lucid) Firefox/3.6.17'
DEFAULT_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 
    'Accept-Encoding': 'gzip,deflate',
    'Connection': 'keep-alive'}


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
    ua = UserAgent(max_retries=1, headers=DEFAULT_HEADERS)
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

def test_download_parts():
    url = 'http://de.archive.ubuntu.com/ubuntu/pool/universe/v/vlc/vlc_2.0.4-0ubuntu1_i386.deb'
    fpath = '/tmp/_test_download'
    fpath_part = '/tmp/_test_download_part'
    part_size = 400000
    ua = UserAgent(max_retries=3)
    ua.download(url, fpath)
    assert os.path.getsize(fpath) > part_size
    with open(fpath_part, 'w') as chunk:
        chunk.write(open(fpath).read(part_size))
        chunk.flush()
    assert part_size == os.path.getsize(fpath_part)
    
    try:
        r = ua.download(url, fpath_part, resume=True)
    except RetriesExceeded:
        print "Redirect failed"
    except ConnectionError as e:
        print "Not found: %s %s" % (type(e).__name__, e)
    else:
        assert len(r) + part_size == os.path.getsize(fpath)
        assert os.path.getsize(fpath) == os.path.getsize(fpath_part)
        assert filecmp.cmp(fpath, fpath_part)
    os.remove(fpath)
    os.remove(fpath_part)

def test_gzip():
    ua = UserAgent(max_retries=1, headers=DEFAULT_HEADERS)
    resp = ua.urlopen('https://google.com')
    assert resp.headers.get('content-encoding') == 'gzip'
    cl = int(resp.headers.get('content-length', 0))
    if cl:
        # Looks like google dropped content-length recently
        assert cl > 5000
        assert len(resp.content) >  2 * cl
    # Check, if unzip produced readable output
    for word in ('doctype', 'html', 'function', 'script', 'google'):
        assert word in resp.content


if __name__ == '__main__':
#    test_gzip()
#    test_download()
    test_download_parts()

#    test_open_multiple_domains_parallel()

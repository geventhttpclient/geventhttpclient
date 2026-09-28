#!/usr/bin/env python

import json

import gevent.pool

from geventhttpclient import HTTPClient
from geventhttpclient.url import URL

# go to http://developers.facebook.com/tools/explorer and copy the access token
TOKEN = "<MY_DEV_TOKEN>"

url = URL("https://graph.facebook.com/me/friends", params={"access_token": TOKEN})

# setting the concurrency to 10 allow to create 10 connections and
# reuse them.
client = HTTPClient.from_url(url, concurrency=10)

response = client.get(url.request_uri)
assert response.status_code == 200

# response comply to the read protocol. It passes the stream to
# the json parser as it's being read.
data = json.load(response)["data"]


def print_friend_username(client, friend_id):
    friend_url = URL("/" + str(friend_id), params={"access_token": TOKEN})
    # the greenlet will block until a connection is available
    response = client.get(friend_url.request_uri)
    assert response.status_code == 200
    friend = json.load(response)
    if "username" in friend:
        print(f"{friend['username']}: {friend['name']}")
    else:
        print(f"{friend['name']} has no username.")


# allow to run 20 greenlet at a time, this is more than concurrency
# of the http client but isn't a problem since the client has its own
# connection pool.


pool = gevent.pool.Pool(20)
for item in data:
    friend_id = item["id"]
    pool.spawn(print_friend_username, client, friend_id)

pool.join()
client.close()

import json
import time
from pprint import pprint as pp

import oauth2 as oauthlib

from geventhttpclient import HTTPClient
from geventhttpclient.url import URL

APP_ID = "<YOUR_APP_ID>"
APP_SECRET = "<YOUR_APP_SECRET>"

# see https://github.com/simplegeo/python-oauth2
# "Twitter Three-legged OAuth Example"
token_info = {
    "oauth_token_secret": "...",
    "user_id": "...",
    "oauth_token": "...",
    "screen_name": "...",
}

oauthlib_consumer = oauthlib.Consumer(APP_ID, APP_SECRET)
token = oauthlib.Token(token_info["oauth_token"], token_info["oauth_token_secret"])

params = {
    "oauth_version": "1.0",
    "oauth_nonce": oauthlib.generate_nonce(),
    "oauth_timestamp": int(time.time()),
    "oauth_token": token.key,
    "oauth_consumer_key": oauthlib_consumer.key,
    "locations": "-122.75,36.8,-121.75,37.8",  # San Francisco
}

url = URL("https://stream.twitter.com/1/statuses/filter.json")
req = oauthlib.Request.from_consumer_and_token(
    oauthlib_consumer, token=token, http_url=str(url), http_method="POST"
)

signature_method = oauthlib.SignatureMethod_HMAC_SHA1()
req = oauthlib.Request(method="POST", url=str(url), parameters=params)
req.sign_request(signature_method, oauthlib_consumer, token)

client = HTTPClient.from_url(url)
response = client.request(
    "POST",
    url.request_uri,
    body=req.to_postdata(),
    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
)

data = json.loads(response.readline())
while data:
    pp(data)
    data = json.loads(response.readline())

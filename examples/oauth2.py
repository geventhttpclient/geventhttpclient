#!/usr/bin/env python
"""Minimal OAuth 2.0 client credentials grant with geventhttpclient.

geventhttpclient needs no third-party OAuth library: the token endpoint is
a plain form-encoded POST and every API call carries the access token in
the Authorization header.

Replace the placeholder URLs and credentials with the ones of your OAuth
2.0 provider. The API endpoint in this example returns newline delimited
JSON, which is parsed line by line while the response streams in.
"""

import json
from urllib.parse import urlencode

from geventhttpclient import URL, HTTPClient

TOKEN_URL = URL("https://auth.example.com/oauth2/token")
API_URL = URL("https://api.example.com/v1/events", params={"lang": "en"})

CLIENT_ID = "<YOUR_CLIENT_ID>"
CLIENT_SECRET = "<YOUR_CLIENT_SECRET>"

# OAuth 2.0 client credentials grant (RFC 6749, section 4.4): exchange the
# client credentials for a short lived bearer access token.
token_client = HTTPClient.from_url(TOKEN_URL)
response = token_client.post(
    TOKEN_URL.request_uri,
    body=urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
    ),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
assert response.status_code == 200
access_token = json.load(response)["access_token"]

# Authenticated API call, bearer token usage (RFC 6750).
api_client = HTTPClient.from_url(API_URL)
with api_client.get(
    API_URL.request_uri, headers={"Authorization": f"Bearer {access_token}"}
) as response:
    assert response.status_code == 200
    # iterating a response yields block_size chunks, not lines, so read lines
    line = response.readline(b"\n")  # default separator ends HTTP headers
    while line:
        print(json.loads(line))
        line = response.readline(b"\n")

token_client.close()
api_client.close()

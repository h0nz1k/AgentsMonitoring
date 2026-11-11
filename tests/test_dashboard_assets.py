"""Transport compression and authenticated cache validation for dashboard resources."""
import gzip
from unittest.mock import patch

from test_hardening import Base
from agentsmon import dashboard


class Resources(Base):
    def test_gzip_and_etags(self):
        with patch.object(dashboard, '_cached_state', return_value=b'{"agents": []}'):
            for path in ('/api/state', '/static/dashboard.css', '/static/logos/openai.svg'):
                with self.subTest(path=path):
                    status, headers, body = self.req('GET', path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['cache-control'],
                                     'private, no-cache' if path == '/api/state' else 'public, no-cache')
                    self.assertEqual(headers['vary'], 'Accept-Encoding')
                    status, encoded_headers, encoded = self.req('GET', path, headers={'Accept-Encoding': 'gzip'})
                    self.assertEqual(status, 200)
                    self.assertEqual(gzip.decompress(encoded), body)
                    self.assertEqual(encoded_headers['content-encoding'], 'gzip')
                    self.assertEqual(encoded_headers['etag'], headers['etag'])
                    for tag in (headers['etag'], headers['etag'][2:], '*', '"old", '+headers['etag']):
                        status, cached_headers, cached = self.req('GET', path, headers={'If-None-Match': tag})
                        self.assertEqual(status, 304)
                        self.assertEqual(cached, b'')
                        self.assertEqual(cached_headers['etag'], headers['etag'])
                    self.assertEqual(self.req('GET', path, headers={'If-None-Match': '"old"'})[0], 200)

    def test_state_etag_changes_with_body(self):
        with patch.object(dashboard, '_cached_state', return_value=b'{}'):
            tag = self.req('GET', '/api/state')[1]['etag']
        with patch.object(dashboard, '_cached_state', return_value=b'{"agents": []}'):
            self.assertEqual(self.req('GET', '/api/state', headers={'If-None-Match': tag})[0], 200)

    def test_page_gzip_remains_no_store_and_encoding_opt_out(self):
        plain = self.req('GET', '/')[2]
        status, headers, body = self.req('GET', '/', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(gzip.decompress(body), plain)
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertNotIn('etag', headers)
        for encoding in ('gzip;q=0', 'gzip; q=0.0', 'br'):
            self.assertNotIn('content-encoding', self.req('GET', '/', headers={'Accept-Encoding': encoding})[1])

    def test_only_allowlisted_assets(self):
        for path in dashboard._STATIC_ASSETS:
            self.assertEqual(self.req('GET', path)[0], 200)
        for path in ('/static/logos/../../dashboard.py', '/static/logos/LICENSE.md', '/static/tailwind.js'):
            self.assertEqual(self.req('GET', path)[0], 404)
        self.assertEqual(self.req('GET', '/static/dashboard.css?v=1')[0], 200)

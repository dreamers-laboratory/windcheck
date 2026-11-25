"""Privacy-page reachability, disclosure and unlisted-route regression checks."""
import unittest

from bs4 import BeautifulSoup

import metar


class PrivacyPageTest(unittest.TestCase):
    def setUp(self):
        self.client = metar.app.test_client()

    def test_policy_is_public_and_canonical(self):
        response = self.client.get('/privacy/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith('text/html'))
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertEqual(page.title.string, 'Windcheck Privacy Policy')
        self.assertEqual(page.find('link', rel='canonical')['href'],
                         'https://windcheck.net/privacy/')
        self.assertEqual(self.client.get('/privacy').status_code, 301)
        self.assertTrue(self.client.get('/privacy').location.endswith('/privacy/'))

    def test_policy_is_unlisted_and_not_indexable(self):
        response = self.client.get('/privacy/')
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertIn('noindex', page.find('meta', attrs={'name': 'robots'})['content'])
        self.assertIn('noindex', response.headers['X-Robots-Tag'])
        for route in ['/', '/about', '/sitemap.xml', '/llms.txt']:
            with self.subTest(route=route):
                self.assertNotIn('/privacy', self.client.get(route).get_data(as_text=True))

    def test_policy_has_no_tracking_or_runtime_requests(self):
        response = self.client.get('/privacy/')
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertFalse(page.find_all('script'))
        self.assertFalse(page.find_all('iframe'))
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_material_disclosures_and_contact_are_present(self):
        page = BeautifulSoup(self.client.get('/privacy/').data, 'html.parser')
        text = page.get_text(' ', strip=True)
        for disclosure in ['Dreamers Inc.', 'IP addresses', 'Google reCAPTCHA',
                           'TestFlight', '14 archived log files', 'local preferences']:
            with self.subTest(disclosure=disclosure):
                self.assertIn(disclosure, text)
        self.assertIsNotNone(page.find('a', href='https://thedreamers.us/#contact'))
        self.assertNotIn('[OPERATOR', text)
        self.assertNotIn('[DATE]', text)

    def test_ios_update_provider_and_install_identifier_are_disclosed(self):
        page = BeautifulSoup(self.client.get('/privacy/').data, 'html.parser')
        link = page.select_one('section[aria-labelledby="providers"] a[href="https://shorebird.dev/privacy"]')
        self.assertIsNotNone(link)
        disclosure = link.find_parent('p').get_text(' ', strip=True)
        for phrase in ['starting with version 1.0.3', 'identifier for this installation',
                       'install and failure events', 'does not send your weather queries']:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, disclosure)


if __name__ == '__main__':
    unittest.main()
